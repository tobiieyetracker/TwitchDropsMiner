"""A locked, durable record of one bounded drop-claim attempt.

The journal contains account/drop binding and outcome only. Credentials and the
server's raw claim instance ID must never be passed to this module.
"""
from __future__ import annotations

import errno
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from watch_check_state import WatchCheckError


_FIELDS = {
    "version", "user_id", "campaign_id", "campaign_name", "drop_id",
    "attempted_at", "outcome",
}
_MAX_BYTES = 16 * 1024


def _invalid() -> WatchCheckError:
    return WatchCheckError("finish_journal_invalid")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _validate(record: object) -> dict:
    if type(record) is not dict or set(record) != _FIELDS:
        raise _invalid()
    if type(record["version"]) is not int or record["version"] != 1:
        raise _invalid()
    user_id = record["user_id"]
    if type(user_id) is not str or not re.fullmatch(r"[1-9][0-9]{0,31}", user_id):
        raise _invalid()
    for key in ("campaign_id", "campaign_name", "drop_id"):
        value = record[key]
        if (type(value) is not str or not value.strip() or len(value) > 1024
                or any(ord(character) < 32 for character in value)):
            raise _invalid()
    if record["outcome"] not in ("attempted", "confirmed"):
        raise _invalid()
    timestamp = record["attempted_at"]
    if type(timestamp) is not str:
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid() from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _invalid()
    return dict(record)


class FinishJournal:
    """Hold a process lock while reading or changing one claim-attempt journal.

    The stable lock file is never unlinked: replacing it while another process
    holds a lock would allow two independent locks for the same journal.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser()
        self.journal_path = self.state_dir / "journal.json"
        self.lock_path = self.state_dir / "finish-drop.lock"
        self._lock_fd: int | None = None

    def __enter__(self):
        if self._lock_fd is not None:
            raise WatchCheckError("finish_journal_already_open")
        fd = None
        locking = False
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.state_dir, 0o700)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                locking = True
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                os.fchmod(fd, 0o600)
                locking = True
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return self
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            code = "finish_journal_locked" if locking and exc.errno in (
                errno.EACCES, errno.EAGAIN, errno.EDEADLK,
            ) else "finish_journal_open_failed"
            raise WatchCheckError(code) from None

    def __exit__(self, exc_type, exc, traceback):
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return False

    def _require_lock(self):
        if self._lock_fd is None:
            raise WatchCheckError("finish_journal_not_locked")

    def read(self) -> dict | None:
        return self._read_at(self.journal_path)

    def _read_at(self, path: Path) -> dict | None:
        self._require_lock()
        try:
            with path.open("rb") as source:
                content = source.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise WatchCheckError("finish_journal_read_failed") from None
        if len(content) > _MAX_BYTES:
            raise _invalid()
        try:
            return _validate(json.loads(content, object_pairs_hook=_object))
        except (ValueError, UnicodeError, TypeError):
            raise _invalid() from None

    def _sync_directory(self):
        if os.name != "nt":
            fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _write(self, record: dict):
        self._require_lock()
        record = _validate(record)
        content = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(content) > _MAX_BYTES:
            raise _invalid()
        temp_path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".journal-", suffix=".tmp", dir=self.state_dir)
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
            raise WatchCheckError("finish_journal_write_failed") from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def record_attempt(self, user_id, campaign_id: str, campaign_name: str, drop_id: str) -> dict:
        self._require_lock()
        if self.read() is not None:
            raise WatchCheckError("finish_journal_attempt_exists")
        if type(user_id) is int and user_id > 0:
            user_id = str(user_id)
        record = _validate({
            "version": 1,
            "user_id": user_id,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "drop_id": drop_id,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "attempted",
        })
        self._write(record)
        return dict(record)

    def confirm(self) -> dict:
        self._require_lock()
        record = self.read()
        if record is None:
            raise WatchCheckError("finish_journal_attempt_missing")
        if record["outcome"] != "confirmed":
            record["outcome"] = "confirmed"
            self._write(record)
        return dict(record)
