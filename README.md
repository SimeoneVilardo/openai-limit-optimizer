# openai-limit-optimizer

Conservative 5h Codex window poller: it sends the configured prompt via an
ephemeral session **only** on a moving-full-window signal at two readings.
By default it does nothing (skip) and stays in 5h cooldown after each
attempt.

## Requirements

- Docker + Compose v2, image `simeonevilardo/openai-limit-optimizer`.
- No API key: auth is ChatGPT via dedicated device login.
- Stdlib-only Python: no `pytest` required for tests.

## Setup

```bash
cp config/example.env .env
# edit .env if needed (see Config), then verify the parent and create ./data
# with the owner expected by the container (65532:65532):
ls -d .
sudo install -d -m 0700 -o 65532 -g 65532 ./data
docker compose up -d
docker compose logs -f app
```

`mkdir ./data` as the host user assigns the wrong uid and the
`65532:65532` daemon cannot write there: use the `install` above.
`compose.yaml` is image-only
(`${OLO_IMAGE:-simeonevilardo/openai-limit-optimizer:latest}`;
digest via `OLO_IMAGE=...@sha256:<digest>`). State and credentials live in
`${OLO_DATA_DIR:-./data} → /data` (`state.json`, `heartbeat.json`,
`codex/`): do not commit them, keep them private.

## Device login (dedicated, do not copy the desktop)

Prerequisite (the user hit this and then resolved it): first enable
device-code authentication in ChatGPT **Settings > Security**;
alternatively have it enabled by the workspace administrator. Without
this setting the flow fails with an explicit provider error.

The daemon holds the state lock for its whole lifetime: **stop it first**
before `login`, `check` and `daemon --once`, then restart it.

```bash
docker compose stop app
docker compose run --rm app login
# complete the device flow, then verify with the daemon stopped:
docker compose run --rm app check
docker compose up -d
```

After `login` use `check`, not `healthcheck`: the heartbeat is written only
by the daemon, so `healthcheck` before startup would read a stale/missing
file. Details (`app/main.py`): `codex login --device-auth -c
cli_auth_credentials_store="file"` against the dedicated `CODEX_HOME`
(`/data/codex`), then `codex login status`. The refresh persists on disk,
but provider revocation or policy may require a new login:
no "once forever" guarantee.

## Compose / startup

```bash
docker compose up -d
docker compose logs -f app
# single cycle without sending (with the daemon stopped):
docker compose stop app
docker compose run --rm app daemon --once --dry-run
# single decision (with the daemon stopped):
docker compose stop app
docker compose run --rm app check
# heartbeat (reads the file, daemon may be running):
docker compose run --rm app healthcheck
docker compose up -d
```

## Config (all via environment, see `config/example.env`)

| Var | Default | Notes |
|---|---|---|
| `OLO_POLL_SECONDS` | `600` | 60–86400 |
| `OLO_CONFIRM_SECONDS` | `30` | 21–600, gap between the 2 readings |
| `OLO_MODEL` | `gpt-5.6-luna` | exact id verified on `model/list` |
| `OLO_EFFORT` | `low` | minimal/low/medium/high/xhigh |
| `OLO_PROMPT` | `Answer only with "hi"` | ≤500 chars |
| `OLO_RPC_TIMEOUT` | `60` | 5–300 |
| `OLO_SEND_TIMEOUT` | `300` | 30–1800 |
| `OLO_COOLDOWN_SECONDS` | `18000` | floor 18000, non-negotiable |
| `OLO_CODEX_BIN` | `codex` | |
| `OLO_CODEX_HOME` | `/data/codex` | absolute, dedicated |
| `OLO_STATE_FILE` | `/data/state.json` | absolute, never inside `CODEX_HOME` |
| `OLO_HEARTBEAT_FILE` | `/data/heartbeat.json` | same |
| `OLO_IMAGE` / `OLO_DATA_DIR` / `OLO_UID` / `OLO_GID` | `…:latest` / `./data` / `65532` | compose only |

Pinned constants (not configurable): 5s tolerance, 300min window, 18000s full.

## Commands

