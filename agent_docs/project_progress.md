# Project Progress

## Current delivery — source implementation accepted

Updated: 2026-09-14. Deployment `reset_target_recovery_20260914`, Heavy, **complete** for source implementation and independent automated verification. Earlier blocked records below are historical.

- Daily target scheduling, persistent runtime commands, timezone/DST behavior, conservative gating, Compose/env wiring and Docker timezone support are implemented. Contracts belong in `project_core_tech.md` and user operation in README.
- Final independent gate: `python -m unittest discover -s tests -t .` **190/190 passed**, preserving all 132 original tests. The 43 independent schedule tests also pass with ResourceWarning treated as error.
- Real daemon-loop tests with fake time/transport verify 03:00 → exact 04:30 activation for a 09:30 target with confirm30 and confirm600, live corrupt-override recovery, runtime edits, unchanged safety gates and bounded reset-boundary fresh-policy retry. No claim of observed live provider timing.
- Workflow timeout misclassification was diagnosed and repaired rather than abandoned. Independent dispatcher gate: **62/62**, typecheck and registration smoke passed. Canonical repair evidence: `/home/simeone/.config/opencode/agent_docs/latest_session_work.md`. Restart OpenCode to load that plugin repair; current app verification resumed successfully through the same stopped tester child after diagnosis.
- No image build, publication, service change, live inference, commit or push. Existing user work preserved. Next optional milestone: build/release/deploy the accepted source when requested, then observe a natural live idle/reset transition without relaxing policy.

## Deployment `reset_target_scheduling_20260914` — blocked at entry

Updated: 2026-09-14. Route: Heavy.

- Requested: multiple desired daily five-hour reset times, configurable through environment and runtime commands; no configured times preserves existing behavior. Example: idle at 03:00, desired reset 09:30, defer activation until 04:30 when feasible.
- Companion dispatch returned terminal `stop_unconfirmed` (`status_unavailable`, opencode tier); child stop could not be confirmed. No local retry or provider override attempted.
- Read all six framework documents; initial `git status --short` was clean. No production implementation, tests, runtime operations, or acceptance performed.
- Next milestone: restore dispatcher control and resolve child `ses_f611cc4c2ffe71n8Yw6tXiSBxR` before resuming Heavy intake, architecture, delegated implementation and independent verification. Scheduling semantics (timezone, overlapping/unreachable targets, runtime persistence) remain undecided.
- Previous delivery evidence below is historical, not reverified in this deployment.

## Deployment `openai_limit_optimizer_20260912` — complete delivery

Updated: 2026-09-12. Route: Heavy, Go workers (Free quota exhausted).

- Implemented configurable Python daemon and pinned Codex 0.154.0 Docker runtime.
- Independent code acceptance: **132 tests passed** (78 unit, 54 acceptance), including real fake-process transport, cleanup, locking and no-send regressions.
- Release image built on homelab amd64 with verified official binary checksum, published and independently checked.
- Source commit `53009be5f96e6fd661ff15ecc98c7d42e2e2a5bb` pushed to public `SimeoneVilardo/openai-limit-optimizer`, branch `master`.
- Docker Hub tags `0.1.0`, `sha-53009be`, `latest` share digest `sha256:e0bfd0a85154d808e7c38501a6c91481f7c5feba1efb4ab773b6d9aa940333b5`.
- Deployed image-only `/home/simeone/openai-limit-optimizer/docker-compose.yml` on `homelab.lan`, `.env` pins that digest. Daemon running **healthy** with 600-second polling, Luna/low and the requested prompt.
- User enabled device-code login in ChatGPT security settings and completed dedicated authentication. Persistent auth is private; desktop credentials were not copied.

## Acceptance boundary

Independent authenticated runtime verification confirmed active-window `resets-fixed` skip, healthy heartbeat, exact model/effort/configuration and no sends. Live inactive-window behavior and a conditional send have **not** been observed. The conservative moving-full-window detector is tested with fixtures, not an explicit upstream idle flag. No live inference was performed; long-term OAuth renewal remains untested.

## Follow-up validation

Observe the next natural idle transition and resulting conditional send without forcing generation or relaxing the safety gate. If the backend does not expose the moving-full-window pattern, the service will skip; investigate that evidence before changing policy. Delivery is complete, but optimization effectiveness is not yet proven live.
