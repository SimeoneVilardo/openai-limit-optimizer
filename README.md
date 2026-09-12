# openai-limit-optimizer

Poller Docker conservativo della finestra Codex da 5h: invia il prompt
configurato via sessione effimera **solo** su segnale moving-full-window
a due letture. Di default non fa nulla (skip) e resta in cooldown 5h dopo
ogni tentativo.

## Requisiti

- Docker + Compose v2, immagine `simeonevilardo/openai-limit-optimizer`.
- Nessuna API key: l'auth è ChatGPT via login device dedicato.
- Solo stdlib Python: nessun `pytest` richiesto per i test.

## Setup

```bash
cp config/example.env .env
# edita .env se serve (vedi Config), poi verifica il parent e crea ./data
# con l'owner atteso dal container (65532:65532):
ls -d .
sudo install -d -m 0700 -o 65532 -g 65532 ./data
docker compose up -d
docker compose logs -f app
```

`mkdir ./data` come utente host assegna l'uid sbagliato e il daemon
`65532:65532` non può scriverci: usare `install` sopra. `compose.yaml` è
image-only (`${OLO_IMAGE:-simeonevilardo/openai-limit-optimizer:latest}`;
digest via `OLO_IMAGE=...@sha256:<digest>`). Stato e credenziali vivono in
`${OLO_DATA_DIR:-./data} → /data` (`state.json`, `heartbeat.json`,
`codex/`): non committarli, tienili privati.

## Login device (dedicato, non copiare il desktop)

Prerequisito (l'utente l'ha incontrato e poi risolto): abilitare prima in
ChatGPT **Settings > Security** l'autenticazione tramite codice device;
in alternativa farsela abilitare dall'amministratore del workspace. Senza
questa impostazione il flow fallisce con un errore esplicito del provider.

Il daemon tiene il lock di stato per tutta la vita: **fermalo prima** di
`login`, `check` e `daemon --once`, poi riavvialo.

```bash
docker compose stop app
docker compose run --rm app login
# completa il device flow, poi verifica a daemon fermo:
docker compose run --rm app check
docker compose up -d
```

Dopo `login` usare `check`, non `healthcheck`: l'heartbeat è scritto solo
dal daemon, quindi `healthcheck` prima dell'avvio leggerebbe un file
vecchio/assente. Dettagli (`app/main.py`): `codex login --device-auth -c
cli_auth_credentials_store="file"` contro il `CODEX_HOME` dedicato
(`/data/codex`), poi `codex login status`. Il refresh persiste su disco,
ma revoca o policy del provider possono richiedere un nuovo login:
nessuna garanzia "una volta per sempre".

## Compose / avvio

```bash
docker compose up -d
docker compose logs -f app
# singolo ciclo senza invio (a daemon fermo):
docker compose stop app
docker compose run --rm app daemon --once --dry-run
# decisione singola (a daemon fermo):
docker compose stop app
docker compose run --rm app check
# heartbeat (legge il file, daemon anche attivo):
docker compose run --rm app healthcheck
docker compose up -d
```

## Config (tutto via environment, vedi `config/example.env`)

| Var | Default | Note |
|---|---|---|
| `OLO_POLL_SECONDS` | `600` | 60–86400 |
| `OLO_CONFIRM_SECONDS` | `30` | 21–600, separazione tra le 2 letture |
| `OLO_MODEL` | `gpt-5.6-luna` | id esatto verificato su `model/list` |
| `OLO_EFFORT` | `low` | minimal/low/medium/high/xhigh |
| `OLO_PROMPT` | `Answer only with "hi"` | ≤500ch |
| `OLO_RPC_TIMEOUT` | `60` | 5–300 |
| `OLO_SEND_TIMEOUT` | `300` | 30–1800 |
| `OLO_COOLDOWN_SECONDS` | `18000` | floor 18000, non negoziabile |
| `OLO_CODEX_BIN` | `codex` | |
| `OLO_CODEX_HOME` | `/data/codex` | assoluto, dedicato |
| `OLO_STATE_FILE` | `/data/state.json` | assoluto, mai dentro `CODEX_HOME` |
| `OLO_HEARTBEAT_FILE` | `/data/heartbeat.json` | idem |
| `OLO_IMAGE` / `OLO_DATA_DIR` / `OLO_UID` / `OLO_GID` | `…:latest` / `./data` / `65532` | solo compose |

Costanti pin (non configurabili): tolleranza 5s, finestra 300min, full 18000s.

## Commands

