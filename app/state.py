"""Persistent cooldown journal: flock + atomic fsynced writes.

The journal records every send ATTEMPT (including failures, timeouts and
unknown outcomes) BEFORE the attempt starts, so a crash/restart can never
produce a second send inside the conservative 5h cooldown.

- interprocess ``flock`` (non-blocking) on ``<state>.lock`` serialises
  login/check/daemon on the same state file; contention => fail closed.
  The daemon and login/check commands hold this lock for their whole
  lifetime (so stop the daemon before running login/check);
- writes are atomic (tmp + fsync + rename + dir fsync) with 0600 file
  permissions; parent directories are created 0700 when absent;
- strict schema: only a fully-shaped record is accepted. An existing
  file that is empty (``{}``), outcome-only, or carries mistyped fields
  (e.g. a boolean timestamp) is corruption => raise, never overwrite
  (fail closed). ONLY an absent file means "uninitialized".
  The outcome vocabulary is open (any non-empty string: sent, failed,
  send-timeout, ...) because every recorded attempt — whatever its
  outcome — extends the cooldown; the heartbeat treats any outcome
  other than ``sent`` as still-degraded.
"""

from __future__ import annotations

import fcntl
import json
import os
import time

class StateError(RuntimeError):
    pass


class LockedError(StateError):
    pass


class CorruptedError(StateError):
    pass


def _lock_path(state_file: str) -> str:
    return state_file + ".lock"


def _secure_mkdir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)


class Journal:
    def __init__(self, path: str, cooldown: int = 18000):
        self.path = path
        self.cooldown = cooldown
        self._lockfh = None

    def acquire(self):
        _secure_mkdir(_lock_path(self.path))
        try:
            fd = os.open(_lock_path(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            raise StateError("cannot open lock")
        try:
            fh = os.fdopen(fd, "a+")
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise StateError("cannot open lock")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            try:
                fh.close()
            except Exception:
                pass
            raise LockedError("state locked by another command")
        self._lockfh = fh
        return self

    def release(self):
        fh, self._lockfh = self._lockfh, None
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    fh.close()
                except Exception:
                    pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            raise CorruptedError("state unreadable")
        if not isinstance(data, dict):
            raise CorruptedError("state not an object")
        if not data:
            raise CorruptedError("state empty")
        last = data.get("last_attempt_wall")
        if not isinstance(last, int) or isinstance(last, bool):
            raise CorruptedError("state last_attempt_wall malformed")
        mono = data.get("last_attempt_mono")
        if ((mono is not None and not isinstance(mono, (int, float)))
                or isinstance(mono, bool)):
            raise CorruptedError("state last_attempt_mono malformed")
        outcome = data.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise CorruptedError("state outcome malformed")
        detail = data.get("detail", "")
        if not isinstance(detail, str):
            raise CorruptedError("state detail malformed")
        return data

    def _write(self, data: dict) -> None:
        _secure_mkdir(self.path)
        tmp = self.path + ".tmp"
        directory = os.path.dirname(self.path) or "."
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        except OSError:
            raise StateError("state write failed")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(self.path if os.path.exists(self.path) else tmp, 0o600)
            except OSError:
                pass
            os.rename(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            raise StateError("state write failed")

    def cooldown_remaining(self, now: int) -> int:
        """Seconds left in cooldown, 0 when a new attempt is allowed."""
        data = self._read()
        last = data.get("last_attempt_wall")
        if not isinstance(last, int):
            return 0
        return max(0, (last + self.cooldown) - now)

    def last_outcome(self) -> str | None:
        """Most recent recorded outcome, None when never attempted."""
        data = self._read()
        outcome = data.get("outcome")
        return outcome if isinstance(outcome, str) and outcome else None

    def record_attempt_before(self, now: int, mono: float | None = None) -> None:
        """Journal the attempt BEFORE sending. Must hold the lock."""
        if self._lockfh is None:
            raise StateError("journal lock not held")
        if mono is None:
            mono = time.monotonic()
        if isinstance(mono, bool) or not isinstance(mono, (int, float)):
            raise StateError("bad mono timestamp")
        self._write({"last_attempt_wall": now, "last_attempt_mono": mono,
                     "outcome": "pending", "detail": ""})

    def record_outcome(self, outcome: str, detail: str = "") -> None:
        """Update outcome after the attempt. Must hold the lock.

        Any non-empty outcome string is accepted (sent, failed,
        send-timeout, ...): every recorded attempt extends the cooldown.
        """
        if self._lockfh is None:
            raise StateError("journal lock not held")
        if not isinstance(outcome, str) or not outcome:
            raise StateError("unknown outcome")
        data = self._read()
        data["outcome"] = outcome
        data["detail"] = detail[:120] if isinstance(detail, str) else ""
        self._write(data)
