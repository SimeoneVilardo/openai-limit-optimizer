# Project Structure — openai-limit-optimizer

Task ID: `openai_limit_optimizer_20260912_docs`

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
  tests/
    unit/          # 78 test (config/main/policy/protocol/send/state)
    acceptance/    # _fakes + journal_safety/policy_idle/protocol_real/send_auth_rpc, 54 test
  config/
    example.env    # esempio OLO_* (copiare in .env, mai secret commitati)
  compose.yaml     # image-only, default simeonevilardo/openai-limit-optimizer:latest
  Dockerfile       # python:3.14-slim + Codex CLI 0.154.0 pin, user 65532
  pyproject.toml   # name 0.1.0, requires-python >=3.12 (test via unittest stdlib, nessun pytest)
  .dockerignore    # esclude .git/agent_docs/data/tests/__pycache__/.env
  .gitignore       # __pycache__/.venv/.env/data/*.log/*.tmp
  agent_docs/      # project_overview/core_tech/structure (Archivist) + progress/diary/latest (main-owned)
  README.md        # operativo utente in italiano (Archivist; senza Task ID né stato release)
```

- Versione `0.1.0`; checksum sha256 canonici in `project_core_tech.md`.
- Test canonici: `python -m unittest discover -s tests -t .` → 132 (78+54).
- Stato/credenziali runtime fuori repo: `${OLO_DATA_DIR:-./data} → /data` (`state.json`, `heartbeat.json`, `codex/`); homelab target esterno al repo.
- Nessuna `docs/evidence` vendored nel repo; i pin upstream sono URL remoti citati in `project_core_tech.md`.

## Regola
Questo file possiede solo il layout. Comportamento → overview, contratti/checksum → core_tech, operativo → README, stato operativo → progress (main-owned).
