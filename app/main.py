"""Daemon entrypoint: poll Codex usage, send only on moving-full-window.

Commands:
  daemon [--once] [--dry-run]   poll loop (single pass with --once);
                                --dry-run never sends (logs would-send).
                                Holds the state lock for its whole
                                lifetime: stop the daemon before running
                                login/check against the same state file.
  check [--dry-run]             one poll + decision to stdout, no send,
                                no state writes. Holds the state lock for
                                the whole check (including RPC), so it
                                never runs concurrently with the daemon.
  login                         device auth flow
                                (``codex login --device-auth`` with the
                                file credential store forced) against the
                                persistent CODEX_HOME. Never copies
                                desktop auth. Holds the state lock for the
                                whole flow.
  healthcheck                   exit 0 healthy loop, 1 degraded/blocked,
                                2 stale/corrupt. Distinguishes a healthy
                                polling loop from auth-blocked or failing
                                polling via the heartbeat file.
  schedule show|set|clear|reset
                                 inspect or change the daily reset target;
                                 these commands use only the schedule lock.

Auth missing => daemon keeps polling and logs a sanitized actionable
reason (heartbeat ``blocked_auth``); it never crashes the loop.
A failed attempt (or a crash mid-attempt) keeps the heartbeat degraded
through the cooldown instead of flipping back to healthy.
``--once``/``check`` exit nonzero on degraded/error dispositions so
failures stay visible to supervisors.
All logs are structured single-line JSON with no payload/token/account
data and no prompt text.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import config as cfgmod
from . import policy as policymod
from . import protocol as protomod
from . import schedule as schedulemod
from . import send as sendmod
from . import state as statemod

APP_VERSION = "0.1.0"
STOP = threading.Event()

LOGIN_ARGV_TAIL = ["login", "--device-auth", "-c",
                   'cli_auth_credentials_store="file"']


def _on_term(signum, frame):
    STOP.set()


def reset_stop() -> None:
    STOP.clear()


def log(level: str, event: str, **fields) -> None:
    rec = {"ts": int(time.time()), "level": level, "event": event}
    for k in sorted(fields):
        v = fields[k]
        if isinstance(v, (str, int, float, bool)) or v is None:
            rec[k] = v
    sys.stdout.write(json.dumps(rec) + "\n")
    sys.stdout.flush()


def write_heartbeat(path: str, status: str, detail: str = "") -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {"status": status, "detail": detail[:120],
               "ts_wall": int(time.time())}
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.rename(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def poll_once(cfg, client: protomod.AppServerClient,
              observation: dict | None = None):
    """Two fresh reads separated by confirm_seconds. Returns decision tuple.

    Returns (allow, reason); an optional observation dict receives the final
    wall/monotonic read timestamps for post-RPC scheduling refresh.
    """
    acct = client.account_read()
    if not protomod.is_chatgpt_account(acct):
        raise protomod.AuthError("auth-required")
    client.ensure_model(cfg.model, cfg.effort)
    r1 = client.rate_limits_read()
    wall1, mono1 = int(time.time()), time.monotonic()
    s1 = policymod.parse(r1)
    deadline = mono1 + cfg.confirm_seconds
    while not STOP.is_set() and time.monotonic() < deadline:
        STOP.wait(min(1.0, max(0.0, deadline - time.monotonic())))
    if STOP.is_set():
        raise protomod.CancelledError("cancelled")
    r2 = client.rate_limits_read()
    wall2, mono2 = int(time.time()), time.monotonic()
    s2 = policymod.parse(r2)
    allow, reason = policymod.decide(s1, wall1, s2, wall2, mono1, mono2,
                                     cfg.confirm_seconds)
    if observation is not None:
        observation["wall1"] = wall1
        observation["wall2"] = wall2
        observation["mono1"] = mono1
        observation["mono2"] = mono2
    return allow, reason


def _cooldown_heartbeat(journal: statemod.Journal, now: int):
    """Heartbeat disposition while cooling down: only a past ``sent``
    reads healthy; failed/pending/unknown outcomes stay degraded so a
    failure is never hidden by the cooldown."""
    if journal.last_outcome() == "sent":
        return "healthy", "cooldown", 0
    return "degraded", "cooldown-after-failure", 1


def _load_schedule(cfg, store=None):
    return (store or schedulemod.ScheduleStore(cfg.schedule_file)).effective(
        cfg.reset_times, cfg.reset_timezone
    )


def _schedule_plan(cfg, current, now: float, remaining: float):
    return schedulemod.plan_next(
        current, now, remaining, cfg.cooldown_seconds, cfg.confirm_seconds
    )


def _schedule_error_heartbeat(cfg, detail="schedule-corrupt"):
    try:
        write_heartbeat(cfg.heartbeat_file, "error", detail)
    except OSError:
        pass


def _meaningful_heartbeat(path: str):
    """Return the last non-healthy disposition without touching its mtime."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        detail = payload.get("detail", "")
        if status not in ("degraded", "blocked_auth", "error"):
            return None
        if not isinstance(detail, str):
            detail = ""
        return status, detail
    except (OSError, ValueError):
        return None


