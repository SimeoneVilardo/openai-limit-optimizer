"""Daily reset-target scheduling and its durable runtime override.

The schedule is deliberately independent from :mod:`app.state`: runtime
commands take only this file's short-lived lock and never hold or mutate the
send journal.  A reset target at local time ``R`` has an activation at
``R - 18000`` real UTC seconds.  The fixed 60 second lateness grace is a
bounded best-effort window; a target is never activated early.

The module contains no Codex or network code.  ``plan_next`` is pure apart
from timezone database lookup and is intended to be the scheduler's testable
semantic boundary.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import fcntl
import json
import math
import os
import re
import stat as _stat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FULL_WINDOW_SECONDS = 18_000
LATENESS_GRACE_SECONDS = 60
SCHEDULE_VERSION = 1
MAX_SEARCH_DAYS = 4  # cooldown is configuration-bounded to at most 86400s
TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class ScheduleError(RuntimeError):
    """Base error for invalid, unavailable, or unwritable schedules."""


class ScheduleCorruptError(ScheduleError):
    """An existing override is malformed and must not fall back to env."""


class ScheduleLockedError(ScheduleError):
    """Another short-lived runtime schedule operation owns the flock."""


@dataclasses.dataclass(frozen=True)
class Schedule:
    times: tuple[str, ...]
    timezone: str
    source: str = "env"

    def __post_init__(self):
        # Keep direct construction safe for callers/tests as well as for the
        # config and JSON parsers.
        object.__setattr__(self, "times", normalize_times(self.times))
        object.__setattr__(self, "timezone", validate_timezone(self.timezone))
        if self.source not in ("env", "override"):
            raise ValueError("unknown schedule source")


@dataclasses.dataclass(frozen=True)
class Target:
    """One local daily occurrence and its UTC activation."""

    date: _dt.date
    time: str
    reset_epoch: int
    activation_epoch: int

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "time": self.time,
            "reset_epoch": self.reset_epoch,
            "reset_utc": _dt.datetime.fromtimestamp(
                self.reset_epoch, _dt.timezone.utc
            ).isoformat(),
            "activation_epoch": self.activation_epoch,
            "activation_utc": _dt.datetime.fromtimestamp(
                self.activation_epoch, _dt.timezone.utc
            ).isoformat(),
        }


@dataclasses.dataclass(frozen=True)
class Plan:
    """Scheduler decision for the earliest reachable target.

    ``allowed`` means a normal send is allowed now (either because the target
    is due or because the send can finish its full cooldown before the next
    activation).  ``confirmation_due`` asks the daemon to start the two-read
    policy session at approximately ``activation-confirm_seconds``.  The
    first rate-limit read is still held until activation minus the policy's
    five-second tolerance, preserving the existing strict policy checks.
    """

    schedule: Schedule
    target: Target | None
    allowed: bool
    due: bool
    confirmation_due: bool
    reason: str
    ready_epoch: float | None = None
    wake_epoch: float | None = None

    @property
    def target_epoch(self) -> int | None:
        return None if self.target is None else self.target.reset_epoch

    @property
    def activation_epoch(self) -> int | None:
        return None if self.target is None else self.target.activation_epoch

    def as_dict(self) -> dict:
        return {
            "enabled": bool(self.schedule.times),
            "allowed": self.allowed,
            "due": self.due,
            "confirmation_due": self.confirmation_due,
            "reason": self.reason,
            "target": None if self.target is None else self.target.as_dict(),
            "ready_epoch": self.ready_epoch,
            "wake_epoch": self.wake_epoch,
        }


def normalize_times(values) -> tuple[str, ...]:
    """Validate, sort, and deduplicate strict ``HH:MM`` values."""
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("reset times must be a sequence")
    result = []
    for raw in values:
        if not isinstance(raw, str) or not TIME_RE.fullmatch(raw):
            raise ValueError("reset times must use strict HH:MM")
        result.append(raw)
    return tuple(sorted(set(result)))


def parse_reset_times(raw: str) -> tuple[str, ...]:
    """Parse the env comma list; only the completely empty value disables."""
    if not isinstance(raw, str):
        raise ValueError("reset times must be a string")
    if raw.strip() == "":
        return ()
    # Spaces around comma-separated env entries are convenient, but the
    # actual value remains strict once surrounding whitespace is removed.
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError("reset times contain an empty value")
    return normalize_times(parts)


def validate_timezone(name: str) -> str:
    """Require a usable IANA zone key from the installed tz database."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone must be a non-empty IANA zone")
    name = name.strip()
    # ZoneInfo accepts absolute paths on some Python/platform combinations;
    # those are not IANA names and would defeat config path boundaries.
    if name.startswith("/") or "\x00" in name or ".." in name:
        raise ValueError("timezone must be an IANA zone name")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown IANA timezone: {name}")
    return name


