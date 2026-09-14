# Project Overview — openai-limit-optimizer

Task ID: `reset_target_release_20260915` (prior scheduling/recovery records are historical and superseded).

## Stato verificato da produzione (2026-09-15)
- Repo: `/home/simeone/dev/open-ai-limit-optimizer`, branch `master`, remote `origin git@github.com:SimeoneVilardo/openai-limit-optimizer.git`.
- Implementato: daemon Python stdlib-only v0.1.0 (`app/`), `Dockerfile` + `compose.yaml` image-only, `config/example.env`, test `tests/unit` + `tests/acceptance`.
- Comportamento: poll conservativo della finestra Codex 5h; invia `OLO_PROMPT` via sessione effimera **solo** su segnale moving-full-window a due letture. Default in `app/config.py`: poll 600s, confirm 30s, model `gpt-5.6-luna`, effort `low`, prompt `Answer only with "hi"`, cooldown 18000s.
- Test: `python -m unittest discover -s tests -v` → **190 OK** (132 original preserved: 78 unit + 54 acceptance; + 15 executor `tests/unit/test_schedule.py`; + 43 independent schedule: 17 reset + 13 regression + 13 loop). Final independent release gate completed on 2026-09-15. No live inference forced.
- Causa missing-home dello smoke preflight risolta in sorgente (`protocol.ensure_codex_home` condiviso daemon/check/login). Release/commit/digest/deploy: canonici in `project_progress.md`.
- Stato operativo (build/push/deploy): canonico in `project_progress.md` (main-owned), non duplicato qui né in README.

## Reset schedule (published and deployed)
- Env `OLO_RESET_TIMES` (comma strict `HH:MM`, empty disables; es. `09:30,14:30`), `OLO_RESET_TIMEZONE` (IANA, es. `Europe/Rome`; default UTC), `OLO_SCHEDULE_FILE` (default beside journal; Compose fixed `/data/schedule.json`).
- Target `R` => attivazione `R-18000` elapsed UTC s; esempio idle 03:00/target 09:30 => wait fino 04:30 esatto (fake wall+mono loop, poll 600, confirm 30 e 600). Earliest reachable, intermediate send solo se cooldown intero prima di A, grace 60s mai early. Vuoto => scheduling standard invariato (schedule gate pass-through).
- Runtime `schedule show/set/clear/reset` (daemon running ammesso; esempio preferito `schedule set 09:30 14:30 --timezone Europe/Rome`): override persistente con short schedule lock separato (mai journal lifetime lock), precede env, sopravvive restart; `clear` = disable persistente, `reset` = ripristina env; `show` legge journal read-only senza lock lifetime (solo short schedule lock), mai auth/send; corrupt override fail closed con recovery via set/clear/reset senza leggere il payload corrotto.
- Timezone/DST: regole IANA, gap saltati, fold prima occorrenza. Reload file su idle-loop waits entro 5s (non garantito durante RPC bloccante/confirm); reread pre-attempt vincolante. Schedule veta soltanto: policy conservativa, journal cooldown e gate restano invariati. `check` espone campi `schedule_*` e `effective_allow = allow AND cooldown==0 AND schedule.allow`.
- Boundary retry limitato: una sola fresh policy pair (mai inference/send retry) solo se denial boundary-due originale, stesso schedule/occurrence validi, cooldown+confirm dentro grace 60s; denial intermedio fixed-active ritorna dopo 30s/due letture, mai attende ore. Reset backend live esatto non garantito.
- Stato: feature pubblicata da commit `b97486f` e deployata sull'homelab al digest canonico in `project_progress.md`; override persistente live `09:30 Europe/Rome`. Nessun claim su reset backend live esatto. Dettagli/test provenance: README (utente) + `latest_session_work.md`/`project_progress.md` (main-owned).

## Policy (sintesi; canonico in `project_core_tech.md`)
- Doppia lettura separata da `OLO_CONFIRM_SECONDS` (default 30s); entrambe finestre fresche `resetsAt-wall ∈ 18000±5s`, `resetsAt` deve avanzare con l'elapsed (`|dResets-dWall|≤5s`), orologi wall/mono d'accordo, `ordinaryUsageAllowed==True`, bucket `codex` 300min/0%, nessun esaurimento weekly/spend/reached.
- `check` riporta `effective_allow = allow AND cooldown==0 AND schedule.allow` senza scrivere il journal.
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
