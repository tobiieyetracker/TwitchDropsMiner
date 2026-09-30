import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import native_claim_state as state
from finish_drop_journal import FinishJournal
from finish_drop_state import Target
from native_claim_state import NativeClaimGate, NativeClaimJournal
from watch_check_state import WatchCheckError
from web_claim_journal import WebClaimJournal


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
CLAIM_ID = "fixture-private-instance"
TOKEN = "fixture-private-oauth"
OLD_INTEGRITY = "fixture-old-integrity"
NEW_INTEGRITY = "fixture-new-integrity"
HEADERS = {
    "Authorization": "OAuth " + TOKEN, "Client-Id": state.WEB_CLIENT_ID,
    "User-Agent": "fixture-private-ua", "X-Device-Id": "fixture-private-device",
    "Client-Session-Id": "fixture-private-session", "Client-Version": "fixture-private-version",
}
CHALLENGE = {"data": {"claimDropRewards": None}, "extensions": {"challenge": {"type": "integrity"}}}
OPERATION = {
    "operationName": state.OPERATION_NAME, "variables": {"input": {"dropInstanceID": CLAIM_ID}},
    "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "a" * 64}},
}


class Clock:
    now = 10.0

    def __call__(self):
        return self.now


def target(**changes):
    start, end = NOW - timedelta(days=1), NOW + timedelta(days=1)
    item = Target(
        campaign_id="campaign", name="Test campaign", drop_id="drop", game_id="game",
        required_minutes=60, minutes=60, is_claimed=False, claim_id=CLAIM_ID,
        starts_at=start, ends_at=end, campaign_ends_at=end, account_link_state=True,
        preconditions_met=True, source_time_bounds=(start, end, start, end),
    )
    return replace(item, **changes)


def seed(path):
    with FinishJournal(path) as journal:
        journal.record_attempt("42", "campaign", "Test campaign", "drop")
    with WebClaimJournal(path) as journal:
        journal.record_attempt("42", "campaign", "Test campaign", "drop")
    return {name: path.joinpath(name).read_bytes() for name in ("journal.json", "web-query-claim-v1.json")}


def gate(journal, clock, **kwargs):
    return NativeClaimGate(
        journal, kwargs.pop("user_id", "42"), kwargs.pop("target", target()),
        kwargs.pop("inventory_observed_at", clock.now),
        expected_headers=kwargs.pop("expected_headers", HEADERS), clock=clock,
        utcnow=lambda: NOW, **kwargs,
    )


