import json
import subprocess
import sys
from pathlib import Path

import pytest

import finish_drop_journal as journal_module
from finish_drop_journal import FinishJournal
from web_claim_journal import WebClaimJournal
from watch_check_state import WatchCheckError


def record(journal):
    return journal.record_attempt(42, "campaign", "Test campaign", "drop")


def seed_original(state_dir, *, confirmed=False):
    with FinishJournal(state_dir) as journal:
        original = record(journal)
        if confirmed:
            original = journal.confirm()
        # Non-default formatting proves the candidate does not rewrite the old file.
        journal.journal_path.write_text(json.dumps(original, separators=(",", ":")) + "\n\n",
                                       encoding="utf-8")
        return original, journal.journal_path.read_bytes()


def assert_error(code, action, *args):
    with pytest.raises(WatchCheckError) as caught:
        action(*args)
    assert caught.value.code == code


def test_fixed_candidate_confirmation_never_modifies_original(tmp_path):
    original, original_bytes = seed_original(tmp_path)
    with WebClaimJournal(tmp_path) as journal:
        assert journal.lock_path == tmp_path / "finish-drop.lock"
        assert journal.journal_path == tmp_path / "web-query-claim-v1.json"
        assert journal.original() == original
        assert journal.read() is None
        candidate = record(journal)
        assert candidate["outcome"] == "attempted"
        assert journal.original_path.read_bytes() == original_bytes
        confirmed = journal.confirm()
        assert confirmed == {**candidate, "outcome": "confirmed"}
        assert journal.confirm() == confirmed
        assert journal.original_path.read_bytes() == original_bytes
    with WebClaimJournal(tmp_path) as journal:
        assert journal.read() == confirmed
        assert journal.original() == original
        assert journal.original_path.read_bytes() == original_bytes


@pytest.mark.parametrize("confirmed", [False, True])
def test_any_existing_candidate_prevents_another_attempt(tmp_path, confirmed):
    _, original_bytes = seed_original(tmp_path)
    with WebClaimJournal(tmp_path) as journal:
        record(journal)
        if confirmed:
            journal.confirm()
        candidate_bytes = journal.journal_path.read_bytes()
        assert_error("finish_journal_attempt_exists", record, journal)
        assert journal.journal_path.read_bytes() == candidate_bytes
        assert journal.original_path.read_bytes() == original_bytes


def test_original_is_required_for_new_and_existing_candidate(tmp_path):
    with WebClaimJournal(tmp_path) as journal:
        assert_error("web_claim_original_missing", journal.original)
        assert_error("web_claim_original_missing", record, journal)
        assert not journal.journal_path.exists()
    other_dir = tmp_path / "other"
    with FinishJournal(other_dir) as other:
        existing = record(other)
    with WebClaimJournal(tmp_path) as journal:
        journal.journal_path.write_text(json.dumps(existing), encoding="utf-8")
        assert_error("web_claim_original_missing", journal.read)
        assert_error("web_claim_original_missing", journal.confirm)


@pytest.mark.parametrize("content", [b"", b"{", b"{}", b'{"version":1,"version":1}', b"x" * 17000])
def test_corrupt_original_is_preserved_and_blocks_candidate(tmp_path, content):
    tmp_path.joinpath("journal.json").write_bytes(content)
    with WebClaimJournal(tmp_path) as journal:
        assert_error("finish_journal_invalid", journal.original)
        assert_error("finish_journal_invalid", record, journal)
        assert not journal.journal_path.exists()
        assert journal.original_path.read_bytes() == content


def test_confirmed_original_cannot_authorize_candidate(tmp_path):
    original, original_bytes = seed_original(tmp_path, confirmed=True)
    with WebClaimJournal(tmp_path) as journal:
        assert journal.original() == original
        assert_error("web_claim_original_confirmed", record, journal)
        assert not journal.journal_path.exists()
        assert journal.original_path.read_bytes() == original_bytes


@pytest.mark.parametrize("field,value", [
    ("user_id", "99"), ("campaign_id", "another-campaign"),
    ("campaign_name", "Another campaign"), ("drop_id", "another-drop"),
])
def test_new_binding_mismatch_never_writes_candidate(tmp_path, field, value):
    original, original_bytes = seed_original(tmp_path)
    original[field] = value
    with WebClaimJournal(tmp_path) as journal:
        assert_error("web_claim_binding_mismatch", journal.record_attempt,
                     *(original[key] for key in ("user_id", "campaign_id", "campaign_name", "drop_id")))
        assert not journal.journal_path.exists()
        assert journal.original_path.read_bytes() == original_bytes