_BOUNDARY_RETRY_REASONS = frozenset({
    # ``resets-fixed`` is the mixed snapshot case. The remaining reasons are
    # the existing policy's full-window gate; one fresh pair can recover a
    # moving boundary without broadening policy authority.
    "resets-fixed",
    "primary-missing",
    "window-not-300",
    "used-nonzero",
    "resets-null",
    "resets-expired",
    "resets-beyond-5h",
    "window-not-full",
})


def _boundary_retry(cfg, journal, store, client, schedule_state,
                    schedule_plan, reason):
    """Return one fresh policy result when a reset-boundary retry fits.

    The occurrence and effective runtime schedule are revalidated on every
    wait iteration. Eligibility requires the retry pair to fit inside the
    ``A-confirm``..``A+60`` estimate (cooldown end included in the start
    bound); RPC latency means this is an eligibility estimate, and the
    durable pre-send gates recheck cooldown/schedule before any attempt.
    ``None`` means no retry is safe or useful.
    """
    if reason not in _BOUNDARY_RETRY_REASONS:
        return None
    if not schedule_state.times or schedule_plan.target is None:
        return None
    if not (schedule_plan.confirmation_due or schedule_plan.due):
        # Intermediate standard-allowed attempts hours before activation must
        # return their skip after one pair, never block the client waiting
        # until A-confirm. Only an actual boundary confirmation/due denial
        # may consume the single extra pair.
        return None
    original_target = schedule_plan.target
    original_schedule = schedule_state
    while not STOP.is_set():
        now = time.time()
        latest_schedule = _load_schedule(cfg, store)
        remaining = journal.cooldown_remaining(int(now))
        latest_plan = _schedule_plan(cfg, latest_schedule, now, remaining)
        # A set/clear/reset, timezone change, or target reselection invalidates
        # the old denial. Never apply a retry to a different occurrence.
        if latest_schedule != original_schedule:
            return None
        if latest_plan.target != original_target:
            return None
        activation = float(original_target.activation_epoch)
        grace_end = activation + schedulemod.LATENESS_GRACE_SECONDS
        start = max(
            now,
            activation - cfg.confirm_seconds,
            now + remaining,
        )
        if start + cfg.confirm_seconds > grace_end:
            return None
        if now < start:
            _write_wait_heartbeat(cfg, journal, remaining,
                                  "waiting-boundary-retry")
            if STOP.wait(min(5.0, start - now)):
                return None
            continue
        log("info", "schedule-boundary-retry",
            activation=original_target.activation_epoch)
        # A second invocation is deliberately the only extra policy pair for
        # this occurrence. Its errors flow through _cycle's normal handlers.
        return poll_once(cfg, client)
    return None