def assert_code(code, action, *args, **kwargs):
    with pytest.raises(WatchCheckError) as caught:
        action(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def issue(control, clock, *, token=NEW_INTEGRITY, headers=HEADERS, status=200, stamp=None, expiration=None):
    return control.observe_issuance(
        {"token": token, "expiration": NOW.timestamp() * 1000 + 60_000 if expiration is None else expiration},
        headers, http_status=status, received_at=clock.now if stamp is None else stamp,
    )


def allow_recovery(control, clock):
    clock.now = 20
    assert control.observe_response(CHALLENGE, received_at=20)
    clock.now = 21
    assert issue(control, clock)


def test_initial_and_single_official_recovery_reserve_durable_budget_first(tmp_path):
    old = seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        assert journal.lock_path == tmp_path / "finish-drop.lock"
        assert journal.journal_path == tmp_path / "native-inventory-claim-v1.json"
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        first = journal.read()
        assert first["requests_reserved"] == 1 and first["recovery_reserved_at"] is None
        assert first["outcome"] == "attempted"
        allow_recovery(control, clock)
        control.authorize_request(OPERATION, {**HEADERS, "Client-Integrity": NEW_INTEGRITY})
        assert journal.read()["requests_reserved"] == 2
        assert journal.read()["recovery_reserved_at"] is not None
        assert_code("native_claim_request_budget_exhausted", control.authorize_request,
                    OPERATION, {**HEADERS, "Client-Integrity": "third-token"})
        assert journal.confirm()["outcome"] == "confirmed"
        output = json.dumps(journal.read()) + repr(control) + json.dumps(control.summary)
        assert "fixture-" not in output
        for name, original in old.items():
            assert (tmp_path / name).read_bytes() == original


@pytest.mark.parametrize("reserve_recovery,confirmed", [(False, False), (True, False), (True, True)])
def test_reopening_after_any_attempt_only_allows_reconciliation(tmp_path, reserve_recovery, confirmed):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        original = gate(journal, clock)
        original.authorize_request(OPERATION, HEADERS)
        if reserve_recovery:
            allow_recovery(original, clock)
            original.authorize_request(OPERATION, {**HEADERS, "Client-Integrity": NEW_INTEGRITY})
        if confirmed:
            journal.confirm()
    with NativeClaimJournal(tmp_path) as journal:
        before = journal.journal_path.read_bytes()
        resumed = gate(journal, clock)
        assert resumed.reconcile_only
        assert_code("native_claim_previous_attempt", resumed.authorize_request, OPERATION, HEADERS)
        assert_code("native_claim_previous_attempt", journal.reserve_recovery)
        assert journal.journal_path.read_bytes() == before
        assert journal.confirm()["outcome"] == "confirmed"


@pytest.mark.parametrize("filename", ["journal.json", "web-query-claim-v1.json"])
def test_both_prior_records_are_mandatory_and_immutable(tmp_path, filename):
    seed(tmp_path)
    tmp_path.joinpath(filename).unlink()
    with NativeClaimJournal(tmp_path) as journal:
        assert_code("native_claim_prior_records_missing", journal.originals)
        assert_code("native_claim_prior_records_missing", journal.read)
        assert not journal.journal_path.exists()


@pytest.mark.parametrize("filename", ["journal.json", "web-query-claim-v1.json"])
def test_confirmed_prior_records_allow_read_but_no_new_mutation(tmp_path, filename):
    seed(tmp_path)
    path = tmp_path / filename
    record = json.loads(path.read_text())
    record["outcome"] = "confirmed"
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    with NativeClaimJournal(tmp_path) as journal:
        assert journal.read() is None
        control = gate(journal, Clock())
        assert_code("native_claim_prior_confirmed", control.authorize_request, OPERATION, HEADERS)
        assert not journal.journal_path.exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("user_id", "99"), ("campaign_id", "other"),
                                         ("campaign_name", "Other"), ("drop_id", "other")])
def test_prior_record_binding_mismatch_stops_without_creating_file(tmp_path, field, value):
    seed(tmp_path)
    path = tmp_path / "web-query-claim-v1.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with NativeClaimJournal(tmp_path) as journal:
        assert_code("native_claim_binding_mismatch", journal.read)
        assert not journal.journal_path.exists()


def test_gate_target_and_user_must_match_prior_records(tmp_path):
    seed(tmp_path)
    with NativeClaimJournal(tmp_path) as journal:
        assert_code("native_claim_binding_mismatch", gate, journal, Clock(), user_id="99")
        assert_code("native_claim_binding_mismatch", gate, journal, Clock(), target=target(drop_id="other"))


def test_native_uses_same_lock_as_prior_tools_and_requires_lock(tmp_path):
    seed(tmp_path)
    native = NativeClaimJournal(tmp_path)
    assert_code("finish_journal_not_locked", native.read)
    with FinishJournal(tmp_path):
        assert_code("finish_journal_locked", native.__enter__)
    with native:
        with pytest.raises(WatchCheckError) as caught:
            with WebClaimJournal(tmp_path):
                pass
        assert caught.value.code == "finish_journal_locked"


@pytest.mark.parametrize("corruption", [
    b"{}", b"invalid-secret", b'{"version":1,"version":1}', b"x" * 17_000,
])
def test_corrupt_native_record_is_never_deleted_or_replaced(tmp_path, corruption):
    seed(tmp_path)
    with NativeClaimJournal(tmp_path) as journal:
        journal.journal_path.write_bytes(corruption)
        with pytest.raises(WatchCheckError):
            journal.read()
        assert journal.journal_path.read_bytes() == corruption


