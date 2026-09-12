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


def poll_once(cfg, client: protomod.AppServerClient):
    """Two fresh reads separated by confirm_seconds. Returns decision tuple.

    (allow, reason, wall1, wall2). Raises ProtocolError/AuthError.
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
    return allow, reason


def _cooldown_heartbeat(journal: statemod.Journal, now: int):
    """Heartbeat disposition while cooling down: only a past ``sent``
    reads healthy; failed/pending/unknown outcomes stay degraded so a
    failure is never hidden by the cooldown."""
    if journal.last_outcome() == "sent":
        return "healthy", "cooldown", 0
    return "degraded", "cooldown-after-failure", 1


def _cycle(cfg, journal: statemod.Journal, dry_run: bool):
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
    if remaining > 0:
        status, detail, code = _cooldown_heartbeat(journal, now)
        log("info", "cooldown", remaining_s=remaining, status=status)
        write_heartbeat(cfg.heartbeat_file, status, detail)
        return "cooldown", code
    try:
        with protomod.AppServerClient(cfg.codex_bin, cfg.codex_home,
                                      cfg.rpc_timeout,
                                      cancel_event=STOP) as client:
            allow, reason = poll_once(cfg, client)
    except protomod.AuthError:
        log("warning", "auth-missing",
            hint="run login command for device auth")
        write_heartbeat(cfg.heartbeat_file, "blocked_auth", "auth-missing")
        return "blocked_auth", 1
    except protomod.CancelledError:
        log("info", "cancelled")
        return "error", 2
    except protomod.ProtocolError:
        log("warning", "poll-failed")
        write_heartbeat(cfg.heartbeat_file, "degraded", "poll-failed")
        return "degraded", 2
    except policymod.PolicyError:
        log("warning", "schema-rejected")
        write_heartbeat(cfg.heartbeat_file, "degraded", "schema-rejected")
        return "degraded", 2
    if not allow:
        log("info", "skip", reason=reason)
        write_heartbeat(cfg.heartbeat_file, "healthy", reason)
        return "skip", 0
    if dry_run:
        log("info", "would-send", reason=reason)
        write_heartbeat(cfg.heartbeat_file, "healthy", "would-send")
        return "skip", 0
    if journal.cooldown_remaining(int(time.time())) > 0:
        log("info", "skip", reason="cooldown-raced")
        write_heartbeat(cfg.heartbeat_file, "healthy", "cooldown-raced")
        return "cooldown", 0
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
    try:
        while not STOP.is_set():
            cycle_start = time.monotonic()
            try:
                _disp, code = _cycle(cfg, journal, dry_run)
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
            wait = max(0.0, cfg.poll_seconds - elapsed)
            STOP.wait(wait)
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
            remaining = journal.cooldown_remaining(int(time.time()))
        except statemod.CorruptedError:
            print(json.dumps({"ok": False, "reason": "state-corrupt"}))
            return 2
        except statemod.StateError:
            print(json.dumps({"ok": False, "reason": "state-error"}))
            return 2
        try:
            with protomod.AppServerClient(cfg.codex_bin, cfg.codex_home,
                                          cfg.rpc_timeout,
                                          cancel_event=STOP) as client:
                allow, reason = poll_once(cfg, client)
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
    finally:
        journal.release()
    # No state writes here by design; the reported allow is gated by the
    # observed cooldown so check never claims an action the daemon would
    # refuse.
    print(json.dumps({"ok": True, "allow": allow,
                      "effective_allow": bool(allow and remaining == 0),
                      "reason": reason, "cooldown_remaining_s": remaining}))
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