def _cycle(cfg, journal: statemod.Journal, dry_run: bool,
           schedule_state=None, schedule_plan=None, schedule_store=None):
    """One poll cycle. Returns (disposition, exit_hint).

    disposition is one of sent/skip/cooldown/blocked_auth/degraded/error.
    A degraded cooldown (after a non-sent attempt) reports nonzero so
    supervisors and --once callers still see the failure.
    """
    try:
        protomod.ensure_codex_home(cfg.codex_home)
    except protomod.HomeError:
        log("error", "codex-home-unwritable")
        try:
            write_heartbeat(cfg.heartbeat_file, "error",
                            "codex-home-unwritable")
        except OSError:
            pass
        return "error", 2
    now = int(time.time())
    try:
        remaining = journal.cooldown_remaining(now)
    except statemod.CorruptedError:
        log("error", "state-corrupt")
        write_heartbeat(cfg.heartbeat_file, "error", "state-corrupt")
        return "error", 2
    try:
        fresh_schedule = _load_schedule(cfg, schedule_store)
        if schedule_state is None or fresh_schedule != schedule_state:
            schedule_state = fresh_schedule
            schedule_plan = None
        if schedule_state.times or schedule_plan is None:
            schedule_plan = _schedule_plan(cfg, schedule_state, now, remaining)
    except schedulemod.ScheduleError:
        log("error", "schedule-corrupt")
        _schedule_error_heartbeat(cfg)
        return "error", 2
    scheduled_confirmation = bool(
        schedule_state.times and schedule_plan.confirmation_due
    )
    if remaining > 0 and not scheduled_confirmation:
        status, detail, code = _cooldown_heartbeat(journal, now)
        log("info", "cooldown", remaining_s=remaining, status=status)
        write_heartbeat(cfg.heartbeat_file, status, detail)
        return "cooldown", code
    if schedule_state.times and not schedule_plan.allowed and not scheduled_confirmation:
        log("info", "schedule-wait", reason=schedule_plan.reason)
        _write_wait_heartbeat(cfg, journal, remaining, "waiting-schedule")
        return "scheduled-wait", 0
    try:
        with protomod.AppServerClient(cfg.codex_bin, cfg.codex_home,
                                      cfg.rpc_timeout,
                                      cancel_event=STOP) as client:
            # The client/session is opened at activation-confirm_seconds by
            # the daemon wake. For moving windows, the first read is valid at
            # that time; leave it immediate so confirm=600 can finish at A.
            allow, reason = poll_once(cfg, client)
            if not allow:
                retry_result = _boundary_retry(
                    cfg, journal, schedule_store, client, schedule_state,
                    schedule_plan, reason
                )
                if retry_result is not None:
                    allow, reason = retry_result
    except schedulemod.ScheduleError:
        log("error", "schedule-corrupt")
        _schedule_error_heartbeat(cfg)
        return "error", 2
    except protomod.AuthError:
        log("warning", "auth-missing",
            hint="run login command for device auth")
        if remaining > 0 and journal.last_outcome() != "sent":
            write_heartbeat(cfg.heartbeat_file, "degraded",
                            "cooldown-after-failure")
        else:
            write_heartbeat(cfg.heartbeat_file, "blocked_auth", "auth-missing")
        return "blocked_auth", 1
    except protomod.CancelledError:
        log("info", "cancelled")
        return "error", 2
    except protomod.ProtocolError:
        log("warning", "poll-failed")
        if remaining > 0:
            status, detail, _code = _cooldown_heartbeat(
                journal, int(time.time())
            )
            write_heartbeat(cfg.heartbeat_file, status, detail)
        else:
            write_heartbeat(cfg.heartbeat_file, "degraded", "poll-failed")
        return "degraded", 2
    except policymod.PolicyError:
        log("warning", "schema-rejected")
        if remaining > 0:
            status, detail, _code = _cooldown_heartbeat(
                journal, int(time.time())
            )
            write_heartbeat(cfg.heartbeat_file, status, detail)
        else:
            write_heartbeat(cfg.heartbeat_file, "degraded", "schema-rejected")
        return "degraded", 2
    if not allow:
        log("info", "skip", reason=reason)
        if remaining > 0:
            status, detail, _code = _cooldown_heartbeat(
                journal, int(time.time())
            )
            write_heartbeat(cfg.heartbeat_file, status, detail)
        else:
            write_heartbeat(cfg.heartbeat_file, "healthy", reason)
        return "skip", 0
    if journal.cooldown_remaining(int(time.time())) > 0:
        race_status, race_detail, race_code = _cooldown_heartbeat(
            journal, int(time.time())
        )
        log("info", "skip", reason="cooldown-raced", status=race_status)
        write_heartbeat(cfg.heartbeat_file, race_status, race_detail)
        return "cooldown", race_code
    # Runtime schedule edits are observed independently while the daemon is
    # running. Re-read immediately before the durable attempt record so a
    # stale pre-RPC scheduling decision can never authorize a send.
    try:
        latest_schedule = _load_schedule(cfg, schedule_store)
        if latest_schedule.times:
            latest_now = int(time.time())
            latest_remaining = journal.cooldown_remaining(latest_now)
            latest_plan = _schedule_plan(
                cfg, latest_schedule, latest_now, latest_remaining
            )
        else:
            # Preserve the legacy no-target call/timing path. The cooldown
            # race was already checked immediately above; an empty runtime
            # schedule cannot add a scheduling gate.
            latest_remaining = 0
            latest_plan = None
    except schedulemod.ScheduleError:
        log("error", "schedule-corrupt")
        _schedule_error_heartbeat(cfg)
        return "error", 2
    except statemod.CorruptedError:
        log("error", "state-corrupt")
        _schedule_error_heartbeat(cfg, "state-corrupt")
        return "error", 2
    if latest_remaining > 0:
        log("info", "skip", reason="cooldown-raced")
        write_heartbeat(cfg.heartbeat_file, "healthy", "cooldown-raced")
        return "cooldown", 0
    if latest_schedule.times and not latest_plan.allowed:
        log("info", "skip", reason="schedule-raced")
        write_heartbeat(cfg.heartbeat_file, "healthy", "schedule-raced")
        return "skip", 0
    if dry_run:
        log("info", "would-send", reason=reason)
        if remaining > 0:
            status, detail, _code = _cooldown_heartbeat(
                journal, int(time.time())
            )
            write_heartbeat(cfg.heartbeat_file, status, detail)
        else:
            write_heartbeat(cfg.heartbeat_file, "healthy", "would-send")
        return "skip", 0
    attempt_at = int(time.time())
    journal.record_attempt_before(attempt_at, time.monotonic())
    ok, detail = sendmod.run_send(cfg.codex_bin, cfg.codex_home, cfg.model,
                                  cfg.effort, cfg.prompt, cfg.send_timeout,
                                  cancel_event=STOP)
    journal.record_outcome("sent" if ok else "failed", detail)
    if ok:
        log("info", "send", outcome="sent", detail=detail)
        write_heartbeat(cfg.heartbeat_file, "healthy", detail)
        return "sent", 0
    log("error", "send", outcome="failed", detail=detail)
    write_heartbeat(cfg.heartbeat_file, "degraded", detail)
    return "degraded", 2