@pytest.mark.parametrize("change", ["other_operation", "other_instance", "extra_variable", "raw_query", "bad_hash"])
def test_only_one_exact_target_persisted_mutation_can_be_sent(tmp_path, change):
    seed(tmp_path)
    operation = deepcopy(OPERATION)
    if change == "other_operation":
        operation["operationName"] = "Inventory"
    elif change == "other_instance":
        operation["variables"]["input"]["dropInstanceID"] = "other-private-instance"
    elif change == "extra_variable":
        operation["variables"]["extra"] = True
    elif change == "raw_query":
        operation["query"] = "mutation { anything }"
    else:
        operation["extensions"]["persistedQuery"]["sha256Hash"] = "bad"
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, Clock())
        with pytest.raises(WatchCheckError):
            control.authorize_request(operation, HEADERS)
        assert journal.read() is None
        assert control.summary["stopped"]


@pytest.mark.parametrize("name", list(HEADERS))
def test_first_request_requires_all_six_website_inventory_identity_headers(tmp_path, name):
    seed(tmp_path)
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, Clock())
        assert_code("native_claim_identity_mismatch", control.authorize_request,
                    OPERATION, {**HEADERS, name: "different-private-value"})
        assert journal.read() is None


def test_first_request_rejects_stale_inventory(tmp_path):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        clock.now += state.MAX_INVENTORY_AGE + 1
        assert_code("native_claim_inventory_stale", control.authorize_request, OPERATION, HEADERS)
        assert journal.read() is None


