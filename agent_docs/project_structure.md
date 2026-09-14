# Project Structure — openai-limit-optimizer

Task ID: `reset_target_recovery_20260914_closure` (continuation `ses_f5f21e9e2ffeAXPqUpOluHg866`; prior blocked notes historical).

## Layout verificato (lettura directory)
```
open-ai-limit-optimizer/
  app/
    __init__.py  config.py      # OLO_* parsing/validazione (canonico env)
    main.py        # daemon/check(login/healthcheck + lock lifetime + heartbeat; check→effective_allow/cooldown)
    policy.py      # policy pura fail-closed (doppia lettura, full-window)
    protocol.py    # client app-server stdio + child_env sanificato + ensure_codex_home
    send.py        # unico send effimero (argv esatto; AGENTS isolato via -C, non via --ignore-user-config)
    state.py       # journal flock + cooldown pre-attempt
    schedule.py    # NUOVO working-tree: planner/override persistente (non pubblicato); dettagli in core_tech
  tests/
    unit/          # storico 78 test (config/main/policy/protocol/send/state) + 15 schedule executor
    acceptance/    # _fakes + journal_safety/policy_idle/protocol_real/send_auth_rpc, storico 54 test + 43 schedule indipendenti (17 reset + 13 regression + 13 loop)
  config/
    example.env    # esempio OLO_* (copiare in .env, mai secret commitati)
  compose.yaml     # image-only, default simeonevilardo/openai-limit-optimizer:latest
  Dockerfile       # python:3.14-slim + Codex CLI 0.154.0 pin, user 65532
  pyproject.toml   # name 0.1.0, requires-python >=3.12 (test via unittest stdlib, nessun pytest)
  .dockerignore    # esclude .git/agent_docs/data/tests/__pycache__/.env
  .gitignore       # __pycache__/.venv/.env/data/*.log/*.tmp
   agent_docs/      # project_overview/core_tech/structure (Archivist) + progress/diary/latest (main-owned)
   README.md        # operativo utente in inglese (Archivist; senza Task ID né stato release)
```

- Versione `0.1.0`; checksum 2026-09-12 STORICI (canonici etichettati in `project_core_tech.md`), nessun hash corrente verificato.
- Test: 190 finali indipendenti (132 preservati + 15 executor + 43 indipendenti); precedenti 164/bloccati storici (provenance in README + progress/latest, main-owned).
- Stato/credenziali runtime fuori repo: `${OLO_DATA_DIR:-./data} → /data` (`state.json`, `heartbeat.json`, `codex/`); homelab target esterno al repo.
- Nessuna `docs/evidence` vendored nel repo; i pin upstream sono URL remoti citati in `project_core_tech.md`.

## Regola
Questo file possiede solo il layout. Comportamento → overview, contratti/checksum → core_tech, operativo → README, stato operativo → progress (main-owned).