def _write_wait_heartbeat(cfg, journal, remaining,
                          detail="waiting-schedule"):
    try:
        meaningful = _meaningful_heartbeat(cfg.heartbeat_file)
        if meaningful is not None:
            status, wait_detail = meaningful
        elif remaining > 0:
            status, _detail, _code = _cooldown_heartbeat(
                journal, int(time.time())
            )
            wait_detail = _detail
        else:
            status = "healthy"
            wait_detail = detail
        # Waiting refreshes heartbeat freshness but is not itself a successful
        # backend probe. Preserve blocked/degraded/error dispositions and
        # their actionable detail until a real cycle updates them.
        write_heartbeat(cfg.heartbeat_file, status, wait_detail)
    except (OSError, statemod.StateError):
        pass


def _wait_for_daemon(cfg, store, journal, next_poll_at,
                     handled_activation, observed_schedule=None):
    """Wait with five-second runtime reloads and precise target wakes."""
    while not STOP.is_set():
        now = time.time()
        try:
            remaining = journal.cooldown_remaining(int(now))
            current = _load_schedule(cfg, store)
            plan = _schedule_plan(cfg, current, now, remaining)
        except schedulemod.ScheduleError:
            log("error", "schedule-corrupt")
            _schedule_error_heartbeat(cfg)
            if STOP.wait(5.0):
                return None
            continue
        if (observed_schedule is not None
                and current != observed_schedule):
            # A live set/clear/reset is actionable immediately, not merely at
            # the legacy poll deadline.
            return current, plan
        if now >= next_poll_at:
            return current, plan
        wake = plan.wake_epoch if current.times and plan.target else None
        if (wake is not None and wake <= now
                and plan.activation_epoch != handled_activation):
            return current, plan
        deadline = next_poll_at
        schedule_wake = False
        if wake is not None and now < wake and wake < deadline:
            deadline = wake
            schedule_wake = True
        wait = max(0.0, min(5.0, deadline - now))
        if current.times or remaining > 0:
            _write_wait_heartbeat(cfg, journal, remaining)
        if STOP.wait(wait):
            return None
        if schedule_wake and time.time() >= deadline:
            # Loop once more at the boundary so the file and cooldown are
            # authoritative immediately before the RPC cycle.
            continue
    return None


