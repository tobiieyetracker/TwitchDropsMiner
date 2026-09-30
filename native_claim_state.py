"""Durable one-click claim budget and a gate for the website's own recovery.

No HTTP or browser operations live here. Callers must first verify the webpage's
Inventory identity against the imported cookies, and call authorize_request
before allowing each actual ClaimDrop request onto the network.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Callable

from finish_drop_journal import FinishJournal, _FIELDS, _MAX_BYTES, _object, _validate
from finish_drop_state import Target
from watch_check_state import WatchCheckError
from web_session import WEB_CLIENT_ID


OPERATION_NAME = "DropsPage_ClaimDropRewards"
IDENTITY_HEADERS = (
    "authorization", "client-id", "user-agent", "x-device-id",
    "client-session-id", "client-version",
)
BINDING_FIELDS = ("user_id", "campaign_id", "campaign_name", "drop_id")
MAX_INVENTORY_AGE = 60
_NATIVE_FIELDS = _FIELDS | {"requests_reserved", "recovery_reserved_at"}


def _fail(code: str):
    raise WatchCheckError(code)


def _native_record(record: Any) -> dict:
    if type(record) is not dict or set(record) != _NATIVE_FIELDS:
        _fail("native_claim_journal_invalid")
    _validate({key: record[key] for key in _FIELDS})
    count, recovered = record["requests_reserved"], record["recovery_reserved_at"]
    if type(count) is not int or count not in (1, 2):
        _fail("native_claim_journal_invalid")
    if count == 1:
        if recovered is not None:
            _fail("native_claim_journal_invalid")
    else:
        # Reuse the base validator's strict UTC timestamp rules.
        _validate({**{key: record[key] for key in _FIELDS}, "attempted_at": recovered})
        if datetime.fromisoformat(recovered.replace("Z", "+00:00")) < datetime.fromisoformat(
            record["attempted_at"].replace("Z", "+00:00")
        ):
            _fail("native_claim_journal_invalid")
    return dict(record)


class NativeClaimJournal(FinishJournal):
    """A fixed native-claim record sharing the original stable process lock."""

    def __init__(self, state_dir: Path):
        super().__init__(state_dir)
        self.original_path = self.state_dir / "journal.json"
        self.web_candidate_path = self.state_dir / "web-query-claim-v1.json"
        self.journal_path = self.state_dir / "native-inventory-claim-v1.json"
        self._reserved_in_this_run = False

    def __exit__(self, exc_type, exc, traceback):
        self._reserved_in_this_run = False
        return super().__exit__(exc_type, exc, traceback)

    @staticmethod
    def _check_binding(first: dict, second: dict):
        if any(first[key] != second[key] for key in BINDING_FIELDS):
            _fail("native_claim_binding_mismatch")

    def originals(self) -> tuple[dict, dict]:
        original = self._read_at(self.original_path)
        candidate = self._read_at(self.web_candidate_path)
        if original is None or candidate is None:
            _fail("native_claim_prior_records_missing")
        self._check_binding(original, candidate)
        return original, candidate

    def read(self) -> dict | None:
        originals = self.originals()
        try:
            with self.journal_path.open("rb") as source:
                content = source.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            _fail("native_claim_journal_read_failed")
        if len(content) > _MAX_BYTES:
            _fail("native_claim_journal_invalid")
        try:
            record = _native_record(json.loads(content, object_pairs_hook=_object))
        except (ValueError, TypeError, UnicodeError):
            _fail("native_claim_journal_invalid")
        self._check_binding(originals[0], record)
        return record

    def _write(self, record: dict):
        self._require_lock()
        record = _native_record(record)
        content = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(content) > _MAX_BYTES:
            _fail("native_claim_journal_invalid")
        temp_path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".native-claim-", suffix=".tmp", dir=self.state_dir)
            temp_path = Path(name)
            with os.fdopen(fd, "wb") as target:
                if os.name != "nt":
                    os.fchmod(target.fileno(), 0o600)
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, self.journal_path)
            self._sync_directory()
        except OSError:
            _fail("native_claim_journal_write_failed")
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def record_attempt(self, user_id, campaign_id: str, campaign_name: str, drop_id: str) -> dict:
        originals = self.originals()
        if self.read() is not None:
            _fail("native_claim_previous_attempt")
        if any(old["outcome"] == "confirmed" for old in originals):
            _fail("native_claim_prior_confirmed")
        record = _native_record({
            "version": 1, "user_id": str(user_id) if type(user_id) is int else user_id,
            "campaign_id": campaign_id, "campaign_name": campaign_name, "drop_id": drop_id,
            "attempted_at": datetime.now(timezone.utc).isoformat(), "outcome": "attempted",
            "requests_reserved": 1, "recovery_reserved_at": None,
        })
        self._check_binding(originals[0], record)
        self._write(record)
        self._reserved_in_this_run = True
        return dict(record)

    def reserve_recovery(self) -> dict:
        record = self.read()
        if not self._reserved_in_this_run:
            _fail("native_claim_previous_attempt")
        if record is None or record["outcome"] != "attempted" or record["requests_reserved"] != 1:
            _fail("native_claim_request_budget_exhausted")
        if any(old["outcome"] == "confirmed" for old in self.originals()):
            _fail("native_claim_prior_confirmed")
        record.update(requests_reserved=2, recovery_reserved_at=datetime.now(timezone.utc).isoformat())
        self._write(record)
        return dict(record)

    def confirm(self) -> dict:
        record = self.read()
        if record is None:
            _fail("native_claim_attempt_missing")
        if record["outcome"] != "confirmed":
            record["outcome"] = "confirmed"
            self._write(record)
        return dict(record)


def _headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        _fail("native_claim_headers_invalid")
    result = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            _fail("native_claim_headers_invalid")
        name = key.lower()
        if name in result and result[name] != value:
            _fail("native_claim_headers_invalid")
        result[name] = value
    return result


def _integrity_only(body: Any) -> bool:
    if type(body) is not dict or type(body.get("extensions")) is not dict:
        return False
    challenge = body["extensions"].get("challenge")
    if type(challenge) is not dict or challenge.get("type") != "integrity":
        return False
    # A non-null mutation result is ambiguous even if a challenge accompanies it.
    # In particular, never replay a response already containing a success status.
    return body.get("data") in (None, {}, {"claimDropRewards": None})


class NativeClaimGate:
    """Authorize the first request and at most one observed official recovery.

    The caller supplies the timestamp and six request headers of a freshly
    accepted website Inventory response, already checked against its cookies.
    Bodies, OAuth, integrity tokens and the raw drop instance remain private.
    """

    def __init__(
        self, journal: NativeClaimJournal, user_id: str, target: Target,
        inventory_observed_at: float, *, expected_headers: dict[str, str],
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.journal, self.target = journal, target
        self._clock, self._utcnow = clock, utcnow
        self._inventory_at = inventory_observed_at
        expected = _headers(expected_headers)
        if any(not expected.get(name) for name in IDENTITY_HEADERS):
            _fail("native_claim_identity_headers_missing")
        if (
            expected["client-id"] != WEB_CLIENT_ID
            or not expected["authorization"].startswith("OAuth ")
            or len(expected["authorization"]) <= 6
        ):
            _fail("native_claim_identity_headers_invalid")
        self._identity = {name: expected[name] for name in IDENTITY_HEADERS}
        self._binding = {
            "user_id": str(user_id), "campaign_id": target.campaign_id,
            "campaign_name": target.name, "drop_id": target.drop_id,
        }
        for old in journal.originals():
            journal._check_binding(old, self._binding)
        self.reconcile_only = journal.read() is not None
        self._first_operation: dict | None = None
        self._first_token: str | None = None
        self._requests = 0
        self._first_response_seen = False
        self._challenge_at: float | None = None
        self._issuances: dict[str, tuple[float, float]] = {}
        self._blocked = False

    @property
    def summary(self) -> dict[str, int | bool]:
        return {
            "reconcile_only": self.reconcile_only, "requests_authorized": self._requests,
            "first_response_seen": self._first_response_seen,
            "first_response_integrity_only": self._challenge_at is not None,
            "stopped": self._blocked,
        }

    def _timestamp(self, value: float | None) -> float:
        value = self._clock() if value is None else value
        if type(value) not in (int, float) or not isfinite(value) or value < 0 or value > self._clock():
            _fail("native_claim_observation_time_invalid")
        return value

    def _same_identity(self, headers: dict[str, str]) -> bool:
        return all(headers.get(name) == value for name, value in self._identity.items())

    def recovery_evidence_ready(self, headers: dict[str, str]) -> bool:
        """Check this request's token without consuming budget or stopping the gate.

        Browser callbacks can finish parsing different issuance responses out of
        order. An event for any issuance is not proof about this request's token.
        """
        if self.reconcile_only or self._blocked or self._requests != 1 or self._challenge_at is None:
            return False
        try:
            actual = _headers(headers)
        except WatchCheckError:
            return False
        token = actual.get("client-integrity")
        if not self._same_identity(actual) or not token or token == self._first_token:
            return False
        issuance = self._issuances.get(token)
        return bool(
            issuance is not None and issuance[0] > self._challenge_at
            and issuance[1] > self._utcnow().timestamp() * 1000
        )

    def authorize_request(self, operation: Any, headers: dict[str, str]) -> None:
        """Reserve durable budget synchronously, before the caller continues the request."""
        try:
            if self.reconcile_only or self._blocked:
                _fail("native_claim_previous_attempt" if self.reconcile_only else "native_claim_stopped")
            if self._requests >= 2:
                _fail("native_claim_request_budget_exhausted")
            actual_headers = _headers(headers)
            if not self._same_identity(actual_headers):
                _fail("native_claim_identity_mismatch")
            if type(operation) is not dict or set(operation) != {"operationName", "variables", "extensions"}:
                _fail("native_claim_operation_not_allowed")
            if operation["operationName"] != OPERATION_NAME:
                _fail("native_claim_operation_not_allowed")
            if operation["variables"] != {"input": {"dropInstanceID": self.target.require_claim_id(self._utcnow())}}:
                _fail("native_claim_target_mismatch")
            if self.target.account_link_state is False:
                _fail("native_claim_account_not_linked")
            extensions = operation["extensions"]
            query = extensions.get("persistedQuery") if type(extensions) is dict else None
            if (
                type(extensions) is not dict or set(extensions) != {"persistedQuery"}
                or type(query) is not dict or set(query) != {"version", "sha256Hash"}
                or type(query["version"]) is not int or query["version"] != 1
                or type(query["sha256Hash"]) is not str
                or not re.fullmatch(r"[a-fA-F0-9]{64}", query["sha256Hash"])
            ):
                _fail("native_claim_operation_not_allowed")
            token = actual_headers.get("client-integrity") or None
            if self._requests == 0:
                age = self._clock() - self._timestamp(self._inventory_at)
                if age > MAX_INVENTORY_AGE:
                    _fail("native_claim_inventory_stale")
                self.journal.record_attempt(*(self._binding[key] for key in BINDING_FIELDS))
                self._first_operation, self._first_token = deepcopy(operation), token
            else:
                if operation != self._first_operation or not token or token == self._first_token:
                    _fail("native_claim_recovery_not_allowed")
                if not self.recovery_evidence_ready(actual_headers):
                    _fail("native_claim_recovery_not_allowed")
                self.journal.reserve_recovery()
            self._requests += 1
        except Exception:
            self._blocked = True
            raise

    def observe_response(self, body: Any, *, http_status: int = 200, received_at: float | None = None) -> bool:
        """Only the first explicit, data-free integrity challenge can permit recovery."""
        if self._blocked or self._requests != 1 or self._first_response_seen:
            self._blocked = True
            return False
        self._first_response_seen = True
        if http_status == 200 and _integrity_only(body):
            self._challenge_at = self._timestamp(received_at)
            return True
        self._blocked = True
        return False

    def observe_issuance(
        self, body: Any, headers: dict[str, str], *, http_status: int = 200,
        received_at: float | None = None,
    ) -> bool:
        """Remember only matching 200 issuances; actual response timestamps decide order."""
        if self._blocked or self.reconcile_only or type(body) is not dict or http_status != 200:
            return False
        if not self._same_identity(_headers(headers)):
            return False
        token, expiration = body.get("token"), body.get("expiration")
        if (
            type(token) is not str or not token or "error" in body
            or type(expiration) not in (int, float) or not isfinite(expiration)
            or expiration <= self._utcnow().timestamp() * 1000
        ):
            return False
        stamp = self._timestamp(received_at)
        if token not in self._issuances and len(self._issuances) >= 4:
            return False
        self._issuances[token] = stamp, expiration
        return True

    def observe_failure(self) -> None:
        """A transport/parser failure has unknown effects and never permits replay."""
        self._blocked = True

    def clear(self) -> None:
        self._blocked = True
        self._identity.clear()
        self._issuances.clear()
        self._first_operation = None
        self._first_token = None
        self.target = None
        self._binding.clear()
