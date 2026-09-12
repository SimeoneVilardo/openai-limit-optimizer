"""Fail-closed send policy (pure function, no I/O).

Conservative moving-full-window heuristic — NOT a documented explicit
inactive guarantee from the backend. We act only when ALL hold:

- ``ordinaryUsageAllowed`` is exactly True in both snapshots;
- the authoritative ``rateLimitsByLimitId`` map (when present) contains
  the ``codex`` bucket; missing key => reject. Without a map, the
  fallback ``rateLimits`` bucket must carry ``limitId`` absent/``codex``
  (any other bucket => reject), and a ``codex`` map entry whose own
  ``limitId`` conflicts => malformed;
- codex primary is exactly ``windowDurationMins == 300``,
  ``usedPercent == 0`` with a valid future ``resetsAt``;
- BOTH snapshots are "full windows": ``resetsAt - wall`` within
  ``18000 +/- 5s`` (fresh window, not mid-window 0%);
- the two reads are separated by >= ``confirm_seconds`` and ``resetsAt``
  MOVES with elapsed wall time (|dResets - dWall| <= 5s); a fixed
  timestamp (frozen countdown target) => SKIP;
- wall/monotonic clocks agree (|dWall - dMono| <= 6s) else clock-jump SKIP;
- no weekly/other exhaustion, no spend-control reached, no reached-type.

Strict field validation: every supplied optional field must have its
documented shape (bools are bools, percents are non-negative ints,
timestamps are valid epoch ints). Anything malformed — including a
string ``"true"``, a boolean percent, or an out-of-range timestamp —
raises ``PolicyError`` (=> no send, visible ``schema-rejected``).

Rounding caveat: the protocol carries ``usedPercent`` as rounded ``i32``
(``0.4%`` -> ``0``). An exact ``0`` therefore cannot prove true zero
usage; the moving-full-window + 5h cooldown only make accidental sends
conservative, never certain. Unknown shapes => no action with a reason.
"""

from __future__ import annotations

import dataclasses

TOLERANCE = 5
WINDOW_MINUTES = 300
FULL_WINDOW = 18000
MAX_EPOCH = 2 ** 40


@dataclasses.dataclass(frozen=True)
class Window:
    used: int
    duration: int | None
    resets_at: int | None


@dataclasses.dataclass(frozen=True)
class Bucket:
    primary: Window | None
    secondary: Window | None
    spend_reached: bool | None
    indiv_remaining: int | None
    reached_type: str | None
    plan: str | None


@dataclasses.dataclass(frozen=True)
class Snapshot:
    ordinary: bool | None
    codex: Bucket | None
    others: tuple


