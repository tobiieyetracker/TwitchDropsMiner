"""Fixed matching-SMARTBOX experiment, preserving all previous claim records."""
from __future__ import annotations

from pathlib import Path

from finish_drop_journal import FinishJournal
from watch_check_state import WatchCheckError


_BINDING_FIELDS = ("user_id", "campaign_id", "campaign_name", "drop_id")


class SmartboxClaimJournal(FinishJournal):
    """One candidate record under the original finish-drop process lock."""

    def __init__(self, state_dir: Path):
        super().__init__(state_dir)
        self.original_path = self.state_dir / "journal.json"
        self.journal_path = self.state_dir / "smartbox-claim-v1.json"

    def original(self) -> dict:
        record = self._read_at(self.original_path)
        if record is None:
            raise WatchCheckError("smartbox_claim_original_missing")
        return record

    @staticmethod
    def _check_binding(original: dict, candidate: dict) -> None:
        if any(candidate[key] != original[key] for key in _BINDING_FIELDS):
            raise WatchCheckError("smartbox_claim_binding_mismatch")

    def read(self) -> dict | None:
        record = super().read()
        if record is not None:
            self._check_binding(self.original(), record)
        return record

    def record_attempt(self, user_id, campaign_id: str, campaign_name: str, drop_id: str) -> dict:
        original = self.original()
        if original["outcome"] != "attempted":
            raise WatchCheckError("smartbox_claim_original_confirmed")
        normalized_id = str(user_id) if type(user_id) is int and user_id > 0 else user_id
        self._check_binding(original, {
            "user_id": normalized_id, "campaign_id": campaign_id,
            "campaign_name": campaign_name, "drop_id": drop_id,
        })
        return super().record_attempt(normalized_id, campaign_id, campaign_name, drop_id)