@pytest.mark.parametrize("body,http_status", [
    ({}, 200), ({"errors": [{"message": "private failure"}]}, 200), (CHALLENGE, 429),
    ({"extensions": {"challenge": {"type": "other"}}}, 200),
    ({**CHALLENGE, "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}, 200),
    ({**CHALLENGE, "data": {"unrelatedSuccess": True}}, 200),
    ({**CHALLENGE, "data": {"claimDropRewards": {"status": None}}}, 200),
])
def test_unknown_failed_or_successful_responses_never_authorize_replay(tmp_path, body, http_status):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        assert not control.observe_response(body, http_status=http_status)
        assert_code("native_claim_stopped", control.authorize_request,
                    OPERATION, {**HEADERS, "Client-Integrity": NEW_INTEGRITY})
        assert journal.read()["requests_reserved"] == 1


@pytest.mark.parametrize("case", ["unobserved", "before_challenge", "same_time", "different_identity", "bad_http", "expired", "same_token"])
def test_recovery_requires_a_new_matching_post_challenge_issuance(tmp_path, case):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        control.authorize_request(OPERATION, {**HEADERS, "Client-Integrity": OLD_INTEGRITY})
        clock.now = 20
        assert control.observe_response(CHALLENGE, received_at=20)
        clock.now = 21
        if case != "unobserved":
            issue(control, clock,
                  token=OLD_INTEGRITY if case == "same_token" else NEW_INTEGRITY,
                  stamp=19 if case == "before_challenge" else 20 if case == "same_time" else 21,
                  headers={**HEADERS, "X-Device-Id": "wrong-device"} if case == "different_identity" else HEADERS,
                  status=403 if case == "bad_http" else 200,
                  expiration=NOW.timestamp() * 1000 if case == "expired" else None)
        assert_code("native_claim_recovery_not_allowed", control.authorize_request,
                    OPERATION, {**HEADERS, "Client-Integrity": OLD_INTEGRITY if case == "same_token" else NEW_INTEGRITY})
        assert journal.read()["requests_reserved"] == 1


def test_issuance_parsed_first_can_match_later_parsed_challenge_by_actual_event_times(tmp_path):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        clock.now = 30
        assert issue(control, clock, stamp=21)
        assert control.observe_response(CHALLENGE, received_at=20)
        control.authorize_request(OPERATION, {**HEADERS, "Client-Integrity": NEW_INTEGRITY})
        assert journal.read()["requests_reserved"] == 2


def test_recovery_readiness_waits_for_exact_token_without_consuming_or_stopping(tmp_path):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        clock.now = 20
        assert control.observe_response(CHALLENGE, received_at=20)
        clock.now = 21
        assert issue(control, clock, token="some-other-issued-token")
        retry_headers = {**HEADERS, "Client-Integrity": NEW_INTEGRITY}
        assert not control.recovery_evidence_ready(retry_headers)
        assert not control.summary["stopped"]
        assert journal.read()["requests_reserved"] == 1
        assert issue(control, clock)
        assert control.recovery_evidence_ready(retry_headers)
        assert journal.read()["requests_reserved"] == 1
        control.authorize_request(OPERATION, retry_headers)
        assert not control.recovery_evidence_ready(retry_headers)
        assert journal.read()["requests_reserved"] == 2


def test_recovery_readiness_rechecks_expiration_at_use_time(tmp_path):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        allow_recovery(control, clock)
        headers = {**HEADERS, "Client-Integrity": NEW_INTEGRITY}
        assert control.recovery_evidence_ready(headers)
        control._utcnow = lambda: NOW + timedelta(minutes=2)
        assert not control.recovery_evidence_ready(headers)
        assert not control.summary["stopped"]


@pytest.mark.parametrize("change", ["hash", "identity"])
def test_official_recovery_cannot_change_payload_or_identity(tmp_path, change):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        allow_recovery(control, clock)
        operation, headers = deepcopy(OPERATION), {**HEADERS, "Client-Integrity": NEW_INTEGRITY}
        if change == "hash":
            operation["extensions"]["persistedQuery"]["sha256Hash"] = "b" * 64
        else:
            headers["Client-Session-Id"] = "other-session"
        with pytest.raises(WatchCheckError):
            control.authorize_request(operation, headers)
        assert journal.read()["requests_reserved"] == 1


def test_lost_response_blocks_replay_and_clearing_removes_cached_secrets(tmp_path):
    seed(tmp_path)
    with NativeClaimJournal(tmp_path) as journal:
        clock = Clock()
        control = gate(journal, clock)
        control.authorize_request(OPERATION, HEADERS)
        issue(control, clock)
        control.observe_failure()
        assert_code("native_claim_stopped", control.authorize_request, OPERATION, HEADERS)
        control.clear()
        assert control._identity == {} and control._issuances == {}
        assert control._first_operation is None and control._first_token is None
        assert control.target is None and control._binding == {}


@pytest.mark.parametrize("failure_phase", ["first", "recovery"])
def test_failed_durable_write_never_grants_request_authorization(tmp_path, monkeypatch, failure_phase):
    old = seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)
        if failure_phase == "recovery":
            control.authorize_request(OPERATION, HEADERS)
            allow_recovery(control, clock)
        before = journal.journal_path.read_bytes() if journal.journal_path.exists() else None

        def fail(*args):
            raise OSError("fixture-private-error")

        monkeypatch.setattr(state.os, "replace", fail)
        error = assert_code("native_claim_journal_write_failed", control.authorize_request,
                            OPERATION, {**HEADERS, "Client-Integrity": NEW_INTEGRITY})
        assert "fixture-" not in str(error)
        assert control.summary["stopped"]
        assert (journal.journal_path.read_bytes() if journal.journal_path.exists() else None) == before
        for name, original in old.items():
            assert (tmp_path / name).read_bytes() == original
        assert not list(tmp_path.glob(".native-claim-*.tmp"))


def test_directory_sync_failure_preserves_consumed_budget_for_crash_reconciliation(tmp_path, monkeypatch):
    seed(tmp_path)
    clock = Clock()
    with NativeClaimJournal(tmp_path) as journal:
        control = gate(journal, clock)

        def fail():
            raise OSError("fixture-private-sync-error")

        monkeypatch.setattr(journal, "_sync_directory", fail)
        assert_code("native_claim_journal_write_failed", control.authorize_request, OPERATION, HEADERS)
        assert journal.read()["requests_reserved"] == 1
        assert_code("native_claim_stopped", control.authorize_request, OPERATION, HEADERS)
    with NativeClaimJournal(tmp_path) as journal:
        assert gate(journal, clock).reconcile_only