class PolicyError(ValueError):
    pass


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _opt_str(raw: dict, key: str, ctx: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise PolicyError(f"{ctx}.{key} malformed")
    return v


def _opt_bool(raw: dict, key: str, ctx: str) -> bool | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise PolicyError(f"{ctx}.{key} malformed")
    return v


def _window(raw, ctx: str) -> Window | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PolicyError(f"{ctx} malformed")
    used = raw.get("usedPercent")
    if not _is_int(used) or used < 0:
        raise PolicyError(f"{ctx}.usedPercent malformed")
    dur = raw.get("windowDurationMins")
    if dur is not None and (not _is_int(dur) or dur < 0):
        raise PolicyError(f"{ctx}.windowDurationMins malformed")
    rst = raw.get("resetsAt")
    if rst is not None and (not _is_int(rst) or not (0 <= rst <= MAX_EPOCH)):
        raise PolicyError(f"{ctx}.resetsAt malformed")
    return Window(used=used, duration=dur, resets_at=rst)


def _bucket(raw, ctx: str) -> Bucket:
    if not isinstance(raw, dict):
        raise PolicyError(f"{ctx} malformed")
    limit_id = _opt_str(raw, "limitId", ctx)
    if ctx == "codex" and limit_id is not None and limit_id != "codex":
        raise PolicyError("conflicting-limit-id")
    indiv = None
    il = raw.get("individualLimit")
    if il is not None:
        if not isinstance(il, dict):
            raise PolicyError(f"{ctx}.individualLimit malformed")
        rem = il.get("remainingPercent")
        if not _is_int(rem) or rem < 0:
            raise PolicyError(f"{ctx}.individualLimit.remainingPercent malformed")
        indiv = rem
        for k in ("limit", "used"):
            v = il.get(k)
            if v is not None and not isinstance(v, str):
                raise PolicyError(f"{ctx}.individualLimit.{k} malformed")
        rst = il.get("resetAt", il.get("resetsAt"))
        if rst is not None and (not _is_int(rst) or not (0 <= rst <= MAX_EPOCH)):
            raise PolicyError(f"{ctx}.individualLimit.reset malformed")
    return Bucket(
        primary=_window(raw.get("primary"), f"{ctx}.primary"),
        secondary=_window(raw.get("secondary"), f"{ctx}.secondary"),
        spend_reached=_opt_bool(raw, "spendControlReached", ctx),
        indiv_remaining=indiv,
        reached_type=_opt_str(raw, "rateLimitReachedType", ctx),
        plan=_opt_str(raw, "planType", ctx),
    )


def parse(result) -> Snapshot:
    """Parse a raw account/rateLimits/read result. Raises PolicyError."""
    if not isinstance(result, dict):
        raise PolicyError("payload not an object")
    ordinary = result.get("ordinaryUsageAllowed")
    if ordinary is not None and not isinstance(ordinary, bool):
        raise PolicyError("ordinaryUsageAllowed malformed")
    by_id = result.get("rateLimitsByLimitId")
    top = result.get("rateLimits")
    if not isinstance(top, dict):
        raise PolicyError("rateLimits missing")
    if by_id is None:
        top_id = top.get("limitId")
        if top_id is not None and top_id != "codex":
            # Fallback bucket belongs to another meter: not ours.
            return Snapshot(ordinary=ordinary, codex=None, others=((top_id, None),))
        return Snapshot(ordinary=ordinary, codex=_bucket(top, "codex"), others=())
    if not isinstance(by_id, dict):
        raise PolicyError("rateLimitsByLimitId malformed")
    if "codex" not in by_id:
        return Snapshot(ordinary=ordinary, codex=None,
                        others=tuple(sorted(by_id.keys())))
    codex_raw = by_id["codex"]
    parsed_others = []
    for k in sorted(by_id.keys()):
        if k == "codex":
            continue
        v = by_id[k]
        if not isinstance(v, dict):
            raise PolicyError("other bucket malformed")
        parsed_others.append((k, _bucket(v, f"other.{k}")))
    return Snapshot(ordinary=ordinary, codex=_bucket(codex_raw, "codex"),
                    others=tuple(parsed_others))


def _full(win: Window, wall: int):
    """Check one window is a fresh full 5h window. Returns reason or None."""
    if win is None:
        return "primary-missing"
    if win.duration != WINDOW_MINUTES:
        return "window-not-300"
    if win.used != 0:
        return "used-nonzero"
    if win.resets_at is None:
        return "resets-null"
    rem = win.resets_at - wall
    if rem <= 0:
        return "resets-expired"
    if rem > FULL_WINDOW + TOLERANCE:
        return "resets-beyond-5h"
    if abs(rem - FULL_WINDOW) > TOLERANCE:
        return "window-not-full"
    return None


def _exhausted(b: Bucket) -> bool:
    for w in (b.primary, b.secondary):
        if w is not None and w.used >= 100:
            return True
    if b.spend_reached is True:
        return True
    if b.indiv_remaining is not None and b.indiv_remaining <= 0:
        return True
    if b.reached_type is not None:
        return True
    return False


def _blocking_extras(snap: Snapshot):
    b = snap.codex
    if b is None:
        return "codex-bucket-missing"
    sec = b.secondary
    if sec is not None and sec.used >= 100:
        return "weekly-exhausted"
    if b.spend_reached is True:
        return "spend-reached"
    if b.indiv_remaining is not None and b.indiv_remaining <= 0:
        return "spend-reached"
    if b.reached_type is not None:
        return "reached-type"
    for _key, ob in snap.others:
        if ob is not None and _exhausted(ob):
            return "other-limit-exhausted"
    return None


def decide(snap1: Snapshot, wall1: int, snap2: Snapshot, wall2: int,
           mono1: float, mono2: float, confirm: int):
    """Return (allow: bool, reason: str). Fail closed on every ambiguity."""
    for s in (snap1, snap2):
        if s.ordinary is not True:
            return False, "ordinary-not-true"
    d_wall = wall2 - wall1
    d_mono = mono2 - mono1
    if abs(d_wall - d_mono) > TOLERANCE + 1:
        return False, "clock-jump"
    if d_wall < confirm or d_mono < confirm - TOLERANCE:
        return False, "separation-too-short"
    if snap1.codex is None or snap2.codex is None:
        return False, "codex-bucket-missing"
    p1, p2 = snap1.codex.primary, snap2.codex.primary
    if p1 is None or p2 is None:
        return False, "primary-missing"
    if p1.resets_at is not None and p2.resets_at is not None:
        if abs((p2.resets_at - p1.resets_at) - d_wall) > TOLERANCE:
            return False, "resets-fixed"
    r1 = _full(snap1.codex.primary, wall1)
    if r1:
        return False, r1
    r2 = _full(snap2.codex.primary, wall2)
    if r2:
        return False, r2
    for s in (snap1, snap2):
        blk = _blocking_extras(s)
        if blk:
            return False, blk
    return True, "allow"