@pytest.mark.parametrize("field,value", [
    ("user_id", "99"), ("campaign_id", "another-campaign"),
    ("campaign_name", "Another campaign"), ("drop_id", "another-drop"),
])
def test_existing_binding_mismatch_blocks_read_confirmation_and_attempt(tmp_path, field, value):
    _, original_bytes = seed_original(tmp_path)
    with WebClaimJournal(tmp_path) as journal:
        candidate = record(journal)
        candidate[field] = value
        journal.journal_path.write_text(json.dumps(candidate), encoding="utf-8")
        candidate_bytes = journal.journal_path.read_bytes()
        assert_error("web_claim_binding_mismatch", journal.read)
        assert_error("web_claim_binding_mismatch", journal.confirm)
        assert_error("web_claim_binding_mismatch", record, journal)
        assert journal.journal_path.read_bytes() == candidate_bytes
        assert journal.original_path.read_bytes() == original_bytes


def test_corrupt_candidate_is_not_removed_or_overwritten(tmp_path):
    _, original_bytes = seed_original(tmp_path)
    with WebClaimJournal(tmp_path) as journal:
        journal.journal_path.write_bytes(b"{}")
        assert_error("finish_journal_invalid", journal.read)
        assert_error("finish_journal_invalid", journal.confirm)
        assert_error("finish_journal_invalid", record, journal)
        assert journal.journal_path.read_bytes() == b"{}"
        assert journal.original_path.read_bytes() == original_bytes


def test_all_access_requires_the_shared_lock(tmp_path):
    seed_original(tmp_path)
    journal = WebClaimJournal(tmp_path)
    for action in (journal.original, journal.read, journal.confirm):
        assert_error("finish_journal_not_locked", action)
    assert_error("finish_journal_not_locked", record, journal)


@pytest.mark.parametrize("parent_kind,child_kind", [
    (FinishJournal, "WebClaimJournal"), (WebClaimJournal, "FinishJournal"),
])
def test_candidate_and_original_lock_exclude_each_other_across_processes(tmp_path, parent_kind, child_kind):
    seed_original(tmp_path)
    code = """
import sys
from pathlib import Path
from finish_drop_journal import FinishJournal
from web_claim_journal import WebClaimJournal
from watch_check_state import WatchCheckError
kind = {'FinishJournal': FinishJournal, 'WebClaimJournal': WebClaimJournal}[sys.argv[2]]
try:
    with kind(Path(sys.argv[1])):
        print('opened')
except WatchCheckError as exc:
    print(exc.code)
    raise SystemExit(3)
"""

    def child():
        return subprocess.run([sys.executable, "-c", code, str(tmp_path), child_kind],
                              cwd=Path(journal_module.__file__).parent, text=True,
                              capture_output=True, timeout=20)

    with parent_kind(tmp_path):
        blocked = child()
        assert blocked.returncode == 3, blocked.stderr
        assert blocked.stdout.strip() == "finish_journal_locked"
    released = child()
    assert released.returncode == 0, released.stderr
    assert released.stdout.strip() == "opened"


def test_disk_replace_failure_creates_no_candidate_and_leaks_no_exception_text(tmp_path, monkeypatch):
    _, original_bytes = seed_original(tmp_path)
    secret = "fixture-private-oauth"

    def fail(*args):
        raise OSError(secret)

    with WebClaimJournal(tmp_path) as journal:
        monkeypatch.setattr(journal_module.os, "replace", fail)
        with pytest.raises(WatchCheckError) as caught:
            record(journal)
        assert caught.value.code == "finish_journal_write_failed"
        assert secret not in str(caught.value)
        assert journal.read() is None
        assert journal.original_path.read_bytes() == original_bytes
        assert not list(tmp_path.glob(".journal-*.tmp"))


def test_file_fsync_failure_prevents_record_and_preserves_original(tmp_path, monkeypatch):
    _, original_bytes = seed_original(tmp_path)

    def fail(*args):
        raise OSError()

    with WebClaimJournal(tmp_path) as journal:
        monkeypatch.setattr(journal_module.os, "fsync", fail)
        assert_error("finish_journal_write_failed", record, journal)
        assert journal.read() is None
        assert journal.original_path.read_bytes() == original_bytes
        assert not list(tmp_path.glob(".journal-*.tmp"))


def test_directory_fsync_failure_retains_candidate_to_prevent_repetition(tmp_path, monkeypatch):
    _, original_bytes = seed_original(tmp_path)

    def fail():
        raise OSError()

    with WebClaimJournal(tmp_path) as journal:
        monkeypatch.setattr(journal, "_sync_directory", fail)
        assert_error("finish_journal_write_failed", record, journal)
        assert journal.read()["outcome"] == "attempted"
        assert_error("finish_journal_attempt_exists", record, journal)
        assert journal.original_path.read_bytes() == original_bytes


def test_confirmation_failure_preserves_both_attempt_records(tmp_path, monkeypatch):
    _, original_bytes = seed_original(tmp_path)
    with WebClaimJournal(tmp_path) as journal:
        candidate = record(journal)
        candidate_bytes = journal.journal_path.read_bytes()

        def fail(*args):
            raise OSError()

        monkeypatch.setattr(journal_module.os, "replace", fail)
        assert_error("finish_journal_write_failed", journal.confirm)
        assert journal.read() == candidate
        assert journal.journal_path.read_bytes() == candidate_bytes
        assert journal.original_path.read_bytes() == original_bytes
