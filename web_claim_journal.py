"""One fixed web-query claim candidate, beside the immutable original attempt.

Both entries use the original process lock. This is not a general retry ledger:
there is exactly one fixed candidate filename and any existing candidate entry
prevents another attempt, including entries with an unconfirmed outcome.
"""
from __future__ import annotations

from pathlib import Path

from finish_drop_journal import FinishJournal
from watch_check_state import WatchCheckError


_BINDING_FIELDS = ("user_id", "campaign_id", "campaign_name", "drop_id")


class WebClaimJournal(FinishJournal):
    """Keep the fixed candidate entry under the original stable process lock."""

    def __init__(self, state_dir: Path):
        super().__init__(state_dir)
        self.original_path = self.state_dir / "journal.json"
        self.journal_path = self.state_dir / "web-query-claim-v1.json"

    def original(self) -> dict:
        record = self._read_at(self.original_path)
        if record is None:
            raise WatchCheckError("web_claim_original_missing")
        return record

    @staticmethod
    def _check_binding(original: dict, candidate: dict) -> None:
        if any(candidate[key] != original[key] for key in _BINDING_FIELDS):
            raise WatchCheckError("web_claim_binding_mismatch")

    def read(self) -> dict | None:
        record = super().read()
        if record is not None:
            self._check_binding(self.original(), record)
        return record

    def record_attempt(self, user_id, campaign_id: str, campaign_name: str, drop_id: str) -> dict:
        original = self.original()
        if original["outcome"] != "attempted":
            raise WatchCheckError("web_claim_original_confirmed")
        normalized_id = str(user_id) if type(user_id) is int and user_id > 0 else user_id
        self._check_binding(original, {
            "user_id": normalized_id,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "drop_id": drop_id,
        })
        return super().record_attempt(normalized_id, campaign_id, campaign_name, drop_id)