def _local_epoch(local: _dt.datetime, zone: ZoneInfo) -> int | None:
    """Map a local wall occurrence to its first valid UTC instant.

    Round-tripping both folds detects nonexistent DST wall times.  For an
    ambiguous time, the smaller epoch is the first occurrence and is selected
    exactly once for that local date.
    """
    naive = local.replace(tzinfo=None)
    candidates = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        epoch = int(aware.timestamp())
        roundtrip = _dt.datetime.fromtimestamp(epoch, zone).replace(tzinfo=None)
        if roundtrip == naive:
            candidates.append(epoch)
    if not candidates:
        return None
    return min(candidates)


def local_occurrence(day: _dt.date, value: str, timezone: str) -> int | None:
    """Return a valid occurrence epoch, or ``None`` for a DST gap."""
    if not TIME_RE.fullmatch(value):
        raise ValueError("reset time must use strict HH:MM")
    zone = ZoneInfo(validate_timezone(timezone))
    hour, minute = (int(x) for x in value.split(":"))
    local = _dt.datetime.combine(day, _dt.time(hour, minute))
    return _local_epoch(local, zone)


def _targets(schedule: Schedule, now: float, days: int = MAX_SEARCH_DAYS):
    zone = ZoneInfo(schedule.timezone)
    local_now = _dt.datetime.fromtimestamp(now, zone)
    # One day before now is useful around unusual offset changes. The bounded
    # lookahead covers the configuration maximum cooldown and DST day shifts;
    # plan_next stops consuming dates as soon as a reachable target is found.
    start = local_now.date() - _dt.timedelta(days=1)
    for offset in range(days + 2):
        day = start + _dt.timedelta(days=offset)
        daily = []
        for value in schedule.times:
            reset = _local_epoch(
                _dt.datetime.combine(day, _dt.time.fromisoformat(value)), zone
            )
            if reset is None:
                # Nonexistent local target: skip only this occurrence.
                continue
            daily.append(Target(day, value, reset, reset - FULL_WINDOW_SECONDS))
        # Normally local-date order is UTC order; sorting within the date also
        # handles unusual offset transitions without materializing a year.
        yield from sorted(
            daily,
            key=lambda target: (target.activation_epoch, target.reset_epoch,
                                target.time),
        )


def plan_next(
    schedule: Schedule,
    now: float,
    cooldown_remaining: float = 0,
    cooldown_seconds: int = FULL_WINDOW_SECONDS,
    confirm_seconds: int = 30,
) -> Plan:
    """Choose the earliest reachable activation using only real UTC seconds.

    A target whose cooldown cannot end by ``activation + 60`` is skipped and
    the next daily target is considered.  Before activation, an intermediate
    send is permitted only when ``now + cooldown_seconds <= activation``.
    """
    if not isinstance(schedule, Schedule):
        raise ValueError("schedule must be a Schedule")
    if not isinstance(now, (int, float)) or isinstance(now, bool):
        raise ValueError("now must be numeric")
    if not math.isfinite(float(now)):
        raise ValueError("now must be finite")
    if not isinstance(cooldown_remaining, (int, float)) or isinstance(
        cooldown_remaining, bool
    ) or cooldown_remaining < 0:
        raise ValueError("cooldown_remaining must be non-negative")
    if cooldown_seconds < 0 or confirm_seconds < 0:
        raise ValueError("durations must be non-negative")
    if not schedule.times:
        return Plan(schedule, None, True, False, False, "disabled")

    cooldown_end = now + cooldown_remaining
    for target in _targets(schedule, float(now)):
        activation = float(target.activation_epoch)
        grace_end = activation + LATENESS_GRACE_SECONDS
        if cooldown_end > grace_end:
            # This occurrence cannot be reached without violating cooldown.
            continue
        if now > grace_end:
            continue

        ready = max(activation, cooldown_end)
        due = now >= ready and now <= grace_end
        confirmation_start = activation - confirm_seconds
        confirmation_due = (
            now >= confirmation_start and now <= grace_end and cooldown_end <= grace_end
        )
        if due:
            return Plan(
                schedule,
                target,
                True,
                True,
                confirmation_due,
                "target-due",
                ready,
                now,
            )

        # The target is still ahead.  It may be too close for a normal send,
        # but the daemon must wake for policy confirmation near activation.
        standard_allowed = now + cooldown_seconds <= activation
        if now < activation:
            return Plan(
                schedule,
                target,
                standard_allowed,
                False,
                confirmation_due,
                "before-target" if standard_allowed else "waiting-for-target",
                ready,
                max(now, confirmation_start),
            )

        # Here now is in the short gap between activation and readiness (the
        # only possible case is a live cooldown); keep waiting for cooldown.
        return Plan(
            schedule,
            target,
            False,
            False,
            confirmation_due,
            "waiting-cooldown",
            ready,
            ready,
        )

    # With a daily schedule this is only reachable for a pathological tz
    # database/range, but fail closed rather than inventing an activation.
    return Plan(schedule, None, False, False, False, "no-reachable-target")


