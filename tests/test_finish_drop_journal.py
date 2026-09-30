import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import finish_drop_journal as journal_module
from finish_drop_journal import FinishJournal
from watch_check_state import WatchCheckError


def attempt(journal):
    return journal.record_attempt(42, "campaign", "Test campaign", "drop")


def assert_error(code, callable, *args):
    with pytest.raises(WatchCheckError) as caught:
        callable(*args)
    assert caught.value.code == code


def test_attempt_and_confirmation_are_persistent_and_keep_binding(tmp_path):
    state = tmp_path / "persistent" / "finish-drop"
    with FinishJournal(state) as journal:
        assert journal.read() is None
        recorded = attempt(journal)
        assert recorded["user_id"] == "42"
        assert recorded["outcome"] == "attempted"
    with FinishJournal(state) as journal:
        assert journal.read() == recorded
        confirmed = journal.confirm()
        assert confirmed == {**recorded, "outcome": "confirmed"}
        assert journal.confirm() == confirmed
    with FinishJournal(state) as journal:
        assert journal.read() == confirmed
    assert journal.lock_path.exists()


@pytest.mark.parametrize("confirmed", [False, True])
def test_existing_record_is_never_overwritten(tmp_path, confirmed):
    with FinishJournal(tmp_path) as journal:
        original = attempt(journal)
        if confirmed:
            original = journal.confirm()
        before = journal.journal_path.read_bytes()
        assert_error("finish_journal_attempt_exists", journal.record_attempt,
                     "99", "another-campaign", "Another", "another-drop")
        assert journal.journal_path.read_bytes() == before
        assert journal.read() == original


def test_lock_required_and_missing_attempt_cannot_be_confirmed(tmp_path):
    journal = FinishJournal(tmp_path)
    assert_error("finish_journal_not_locked", journal.read)
    assert_error("finish_journal_not_locked", attempt, journal)
    assert_error("finish_journal_not_locked", journal.confirm)
    with journal:
        assert_error("finish_journal_attempt_missing", journal.confirm)
        assert_error("finish_journal_already_open", journal.__enter__)


def test_second_instance_blocked_and_exception_releases_lock(tmp_path):
    first, second = FinishJournal(tmp_path), FinishJournal(tmp_path)
    with pytest.raises(RuntimeError):
        with first:
            assert_error("finish_journal_locked", second.__enter__)
            raise RuntimeError("simulated caller failure")
    with second:
        assert second.read() is None
    assert first.lock_path.exists()


def test_lock_excludes_other_process_and_releases_after_exit(tmp_path):
    code = """
import sys
from pathlib import Path
from finish_drop_journal import FinishJournal
from watch_check_state import WatchCheckError
try:
    with FinishJournal(Path(sys.argv[1])):
        print('opened')
except WatchCheckError as exc:
    print(exc.code)
    raise SystemExit(3)
"""

    def child():
        return subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                              cwd=Path(journal_module.__file__).parent,
                              text=True, capture_output=True, timeout=20)

    with FinishJournal(tmp_path):
        blocked = child()
        assert blocked.returncode == 3, blocked.stderr
        assert blocked.stdout.strip() == "finish_journal_locked"
    released = child()
    assert released.returncode == 0, released.stderr
    assert released.stdout.strip() == "opened"


@pytest.mark.parametrize("content", [
    b"", b"not json", b"[]", b"null", b"{}", b"\xff", b"x" * 17000,
    b'{"version":1,"version":1}',
])
def test_corrupt_journal_stops_without_deleting_it(tmp_path, content):
    with FinishJournal(tmp_path) as journal:
        journal.journal_path.write_bytes(content)
        assert_error("finish_journal_invalid", journal.read)
        assert_error("finish_journal_invalid", attempt, journal)
        assert journal.journal_path.read_bytes() == content


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("user_id", 42), ("user_id", ""),
    ("user_id", "0"), ("campaign_id", None), ("campaign_id", " "),
    ("campaign_name", "bad\nname"), ("drop_id", ""),
    ("attempted_at", "yesterday"), ("attempted_at", "2026-09-30T12:00:00"),
    ("attempted_at", "2026-09-30T12:00:00+01:00"),
    ("outcome", "retry"), ("outcome", None), ("oauth", "fixture-secret"),
])
def test_schema_rejects_invalid_fields_and_unknown_keys(tmp_path, field, value):
    with FinishJournal(tmp_path) as journal:
        record = attempt(journal)
        record[field] = value
        journal.journal_path.write_text(json.dumps(record), encoding="utf-8")
        assert_error("finish_journal_invalid", journal.read)


@pytest.mark.parametrize("user_id", [None, True, 0, -1, 1.5, "invalid"])
def test_invalid_attempt_binding_does_not_create_record(tmp_path, user_id):
    with FinishJournal(tmp_path) as journal:
        assert_error("finish_journal_invalid", journal.record_attempt,
                     user_id, "campaign", "Test campaign", "drop")
        assert journal.read() is None


def test_replace_failure_preserves_absence_and_cleans_temporary_file(tmp_path, monkeypatch):
    secret = "fixture-private-proxy-password"

    def fail(*args):
        raise OSError(secret)

    with FinishJournal(tmp_path) as journal:
        monkeypatch.setattr(journal_module.os, "replace", fail)
        with pytest.raises(WatchCheckError) as caught:
            attempt(journal)
        assert caught.value.code == "finish_journal_write_failed"
        assert secret not in str(caught.value)
        assert journal.read() is None
        assert list(tmp_path.glob(".journal-*.tmp")) == []


def test_directory_sync_failure_keeps_written_attempt_and_blocks_repeat(tmp_path, monkeypatch):
    with FinishJournal(tmp_path) as journal:
        monkeypatch.setattr(journal, "_sync_directory", lambda: (_ for _ in ()).throw(OSError()))
        assert_error("finish_journal_write_failed", attempt, journal)
        assert journal.read()["outcome"] == "attempted"
        assert_error("finish_journal_attempt_exists", attempt, journal)


def test_failed_confirmation_keeps_original_attempt(tmp_path, monkeypatch):
    with FinishJournal(tmp_path) as journal:
        original = attempt(journal)
        monkeypatch.setattr(journal_module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
        assert_error("finish_journal_write_failed", journal.confirm)
        assert journal.read() == original


def test_persisted_record_only_contains_allowed_binding_and_outcome(tmp_path):
    with FinishJournal(tmp_path) as journal:
        record = attempt(journal)
        assert set(record) == {"version", "user_id", "campaign_id", "campaign_name",
                               "drop_id", "attempted_at", "outcome"}
        serialized = journal.journal_path.read_text(encoding="utf-8")
        assert not any(key in serialized.lower() for key in (
            "oauth", "cookie", "password", "dropinstanceid", "claim_id",
        ))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not represent Windows ACLs")
def test_state_and_files_are_private_on_linux(tmp_path):
    state = tmp_path / "finish-drop"
    with FinishJournal(state) as journal:
        attempt(journal)
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE(journal.lock_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(journal.journal_path.stat().st_mode) == 0o600
