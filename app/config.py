"""Central configuration: env parsing + robust validation.

All behaviour is driven by ``OLO_*`` environment variables so the Docker
image stays portable. Every value is type-checked and bounds-checked;
any invalid value fails closed with a visible error (no silent default).

Path rules (fail closed): ``OLO_STATE_FILE``, ``OLO_HEARTBEAT_FILE`` and
``OLO_CODEX_HOME`` must be absolute and pairwise distinct, and neither
journal file may live inside ``CODEX_HOME`` (the model must never see
our journal/heartbeat, and Codex config layers must never pick them
up). The 5h cooldown floor (18000s) is not negotiable: shorter values
are rejected.
"""

from __future__ import annotations

import dataclasses
import os
import re

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
MIN_COOLDOWN = 18000

DEFAULTS = {
    "OLO_POLL_SECONDS": "600",
    "OLO_CONFIRM_SECONDS": "30",
    "OLO_MODEL": "gpt-5.6-luna",
    "OLO_EFFORT": "low",
    "OLO_PROMPT": 'Answer only with "hi"',
    "OLO_CODEX_BIN": "codex",
    "OLO_CODEX_HOME": "/data/codex",
    "OLO_STATE_FILE": "/data/state.json",
    "OLO_HEARTBEAT_FILE": "/data/heartbeat.json",
    "OLO_RPC_TIMEOUT": "60",
    "OLO_SEND_TIMEOUT": "300",
    "OLO_COOLDOWN_SECONDS": "18000",
}


@dataclasses.dataclass(frozen=True)
class Config:
    poll_seconds: int = 600
    confirm_seconds: int = 30
    model: str = "gpt-5.6-luna"
    effort: str = "low"
    prompt: str = 'Answer only with "hi"'
    codex_bin: str = "codex"
    codex_home: str = "/data/codex"
    state_file: str = "/data/state.json"
    heartbeat_file: str = "/data/heartbeat.json"
    rpc_timeout: int = 60
    send_timeout: int = 300
    cooldown_seconds: int = 18000

    # Pinned policy constants (not configurable by design).
    tolerance_seconds: int = 5
    window_minutes: int = 300
    full_window_seconds: int = 18000

    def stale_after(self) -> int:
        """Heartbeat age beyond which the loop is presumed dead."""
        return (2 * self.poll_seconds + self.rpc_timeout
                + self.send_timeout + self.confirm_seconds + 60)


class ConfigError(ValueError):
    pass


def _get(env: dict, name: str) -> str:
    val = env.get(name, DEFAULTS[name])
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return val.strip()


def _int(env: dict, name: str, lo: int, hi: int) -> int:
    raw = _get(env, name)
    try:
        val = int(raw, 10)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if not (lo <= val <= hi):
        raise ConfigError(f"{name} must be in [{lo},{hi}], got {val}")
    return val


def _abspath(raw: str, name: str) -> str:
    if not os.path.isabs(raw):
        raise ConfigError(f"{name} must be absolute, got {raw!r}")
    return os.path.normpath(raw)


def load(env: dict | None = None) -> Config:
    """Parse and validate configuration. Raises ConfigError on any problem."""
    src = dict(os.environ) if env is None else dict(env)
    for k, v in DEFAULTS.items():
        src.setdefault(k, v)

    poll = _int(src, "OLO_POLL_SECONDS", 60, 86400)
    # Separation must exceed 4*tolerance (4*5=20) -> minimum 21s.
    confirm = _int(src, "OLO_CONFIRM_SECONDS", 21, 600)
    model = _get(src, "OLO_MODEL")
    if not MODEL_RE.match(model):
        raise ConfigError(f"OLO_MODEL has invalid shape: {model!r}")
    effort = _get(src, "OLO_EFFORT").lower()
    if effort not in EFFORTS:
        raise ConfigError(f"OLO_EFFORT must be one of {EFFORTS}, got {effort!r}")
    prompt = _get(src, "OLO_PROMPT")
    if len(prompt) > 500:
        raise ConfigError("OLO_PROMPT must be <= 500 characters")
    codex_bin = _get(src, "OLO_CODEX_BIN")
    codex_home = _abspath(_get(src, "OLO_CODEX_HOME"), "OLO_CODEX_HOME")
    state_file = _abspath(_get(src, "OLO_STATE_FILE"), "OLO_STATE_FILE")
    heartbeat_file = _abspath(_get(src, "OLO_HEARTBEAT_FILE"), "OLO_HEARTBEAT_FILE")
    rpc_timeout = _int(src, "OLO_RPC_TIMEOUT", 5, 300)
    send_timeout = _int(src, "OLO_SEND_TIMEOUT", 30, 1800)
    cooldown = _int(src, "OLO_COOLDOWN_SECONDS", MIN_COOLDOWN, 86400)
    if len({state_file, heartbeat_file, codex_home}) != 3:
        raise ConfigError("OLO_STATE_FILE, OLO_HEARTBEAT_FILE and "
                          "OLO_CODEX_HOME must all differ")
    for label, path in (("OLO_STATE_FILE", state_file),
                        ("OLO_HEARTBEAT_FILE", heartbeat_file)):
        if os.path.commonpath([codex_home, path]) == codex_home:
            raise ConfigError(f"{label} must not live inside OLO_CODEX_HOME")
    return Config(
        poll_seconds=poll,
        confirm_seconds=confirm,
        model=model,
        effort=effort,
        prompt=prompt,
        codex_bin=codex_bin,
        codex_home=codex_home,
        state_file=state_file,
        heartbeat_file=heartbeat_file,
        rpc_timeout=rpc_timeout,
        send_timeout=send_timeout,
        cooldown_seconds=cooldown,
    )