- `daemon [--once] [--dry-run]`: poll loop; `--dry-run` logs `would-send` without sending; holds the state lock for its whole lifetime.
- `check [--dry-run]`: one poll+decision `{"ok","allow","effective_allow","reason","cooldown_remaining_s"}`; validates the journal (cooldown + corrupt) and holds the lock; does **not** write the journal, but may create/fix `CODEX_HOME` (0700) and the cache/auth under `CODEX_HOME` may be written by the server. `effective_allow` is `allow AND cooldown==0`: what the daemon would do.
- `login`: device flow above; holds the lock.
- `healthcheck`: reads the heartbeat (`healthy/degraded/blocked_auth/error` + `ts_wall`); exit 0/1/2.

## Test

Stdlib only, no pytest:

```bash
python -m unittest discover -s tests -t .
```

132 tests (78 unit + 54 acceptance), verified locally with OK result.

## Security

- User `65532:65532`, `read_only:true`, `no-new-privileges`, `cap_drop: ALL`, dedicated tmpfs, no published ports.
- Sanitized children (`OPENAI_API_KEY, CODEX_API_KEY, CODEX_ACCESS_TOKEN,
  OPENAI_BASE_URL, OPENAI_ORGANIZATION, OPENAI_PROJECT, CODEX_OSS_BASE_URL,
  CODEX_OSS_PORT` removed): no key-billing fallback, no alternative
  provider, `supportsLunaReserve=false`, no credit redemption.
- Minimal ephemeral send: `--ephemeral --json --sandbox read-only
  --skip-git-repo-check --ignore-user-config --ignore-rules --disable
  shell_tool -C <empty-tmpdir> -m <model> -c model_reasoning_effort=<effort>
  -c web_search="disabled"`; prompt with leading `-` via stdin.
  `--ignore-user-config` skips the user `config.toml` layer and repo rules,
  but does **not** disable global instructions such as `AGENTS.md`:
  those stay out thanks to `-C` on an empty tmpdir. The dedicated
  `CODEX_HOME` must stay free of `AGENTS.md`, skills, MCP and `config.toml`
  (operator duty: absence only, no code).
- Single-line JSON logs without payload/token/account/prompt; journal and
  heartbeat `0600`, dirs `0700`.

## Troubleshooting

- `blocked_auth / auth-missing` → stop daemon, `login`, `check`, restart.
- `degraded poll-failed / schema-rejected` → wait for the next cycle; if it persists, `check` with the daemon stopped and structured logs.
- `state-locked` → another command holds the lock: stop the daemon and retry.
- `state-corrupt` → it is **not** fixed by re-login and do **not** delete/reset the journal (a reset would allow a second send within the cooldown): preserve the file, back it up, investigate the cause, then restart only from a valid state.
- `stale heartbeat` / exit 2 → inspect `./data/*.json` (do not share their content), verify the daemon is running.
- `cooldown` / `cooldown-after-failure`: normal — every attempt (even failed) locks for `OLO_COOLDOWN_SECONDS`; a failure stays degraded for the whole cooldown.
- `would-send` in dry-run: the policy would match, no send performed.

## Explicit limits

- Double reading: both windows fresh `resetsAt-wall = 18000±5s`,
  `resetsAt` must **advance** with the elapsed time, wall/mono in agreement,
  `ordinaryUsageAllowed==True`, `codex` 300min/0% bucket, no weekly/spend/reached
  exhaustion. Null/expired/differently-shaped `resetsAt` or mid-window `0%`
  ⇒ skip.
- `usedPercent==0` does **not** prove real inactivity (`i32` rounding):
  conservative heuristic, not an API flag/guarantee. Live inactivity not observed.
- The ping still consumes quota (not free, not zero-context: unavoidable base
  system prompt, tiny usage). It may consume weekly quota even when unused;
  no guarantee that every work start is <5h away or that quota is unlimited.
  No costly automatic fallback.

## Build image

`Dockerfile`: `python:3.14-slim-bookworm` amd64 + Codex CLI **0.154.0**
pinned (official musl tarball), verified as `appuser`. Generic build:

```bash
docker build -t simeonevilardo/openai-limit-optimizer:latest .
# or with digest pin in compose: OLO_IMAGE=simeonevilardo/openai-limit-optimizer@sha256:<digest>
```

Operational state (which builds/pushes/deploys were done) lives in
`agent_docs/project_progress.md`, not here.

Upstream pins (not vendored):

- `https://raw.githubusercontent.com/openai/codex/rust-v0.154.0/codex-rs/app-server-protocol/src/protocol/v2/account.rs`
- `https://raw.githubusercontent.com/openai/codex/rust-v0.154.0/codex-rs/backend-client/src/client.rs`
