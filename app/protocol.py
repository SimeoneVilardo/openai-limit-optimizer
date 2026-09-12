"""Minimal app-server JSON-RPC client over stdio (stdlib only).

Speaks newline-delimited JSON-RPC to ``codex app-server``:
``initialize`` + ``initialized`` notification, then ``account/read``,
``account/rateLimits/read`` and paginated ``model/list``. No inference,
no billing, no reset-credit redemption.

Transport hardening:
- raw ``os.read`` buffering: coalesced frames are split, partial lines
  wait for the rest, oversize lines fail closed — ``readline`` is never
  used, so no blocking on a partial line and no lost frames;
- stderr is drained forever in a daemon thread (pure discard with a
  counting cap), so a chatty server can never deadlock us;
- every deadline uses the monotonic clock and is sliced so a
  cancellation event (SIGTERM) reacts promptly;
- error surfacing is sanitized: fixed reason codes only, never raw
  server text (which could carry tokens or account data);
- spawn failures, I/O errors and init failures clean up the whole
  process group (close pipes, SIGTERM, bounded wait, SIGKILL, reap).

Child environment is sanitized (``child_env``): inherited API keys and
provider overrides are dropped so neither the app-server nor later
``exec`` sends can fall back to key billing or alternate providers.
``CODEX_HOME`` always points at the dedicated persistent volume.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time

MAX_LINE_BYTES = 1_000_000
COUNT_CAP_BYTES = 256_000
READ_CHUNK = 65536
MAX_MODEL_PAGES = 25
_IO_SLICE = 0.5
_REAP_TERM_TIMEOUT = 10
_REAP_KILL_TIMEOUT = 5

# Inherited variables that must never reach Codex children: API keys /
# tokens (would enable key billing instead of the ChatGPT plan) and
# provider endpoint overrides (would redirect traffic).
SANITIZED_ENV_KEYS = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "CODEX_OSS_BASE_URL",
    "CODEX_OSS_PORT",
)


def child_env(codex_home: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SANITIZED_ENV_KEYS}
    env["CODEX_HOME"] = codex_home
    return env


class ProtocolError(RuntimeError):
    pass


class AuthError(ProtocolError):
    """Server reports missing/invalid ChatGPT auth (sanitized, no detail)."""


class CancelledError(ProtocolError):
    pass


class HomeError(RuntimeError):
    """Dedicated CODEX_HOME is missing and cannot be created/written."""


def ensure_codex_home(path: str) -> None:
    """Create the dedicated home 0700 if absent; enforce 0700 + writable.

    Shared bootstrap for daemon/check/login/send children. Raises
    HomeError (visible, no traceback payload) when the directory cannot
    exist writable — callers must fail closed, never fall through to a
    confusing downstream transport error.
    """
    try:
        if not os.path.isdir(path):
            os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        if not os.path.isdir(path) or not os.access(path, os.W_OK | os.X_OK):
            raise HomeError("unwritable")
    except HomeError:
        raise
    except OSError:
        raise HomeError("unwritable")


def _drain_forever(pipe, counter: list) -> None:
    """Read until EOF, counting bytes (cap is counting-only, drain never stops)."""
    try:
        while True:
            try:
                chunk = pipe.read(READ_CHUNK)
            except Exception:
                return
            if not chunk:
                return
            if counter[0] < COUNT_CAP_BYTES + 1:
                counter[0] += len(chunk)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _kill_group(proc) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        pass


def _wait_exit(proc, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if proc.poll() is not None:
                return
        except Exception:
            return
        time.sleep(0.05)


def _terminate_group(proc, leader: int) -> None:
    """SIGTERM, bounded wait, SIGKILL, bounded wait, reap — by saved pgid.

    The group is signalled via the pgid captured at spawn, independent
    of the direct child's status: even when the parent (proc) is already
    reaped/gone, lingering descendants in the same group are still
    reached. Unknown groups (ESRCH) are ignored; the own child is always
    reaped so no zombie survives.
    """
    try:
        os.killpg(leader, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    _wait_exit(proc, _REAP_TERM_TIMEOUT)
    try:
        os.killpg(leader, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    _wait_exit(proc, _REAP_KILL_TIMEOUT)
    try:
        proc.wait(timeout=_REAP_KILL_TIMEOUT)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=_REAP_KILL_TIMEOUT)
        except Exception:
            pass


class AppServerClient:
    """Context-managed client. ``spawn`` is injectable for unit tests."""

    def __init__(self, codex_bin="codex", codex_home="/data/codex",
                 timeout=60, spawn=None, cancel_event=None):
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.timeout = timeout
        self._spawn = spawn or self._default_spawn
        self._cancel = cancel_event
        self._proc = None
        self._leader = None
        self._rid = 0
        self._buf = bytearray()
        self._stderr_total = [0]
        self._stderr_thread = None
        self.notifications_seen = 0

    def _default_spawn(self):
        return subprocess.Popen(
            [self.codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
            env=child_env(self.codex_home),
        )

    def _cancelled(self) -> bool:
        try:
            return bool(self._cancel and self._cancel.is_set())
        except Exception:
            return False

    def __enter__(self):
        try:
            proc = self._spawn()
        except OSError:
            raise ProtocolError("spawn-failed")
        try:
            # start_new_session => child is a group leader: pgid == pid.
            # Saved now so cleanup reaches descendants even after the
            # direct child is reaped/gone (getpgid would fail then).
            self._leader = os.getpgid(proc.pid)
        except Exception:
            self._leader = proc.pid
        self._proc = proc
        try:
            err = proc.stderr
            if err is not None:
                t = threading.Thread(target=_drain_forever,
                                     args=(err, self._stderr_total), daemon=True)
                t.start()
                self._stderr_thread = t
            self._raw_call("initialize",
                           {"clientInfo": {"name": "openai-limit-optimizer",
                                           "version": "0.1.0"}},
                           timeout=self.timeout)
            # Handshake completion required by the protocol: the server
            # only treats the session as initialized after this.
            self._notify("initialized", {})
        except ProtocolError:
            self._cleanup()
            raise
        except Exception:
            self._cleanup()
            raise ProtocolError("init-failed")
        return self

    def __exit__(self, *exc):
        self._cleanup()
        return False

    def _cleanup(self) -> None:
        proc, self._proc = self._proc, None
        leader, self._leader = self._leader, None
        self._buf = bytearray()
        if proc is None:
            return
        for stream in ("stdin", "stdout"):
            try:
                fh = getattr(proc, stream, None)
                if fh is not None:
                    fh.close()
            except Exception:
                pass
        try:
            if leader is None:
                try:
                    leader = os.getpgid(proc.pid)
                except Exception:
                    leader = proc.pid
            _terminate_group(proc, leader)
        finally:
            t = self._stderr_thread
            self._stderr_thread = None
            if t is not None:
                t.join(timeout=_REAP_KILL_TIMEOUT)

    def _notify(self, method: str, params: dict) -> None:
        proc = self._proc
        frame = (json.dumps({"jsonrpc": "2.0", "method": method,
                             "params": params}) + "\n").encode()
        try:
            proc.stdin.write(frame)
            proc.stdin.flush()
        except Exception:
            raise ProtocolError("write-failed")

    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    def _read_frames(self, rid: int, deadline: float):
        """Yield parsed frames until the response ``rid`` arrives."""
        proc = self._proc
        out = proc.stdout
        try:
            fd = out.fileno()
        except Exception:
            raise ProtocolError("transport-closed")
        while True:
            if self._cancelled():
                raise CancelledError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("timeout")
            try:
                r, _, _ = select.select([fd], [], [], min(remaining, _IO_SLICE))
            except Exception:
                raise ProtocolError("transport-closed")
            if not r:
                if proc.poll() is not None and not self._buf:
                    raise ProtocolError("server-exited")
                continue
            try:
                chunk = os.read(fd, READ_CHUNK)
            except OSError:
                raise ProtocolError("transport-closed")
            if not chunk:
                # EOF with no complete trailing line: fail closed.
                self._buf = bytearray()
                raise ProtocolError("eof")
            self._buf += chunk
            if len(self._buf) > MAX_LINE_BYTES + 1 and b"\n" not in self._buf:
                raise ProtocolError("oversize-frame")
            while True:
                nl = self._buf.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                if not raw.strip():
                    continue
                if len(raw) > MAX_LINE_BYTES:
                    raise ProtocolError("oversize-frame")
                try:
                    msg = json.loads(raw)
                except ValueError:
                    raise ProtocolError("malformed-frame")
                if not isinstance(msg, dict):
                    raise ProtocolError("malformed-frame")
                yield msg

    def _raw_call(self, method: str, params, timeout: float | None = None):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise ProtocolError("transport-closed")
        rid = self._next_id()
        frame = (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                             "params": params}) + "\n").encode()
        try:
            proc.stdin.write(frame)
            proc.stdin.flush()
        except Exception:
            raise ProtocolError("write-failed")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        for msg in self._read_frames(rid, deadline):
            if msg.get("id") != rid:
                if "method" in msg and "id" not in msg:
                    self.notifications_seen += 1
                continue
            err = msg.get("error")
            if err is not None:
                # Sanitized: fixed codes only, never raw server text.
                if isinstance(err, dict) and isinstance(err.get("message"), str):
                    low = err["message"].lower()
                    if "auth" in low or "login" in low:
                        raise AuthError("auth-required")
                raise ProtocolError("rpc-error")
            if "result" not in msg:
                raise ProtocolError("malformed-response")
            return msg["result"]
        raise ProtocolError("eof")

    def call(self, method: str, params, timeout: float | None = None):
        return self._raw_call(method, params, timeout=timeout)

    # ---- high-level reads (sanitized shapes, no secrets retained) ----
    def account_read(self):
        return self.call("account/read", {})

    def rate_limits_read(self):
        return self.call("account/rateLimits/read",
                         {"excludeResetCreditDetails": True,
                          "supportsLunaReserve": False})

    def model_list(self, limit: int = 50, cursor=None):
        params: dict = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self.call("model/list", params)

    def iter_models(self, limit: int = 50):
        """Yield model entries across all pages (cursor pagination).

        Bounded: a repeated cursor (server loop) or more than
        MAX_MODEL_PAGES pages fails closed instead of looping forever.
        """
        cursor = None
        seen = set()
        pages = 0
        while True:
            page = self.model_list(limit=limit, cursor=cursor)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ProtocolError("malformed-model-list")
            pages += 1
            if pages > MAX_MODEL_PAGES:
                raise ProtocolError("model-pages-exceeded")
            for entry in page["data"]:
                yield entry
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise ProtocolError("model-pages-loop")
            seen.add(cursor)

    def ensure_model(self, model_id: str, effort: str, limit: int = 50) -> bool:
        """Require the exact model id AND the exact configured effort.

        Raises ``ProtocolError("model-unknown")`` when the id never
        appears, ``("effort-unknown")`` when its effort catalogue is
        missing/empty, ``("effort-unsupported")`` when the configured
        effort is not advertised. Never returns False.
        """
        want = effort.lower()
        for entry in self.iter_models(limit=limit):
            if not isinstance(entry, dict) or entry.get("id") != model_id:
                continue
            efforts = entry.get("supportedReasoningEfforts")
            if not isinstance(efforts, list) or not efforts:
                raise ProtocolError("effort-unknown")
            for opt in efforts:
                if (isinstance(opt, dict)
                        and str(opt.get("reasoningEffort", "")).lower() == want):
                    return True
            raise ProtocolError("effort-unsupported")
        raise ProtocolError("model-unknown")


def is_chatgpt_account(acct_result: dict) -> bool:
    """True only for ChatGPT accounts. apiKey/unknown -> False (fail closed)."""
    if not isinstance(acct_result, dict):
        return False
    acct = acct_result.get("account")
    if not isinstance(acct, dict):
        return False
    return acct.get("type") == "chatgpt"


def model_available(page, model_id: str) -> bool:
    """Single-page exact-id check (no effort gate).

    This is only a building block: the daemon gate is ``ensure_model``,
    which paginates and additionally requires the exact configured
    reasoning effort.
    """
    if isinstance(page, dict):
        data = page.get("data", page.get("models", []))
    elif isinstance(page, list):
        data = page
    else:
        return False
    if not isinstance(data, list):
        return False
    for m in data:
        if isinstance(m, dict) and m.get("id") == model_id:
            return True
        if isinstance(m, str) and m == model_id:
            return True
    return False