def cmd_daemon(cfg, once: bool, dry_run: bool) -> int:
    reset_stop()
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    log("info", "start", version=APP_VERSION, once=once, dry_run=dry_run,
        note="holds state lock; stop daemon before login/check")
    journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
    try:
        journal.acquire()
    except statemod.LockedError:
        log("error", "state-locked",
            hint="another daemon or command holds the lock")
        return 2
    except statemod.StateError:
        log("error", "state-error")
        return 2
    schedule_store = schedulemod.ScheduleStore(cfg.schedule_file)
    try:
        next_poll_at = None
        handled_activation = None
        current_schedule = None
        current_plan = None
        while not STOP.is_set():
            if next_poll_at is not None and time.time() < next_poll_at:
                try:
                    ready = _wait_for_daemon(
                        cfg, schedule_store, journal, next_poll_at,
                        handled_activation, current_schedule
                    )
                except statemod.StateError:
                    log("error", "state-error")
                    _schedule_error_heartbeat(cfg, "state-error")
                    return 2
                if ready is None:
                    break
                current_schedule, current_plan = ready
            else:
                try:
                    current_schedule = _load_schedule(cfg, schedule_store)
                    if current_schedule.times:
                        now = time.time()
                        remaining = journal.cooldown_remaining(int(now))
                        current_plan = _schedule_plan(
                            cfg, current_schedule, now, remaining
                        )
                    else:
                        # No reset target: retain the old immediate cycle
                        # semantics and only observe the override cheaply.
                        current_plan = None
                except schedulemod.ScheduleError:
                    log("error", "schedule-corrupt")
                    _schedule_error_heartbeat(cfg)
                    if once:
                        return 2
                    next_poll_at = time.time() + 5.0
                    continue
                except statemod.StateError:
                    log("error", "state-error")
                    _schedule_error_heartbeat(cfg, "state-error")
                    if once:
                        return 2
                    next_poll_at = time.time() + 5.0
                    continue
            cycle_start = time.monotonic()
            try:
                _disp, code = _cycle(
                    cfg, journal, dry_run, current_schedule, current_plan,
                    schedule_store
                )
            except statemod.StateError:
                log("error", "state-error")
                try:
                    write_heartbeat(cfg.heartbeat_file, "error", "state-error")
                except OSError:
                    pass
                return 2
            if once:
                return code
            elapsed = time.monotonic() - cycle_start
            next_poll_at = time.time() + max(0.0, cfg.poll_seconds - elapsed)
            if (_disp != "scheduled-wait" and current_schedule.times
                    and current_plan is not None
                    and current_plan.target is not None
                    and (current_plan.confirmation_due or current_plan.due)):
                handled_activation = current_plan.activation_epoch
    finally:
        journal.release()
    log("info", "shutdown")
    return 0