# Short aliases make the pure boundary easy to discover for callers and
# downstream self-checks without exposing implementation details.
parse_times = parse_reset_times
next_plan = plan_next


def _lock_path(path: str) -> str:
    return path + ".lock"


def _tmp_path(path: str) -> str:
    return path + ".tmp"


def _secure_mkdir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError:
            raise ScheduleError("schedule directory unavailable")


@contextlib.contextmanager
def _schedule_lock(path: str):
    lock = _lock_path(path)
    _secure_mkdir(lock)
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock, flags, 0o600)
    except OSError:
        raise ScheduleError("schedule lock unavailable")
    try:
        os.chmod(lock, 0o600)
    except OSError:
        pass
    try:
        fh = os.fdopen(fd, "a+")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise ScheduleError("schedule lock unavailable")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            raise ScheduleLockedError("schedule locked")
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _parse_override(data) -> Schedule:
    if not isinstance(data, dict) or set(data) != {"version", "times", "timezone"}:
        raise ScheduleCorruptError("schedule override schema")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEDULE_VERSION:
        raise ScheduleCorruptError("schedule override version")
    times = data.get("times")
    if not isinstance(times, list):
        raise ScheduleCorruptError("schedule override times")
    try:
        normalized = normalize_times(times)
        timezone = validate_timezone(data.get("timezone"))
        return Schedule(normalized, timezone, "override")
    except (ValueError, TypeError):
        raise ScheduleCorruptError("schedule override values")


class ScheduleStore:
    """Durable override store with independent, short-lived locking."""

    def __init__(self, path: str):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ScheduleError("schedule path must be absolute")
        self.path = os.path.normpath(path)

    def _read_unlocked(self) -> Schedule | None:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError:
            raise ScheduleCorruptError("schedule override unreadable")
        if not _stat.S_ISREG(info.st_mode):
            raise ScheduleCorruptError("schedule override is not a file")
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            raise ScheduleCorruptError("schedule override unreadable")
        return _parse_override(data)

    def override(self) -> Schedule | None:
        with _schedule_lock(self.path):
            return self._read_unlocked()

    def effective(self, env_times, env_timezone: str) -> Schedule:
        """Load override or validated env schedule; never silently fallback."""
        override = self.override()
        if override is not None:
            return override
        try:
            parsed_times = (
                normalize_times(env_times)
                if isinstance(env_times, (list, tuple, set, frozenset))
                else parse_reset_times(env_times)
            )
            return Schedule(parsed_times, validate_timezone(env_timezone), "env")
        except (ValueError, TypeError):
            # Config normally catches this before daemon startup; retaining a
            # typed error here keeps runtime reload fail-closed too.
            raise ScheduleError("environment schedule invalid")

    @staticmethod
    def _payload(times, timezone) -> dict:
        try:
            normalized = normalize_times(times)
            zone = validate_timezone(timezone)
        except (ValueError, TypeError):
            raise ScheduleError("invalid schedule values")
        return {"version": SCHEDULE_VERSION, "times": list(normalized), "timezone": zone}

    def write(self, times, timezone: str) -> Schedule:
        payload = self._payload(times, timezone)
        _secure_mkdir(self.path)
        with _schedule_lock(self.path):
            directory = os.path.dirname(self.path) or "."
            tmp = _tmp_path(self.path)
            try:
                flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(tmp, flags, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, self.path)
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
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise ScheduleError("schedule write failed")
        return Schedule(tuple(payload["times"]), payload["timezone"], "override")

    def reset(self) -> None:
        """Remove the override atomically under only the schedule flock."""
        with _schedule_lock(self.path):
            try:
                os.remove(self.path)
            except FileNotFoundError:
                return
            except OSError:
                raise ScheduleError("schedule reset failed")
            directory = os.path.dirname(self.path) or "."
            try:
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                raise ScheduleError("schedule reset failed")
