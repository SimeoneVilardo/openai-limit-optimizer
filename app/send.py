"""Ephemeral Codex send (the ONLY inference path).

Exact argv (every flag verified against ``codex exec --help`` 0.154.0,
every ``-c`` key verified against the 0.154.0 config reference /
``codex-rs/config``)::

    codex exec --ephemeral --json --sandbox read-only
        --skip-git-repo-check --ignore-user-config --ignore-rules
        --disable shell_tool
        -C <fresh-empty-tmpdir>
        -m <model> -c model_reasoning_effort=<effort>
        -c web_search="disabled"
        <prompt | - (prompt on stdin when it starts with '-')>

Tool suppression (verified, layered):
- ``--disable shell_tool`` maps to ``features.shell_tool=false``
  (documented stable feature flag) — the model gets no shell tool;
- ``-c web_search="disabled"`` removes the web-search tool
  (top-level ``web_search`` mode; the legacy ``tools.web_search``
  boolean is deprecated/ignored upstream, so it is NOT used);
- ``--ignore-user-config`` + ``--ignore-rules`` skip the user
  ``config.toml`` layer (where user MCP servers live) and repo
  exec-policy rule files (auth still uses ``CODEX_HOME``).
  NOTE: ``--ignore-user-config`` does NOT suppress global model
  instructions (repo ``AGENTS.md`` and friends); those are kept out by
  running with ``-C`` in a fresh empty tmpdir, and the dedicated
  ``CODEX_HOME`` volume must itself contain no ``AGENTS.md`` or
  instruction files (operator duty — no code needed, just absence);
- ``-C`` empty tmpdir + ``--ephemeral``: no repo, no instructions,
  nothing persisted; ``read-only`` sandbox as the final boundary.
- Child env is sanitized (see ``protocol.child_env``): inherited API
  keys/tokens and provider endpoint overrides are dropped, so the run
  cannot fall back to key billing, credit-backed providers or OSS
  endpoints. The ChatGPT plan identity comes only from the dedicated
  ``CODEX_HOME`` file store.

Documented gaps (pinned 0.154.0):
- no request-retry flag exists on ``exec`` and retry tuning upstream
  lives only inside ``[model_providers.<id>]`` blocks, which do not
  apply to ChatGPT-managed auth — so no retry knob is passed;
- the CLI always prepends its built-in system prompt; no supported
  flag disables it (base context is unavoidable);
- ``app-server`` has no ``--ignore-user-config`` flag; its mitigation
  is the dedicated ``CODEX_HOME`` (we never write ``config.toml``
  there — operators must not add MCP servers to it either).

Dash-leading prompts are fed via stdin (``-`` placeholder, explicitly
documented in ``exec --help``) so they can never parse as flags;
all other prompts travel as a single argv element (no shell).
Output is drained by streaming pump threads with a counting cap and a
monotonic deadline — ``communicate`` is never used, so an unbounded
stream cannot exhaust memory. The child always runs in its own process
group and is reaped (SIGTERM, bounded wait, SIGKILL, reap) even when
the parent is exiting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time

from .protocol import child_env, _terminate_group

READ_CHUNK = 65536
MAX_OUTPUT_BYTES = 512_000
_POLL_SLICE = 0.2


def build_argv(codex_bin: str, model: str, effort: str, prompt: str,
               workdir: str) -> list:
    """Argv vector (never a shell string). Dash-leading prompts use stdin."""
    base = [
        codex_bin, "exec",
        "--ephemeral", "--json",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config", "--ignore-rules",
        "--disable", "shell_tool",
        "-C", workdir,
        "-m", model,
        "-c", f"model_reasoning_effort={effort}",
        "-c", 'web_search="disabled"',
    ]
    if prompt.startswith("-"):
        return base + ["-"]
    return base + [prompt]


def _pump(pipe, total: list) -> None:
    try:
        while True:
            try:
                chunk = pipe.read(READ_CHUNK)
            except Exception:
                return
            if not chunk:
                return
            total[0] += len(chunk)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run_send(codex_bin: str, codex_home: str, model: str, effort: str,
             prompt: str, timeout: int, cancel_event=None) -> tuple:
    """Run one ephemeral send. Returns (ok: bool, detail: str code)."""
    workdir = tempfile.mkdtemp(prefix="olo-empty-")
    proc = None
    leader = None
    pumps: list = []
    try:
        argv = build_argv(codex_bin, model, effort, prompt, workdir)
        stdin_cfg = subprocess.DEVNULL
        stdin_data = None
        if prompt.startswith("-"):
            stdin_cfg = subprocess.PIPE
            stdin_data = prompt.encode("utf-8", "replace")
        try:
            proc = subprocess.Popen(
                argv, env=child_env(codex_home), cwd=workdir,
                stdin=stdin_cfg,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, start_new_session=True,
            )
        except OSError:
            return False, "send-spawn-failed"
        try:
            leader = os.getpgid(proc.pid)
        except Exception:
            leader = proc.pid
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data)
            except Exception:
                pass
            try:
                proc.stdin.close()
            except Exception:
                pass
        out_total, err_total = [0], [0]
        for pipe, total in ((proc.stdout, out_total), (proc.stderr, err_total)):
            t = threading.Thread(target=_pump, args=(pipe, total), daemon=True)
            t.start()
            pumps.append(t)
        deadline = time.monotonic() + timeout
        code = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_group(proc, leader)
                return False, "send-cancelled"
            code = proc.poll()
            if code is not None:
                break
            if time.monotonic() >= deadline:
                _terminate_group(proc, leader)
                return False, "send-timeout"
            time.sleep(_POLL_SLICE)
        for t in pumps:
            t.join(timeout=10)
        if out_total[0] + err_total[0] > MAX_OUTPUT_BYTES:
            return False, "send-oversize-output"
        if code == 0:
            return True, "sent"
        return False, "send-exit-nonzero"
    finally:
        # Always terminate by saved pgid — even when the direct child
        # already exited — so descendants never survive us, then reap,
        # join pumps bounded, and remove the workdir.
        if proc is not None:
            try:
                _terminate_group(proc, leader if leader is not None else proc.pid)
            except Exception:
                pass
        for t in pumps:
            if t.is_alive():
                t.join(timeout=10)
        shutil.rmtree(workdir, ignore_errors=True)