def cmd_check(cfg, dry_run: bool) -> int:
    del dry_run  # check never sends regardless
    reset_stop()
    journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
    try:
        journal.acquire()
    except statemod.LockedError:
        print(json.dumps({"ok": False, "reason": "state-locked"}))
        return 2
    except statemod.StateError:
        print(json.dumps({"ok": False, "reason": "state-error"}))
        return 2
    try:
        try:
            protomod.ensure_codex_home(cfg.codex_home)
        except protomod.HomeError:
            print(json.dumps({"ok": False, "reason": "codex-home-unwritable"}))
            return 2
        try:
            now = int(time.time())
            remaining = journal.cooldown_remaining(now)
        except statemod.CorruptedError:
            print(json.dumps({"ok": False, "reason": "state-corrupt"}))
            return 2
        except statemod.StateError:
            print(json.dumps({"ok": False, "reason": "state-error"}))
            return 2
        try:
            current_schedule = _load_schedule(cfg)
            current_plan = _schedule_plan(
                cfg, current_schedule, now, remaining
            )
        except schedulemod.ScheduleError:
            print(json.dumps({"ok": False, "reason": "schedule-corrupt"}))
            return 2
        try:
            with protomod.AppServerClient(cfg.codex_bin, cfg.codex_home,
                                          cfg.rpc_timeout,
                                          cancel_event=STOP) as client:
                observation = {}
                allow, reason = poll_once(cfg, client, observation=observation)
        except protomod.AuthError:
            # Fresh home, no login yet => auth-missing (never poll-failed).
            print(json.dumps({"ok": False, "reason": "auth-missing",
                              "cooldown_remaining_s": remaining}))
            return 1
        except protomod.CancelledError:
            print(json.dumps({"ok": False, "reason": "cancelled"}))
            return 2
        except (protomod.ProtocolError, policymod.PolicyError):
            print(json.dumps({"ok": False, "reason": "poll-failed",
                              "cooldown_remaining_s": remaining}))
            return 1
        # Keep the journal flock while refreshing both clocks/schedule after
        # the potentially long confirmation RPC. This prevents check from
        # reporting an obsolete effective_allow across a runtime edit or a
        # cooldown/activation boundary.
        try:
            refreshed_schedule = _load_schedule(cfg)
            probe_now = observation.get("wall2")
            if probe_now is None:
                if (current_schedule.times
                        or refreshed_schedule != current_schedule
                        or remaining > 0):
                    probe_now = int(time.time())
                else:
                    probe_now = now
            remaining = journal.cooldown_remaining(int(probe_now))
            now = int(probe_now)
            current_schedule = refreshed_schedule
            current_plan = _schedule_plan(
                cfg, current_schedule, now, remaining
            )
        except schedulemod.ScheduleError:
            print(json.dumps({"ok": False, "reason": "schedule-corrupt"}))
            return 2
        except statemod.StateError:
            print(json.dumps({"ok": False, "reason": "state-corrupt"}))
            return 2
    finally:
        journal.release()
    # No state writes here by design; the reported allow is gated by the
    # observed cooldown so check never claims an action the daemon would
    # refuse.
    plan_data = current_plan.as_dict()
    next_target = plan_data["target"]
    print(json.dumps({"ok": True, "allow": allow,
                      "effective_allow": bool(
                          allow and remaining == 0 and current_plan.allowed
                      ),
                      "reason": reason, "cooldown_remaining_s": remaining,
                      "schedule_times": list(current_schedule.times),
                      "schedule_timezone": current_schedule.timezone,
                      "schedule_source": current_schedule.source,
                      "schedule_allow": current_plan.allowed,
                      "schedule_reason": current_plan.reason,
                      "next_target": next_target,
                      "next_target_time": None if next_target is None else next_target["time"],
                      "next_activation": None if next_target is None else next_target["activation_utc"],
                      "next_activation_epoch": current_plan.activation_epoch,
                      "schedule": plan_data}))
    return 0


def cmd_login(cfg) -> int:
    reset_stop()
    journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
    try:
        journal.acquire()
    except statemod.LockedError:
        log("error", "state-locked",
            hint="stop the daemon before login")
        return 2
    except statemod.StateError:
        log("error", "state-error")
        return 2
    try:
        try:
            protomod.ensure_codex_home(cfg.codex_home)
        except protomod.HomeError:
            log("error", "codex-home-unwritable")
            return 2
        log("info", "login-start")
        try:
            proc = subprocess.run(
                [cfg.codex_bin] + LOGIN_ARGV_TAIL,
                env=protomod.child_env(cfg.codex_home))
        except OSError:
            log("error", "login-spawn-failed")
            return 2
        if proc.returncode != 0:
            log("error", "login-failed")
            return 1
        try:
            status = subprocess.run(
                [cfg.codex_bin, "login", "status"],
                env=protomod.child_env(cfg.codex_home))
        except OSError:
            log("error", "login-status-failed")
            return 2
        if status.returncode != 0:
            log("error", "login-unverified")
            return 1
        log("info", "login-done")
        return 0
    finally:
        journal.release()


def cmd_healthcheck(cfg) -> int:
    try:
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        print("heartbeat missing or corrupt")
        return 2
    try:
        if not isinstance(hb, dict):
            raise ValueError("not an object")
        status = hb.get("status")
        if status not in ("healthy", "degraded", "blocked_auth", "error"):
            raise ValueError("unknown status")
        ts = hb.get("ts_wall")
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise ValueError("bad timestamp")
        now = int(time.time())
        if ts > now + 300:
            print("heartbeat timestamp in the future")
            return 2
        age = now - ts
    except ValueError as exc:
        print(f"heartbeat malformed: {exc}")
        return 2
    if age > cfg.stale_after():
        print(f"stale heartbeat age={age}s status={status}")
        return 2
    if status == "healthy":
        print(f"healthy age={age}s")
        return 0
    print(f"{status} age={age}s")
    return 1