- `daemon [--once] [--dry-run]`: loop di poll; `--dry-run` logga `would-send` senza inviare; tiene il lock di stato per tutta la vita.
- `check [--dry-run]`: una poll+decisione `{"ok","allow","effective_allow","reason","cooldown_remaining_s"}`; valida il journal (cooldown + corrupt) e tiene il lock; **non** scrive il journal, ma può creare/sistemare `CODEX_HOME` (0700) e la cache/auth sotto `CODEX_HOME` può essere scritta dal server. `effective_allow` è `allow AND cooldown==0`: è ciò che il daemon farebbe.
- `login`: flow device sopra; tiene il lock.
- `healthcheck`: legge l'heartbeat (`healthy/degraded/blocked_auth/error` + `ts_wall`); exit 0/1/2.

## Test

Solo stdlib, nessun pytest:

```bash
python -m unittest discover -s tests -t .
```

132 test (78 unit + 54 acceptance), verificati in locale con esito OK.

## Security

- Utente `65532:65532`, `read_only:true`, `no-new-privileges`, `cap_drop: ALL`, tmpfs dedicate, nessuna porta pubblicata.
- Figli sanificati (`OPENAI_API_KEY, CODEX_API_KEY, CODEX_ACCESS_TOKEN,
  OPENAI_BASE_URL, OPENAI_ORGANIZATION, OPENAI_PROJECT, CODEX_OSS_BASE_URL,
  CODEX_OSS_PORT` rimossi): nessun fallback a fatturazione a chiavi, nessun
  provider alternativo, `supportsLunaReserve=false`, nessun riscatto crediti.
- Send effimero minimo: `--ephemeral --json --sandbox read-only
  --skip-git-repo-check --ignore-user-config --ignore-rules --disable
  shell_tool -C <tmpdir-vuoto> -m <model> -c model_reasoning_effort=<effort>
  -c web_search="disabled"`; prompt con `-` iniziale via stdin.
  `--ignore-user-config` salta il layer `config.toml` utente e le regole
  repo, ma **non** disabilita le istruzioni globali tipo `AGENTS.md`:
  quelle restano fuori grazie a `-C` su tmpdir vuoto. Il `CODEX_HOME`
  dedicato deve restare privo di `AGENTS.md`, skill, MCP e `config.toml`
  (dovere dell'operatore: solo assenza, nessun codice).
- Log JSON su singola riga senza payload/token/account/prompt; journal e
  heartbeat `0600`, dir `0700`.

## Troubleshoot

- `blocked_auth / auth-missing` → stop daemon, `login`, `check`, restart.
- `degraded poll-failed / schema-rejected` → attendi il ciclo dopo; se persiste, `check` a daemon fermo e log strutturati.
- `state-locked` → un altro comando tiene il lock: ferma il daemon e riprova.
- `state-corrupt` → **non** si risolve con re-login e **non** cancellare/resettare il journal (un reset permetterebbe un secondo send nel cooldown): preserva il file, fanne backup, indaga la causa, poi riparti solo da uno stato valido.
- `stale heartbeat` / exit 2 → ispeziona `./data/*.json` (non condividerne il contenuto), verifica che il daemon stia girando.
- `cooldown` / `cooldown-after-failure`: normale — ogni tentativo (anche failed) blocca per `OLO_COOLDOWN_SECONDS`; un failure resta degraded per tutto il cooldown.
- `would-send` in dry-run: la policy matcherebbe, nessun invio eseguito.

## Limiti espliciti

- Doppia lettura: entrambe finestre fresche `resetsAt-wall = 18000±5s`,
  `resetsAt` deve **avanzare** con l'elapsed, wall/mono d'accordo,
  `ordinaryUsageAllowed==True`, bucket `codex` 300min/0%, nessun esaurimento
  weekly/spend/reached. `resetsAt` null/scaduto/forma diversa o `0%`
  mid-window ⇒ skip.
- `usedPercent==0` **non** prova inattività reale (arrotondamento `i32`):
  euristica conservativa, non flag/garanzia API. Inattività live non osservata.
- Il ping consuma comunque quota (non gratis, non a contesto zero: system
  prompt base inevitabile, usage tiny). Può consumare weekly anche se
  inutilizzato; nessuna garanzia che ogni inizio lavoro sia <5h o che la
  quota sia illimitata. Nessun fallback automatico costoso.

## Build image

`Dockerfile`: `python:3.14-slim-bookworm` amd64 + Codex CLI **0.154.0**
pinnato (tarball musl ufficiale), verificato come `appuser`. Build
generica:

```bash
docker build -t simeonevilardo/openai-limit-optimizer:latest .
# oppure con digest pin in compose: OLO_IMAGE=simeonevilardo/openai-limit-optimizer@sha256:<digest>
```

Lo stato operativo (quali build/push/deploy risultano fatti) vive in
`agent_docs/project_progress.md`, non qui.

Pin upstream (non vendored):

- `https://raw.githubusercontent.com/openai/codex/rust-v0.154.0/codex-rs/app-server-protocol/src/protocol/v2/account.rs`
- `https://raw.githubusercontent.com/openai/codex/rust-v0.154.0/codex-rs/backend-client/src/client.rs`
