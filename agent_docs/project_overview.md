# Project Overview — openai-limit-optimizer

Task ID: `openai_limit_optimizer_20260912_docs`

## Stato verificato da produzione (2026-09-12, lettura sorgente + unittest)
- Repo: `/home/simeone/dev/open-ai-limit-optimizer`, branch `master`, remote `origin git@github.com:SimeoneVilardo/openai-limit-optimizer.git`.
- Implementato: daemon Python stdlib-only v0.1.0 (`app/`), `Dockerfile` + `compose.yaml` image-only, `config/example.env`, test `tests/unit` + `tests/acceptance`.
- Comportamento: poll conservativo della finestra Codex 5h; invia `OLO_PROMPT` via sessione effimera **solo** su segnale moving-full-window a due letture. Default in `app/config.py`: poll 600s, confirm 30s, model `gpt-5.6-luna`, effort `low`, prompt `Answer only with "hi"`, cooldown 18000s.
- Test: `python -m unittest discover -s tests -t .` → **132 OK** (78 unit + 54 acceptance), verificato dall'Archivista.
- Causa missing-home dello smoke preflight risolta in sorgente (`protocol.ensure_codex_home` condiviso daemon/check/login); nuova build immagine in attesa.
- Stato operativo (build/push/deploy): canonico in `project_progress.md` (main-owned), non duplicato qui né in README.

## Policy (sintesi; canonico in `project_core_tech.md`)
- Doppia lettura separata da `OLO_CONFIRM_SECONDS` (default 30s); entrambe finestre fresche `resetsAt-wall ∈ 18000±5s`, `resetsAt` deve avanzare con l'elapsed (`|dResets-dWall|≤5s`), orologi wall/mono d'accordo, `ordinaryUsageAllowed==True`, bucket `codex` 300min/0%, nessun esaurimento weekly/spend/reached.
- `check` riporta `effective_allow = allow AND cooldown==0` senza scrivere il journal.
- Cooldown durevole con journal pre-attempt: **ogni** tentativo (anche failed) estende `OLO_COOLDOWN_SECONDS` (floor 18000s); journal corrupt ⇒ fail closed, mai cancellare/resettare.
- `usedPercent==0` **non** prova zero reale (arrotondamento `i32`); `resetsAt` null/scaduto/forma diversa ⇒ skip.

## Limiti espliciti (non negoziabili nei doc)
- Inattività live **NON** osservata: euristica conservativa, non flag/garanzia API. Nessuna garanzia che ogni inizio lavoro sia <5h o che la quota sia illimitata; il ping consuma comunque quota weekly anche se inutilizzato.
- Send effimero minimo ma non gratuito/a contesto zero: base system prompt CLI inevitabile, usage tiny. `--ignore-user-config` non disabilita `AGENTS.md` (isolato via `-C` tmpdir vuoto); `CODEX_HOME` dedicato deve restare privo di `AGENTS.md`/skill/MCP/config.
- Auth dedicata `codex login --device-auth` (store file in `CODEX_HOME` dedicato, mai copia del desktop); refresh persistito ma revoca/policy provider può richiedere re-login — mai garantire "one-time per sempre".
- Utente senza API key; nessun fallback automatico costoso (chiavi ereditate sanificate, `supportsLunaReserve=false`, nessun riscatto crediti).
- Stato/credenziali privati: nessun contenuto nei log, nessuna porta esposta. Dopo `login` verificare con `check` (l'heartbeat nasce solo col daemon).
- Setup dati con owner `65532:65532` (`install -d -m0700`, non `mkdir` host); stop daemon prima di `check`/`--once`/`login` per il lock lifetime.

## Case canoniche
- Tech/contratti/checksum → `project_core_tech.md`; layout → `project_structure.md`; operativo utente (IT) → `README.md` (senza Task ID, senza stato release).
- Stato/milestone → `project_progress.md`, diario → `project_diary.md`, handoff → `latest_session_work.md` (main-owned, **non toccati** da questo update).