def _schedule_show(cfg) -> int:
    """Print schedule state only; this path never touches journal/auth/RPC."""
    store = schedulemod.ScheduleStore(cfg.schedule_file)
    try:
        current = _load_schedule(cfg, store)
        now = int(time.time())
        # Read-only journal inspection is safe here and does not acquire its
        # flock; schedule commands must remain usable while daemon/check own
        # the long-lived journal lock.
        remaining = statemod.Journal(
            cfg.state_file, cfg.cooldown_seconds
        ).cooldown_remaining(now)
        plan = _schedule_plan(cfg, current, now, remaining)
    except schedulemod.ScheduleCorruptError:
        print(json.dumps({"ok": False, "reason": "schedule-corrupt"}))
        return 2
    except schedulemod.ScheduleError:
        print(json.dumps({"ok": False, "reason": "schedule-error"}))
        return 2
    except statemod.StateError:
        print(json.dumps({"ok": False, "reason": "state-corrupt"}))
        return 2
    plan_data = plan.as_dict()
    next_target = plan_data["target"]
    print(json.dumps({
        "ok": True,
        "times": list(current.times),
        "timezone": current.timezone,
        "source": current.source,
        "cooldown_remaining_s": remaining,
        "next_target": next_target,
        "next_target_time": None if next_target is None else next_target["time"],
        "next_activation": None if next_target is None else next_target["activation_utc"],
        "next_activation_epoch": plan.activation_epoch,
        "schedule_allow": plan.allowed,
        "schedule_reason": plan.reason,
        "schedule": plan_data,
    }))
    return 0


def cmd_schedule(cfg, action: str, times=None, timezone: str | None = None) -> int:
    """Manage the runtime override using only its independent short lock."""
    store = schedulemod.ScheduleStore(cfg.schedule_file)
    try:
        if action == "show":
            return _schedule_show(cfg)
        if action == "reset":
            store.reset()
            current = _load_schedule(cfg, store)
            print(json.dumps({"ok": True, "action": "reset",
                              "times": list(current.times),
                              "timezone": current.timezone,
                              "source": current.source}))
            return 0
        if action == "clear":
            current = store.write((), cfg.reset_timezone)
        elif action == "set":
            if not isinstance(times, (list, tuple)) or not times:
                raise schedulemod.ScheduleError("schedule set needs a time")
            try:
                normalized = schedulemod.parse_reset_times(",".join(times))
            except (ValueError, TypeError):
                raise schedulemod.ScheduleError("invalid reset time")
            current = store.write(normalized, timezone or cfg.reset_timezone)
        else:
            raise schedulemod.ScheduleError("unknown schedule command")
    except schedulemod.ScheduleCorruptError:
        # set/clear/reset are intentionally usable as recovery operations;
        # they do not need to read the corrupt prior payload.
        print(json.dumps({"ok": False, "reason": "schedule-corrupt"}))
        return 2
    except schedulemod.ScheduleError as exc:
        detail = "schedule-locked" if isinstance(
            exc, schedulemod.ScheduleLockedError
        ) else "schedule-error"
        print(json.dumps({"ok": False, "reason": detail}))
        return 2
    print(json.dumps({"ok": True, "action": action,
                      "times": list(current.times),
                      "timezone": current.timezone,
                      "source": current.source}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openai-limit-optimizer")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon")
    d.add_argument("--once", action="store_true")
    d.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("login")
    sub.add_parser("healthcheck")
    s = sub.add_parser("schedule")
    ss = s.add_subparsers(dest="schedule_action", required=True)
    ss.add_parser("show")
    sp = ss.add_parser("set")
    sp.add_argument("times", nargs="+")
    sp.add_argument("--timezone")
    ss.add_parser("clear")
    ss.add_parser("reset")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "daemon":
        return cmd_daemon(cfg, args.once, args.dry_run)
    if args.cmd == "check":
        return cmd_check(cfg, args.dry_run)
    if args.cmd == "login":
        return cmd_login(cfg)
    if args.cmd == "healthcheck":
        return cmd_healthcheck(cfg)
    if args.cmd == "schedule":
        return cmd_schedule(cfg, args.schedule_action,
                            getattr(args, "times", None),
                            getattr(args, "timezone", None))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
