# Project Progress

## Current delivery — released and deployed

Updated: 2026-09-15. Deployment `reset_target_release_20260915`, **complete**. Earlier source-only and blocked records below are historical.

- Source commit `b97486f80edb60ed54bcdefa4e13179dcda7a49b` is pushed to public `SimeoneVilardo/openai-limit-optimizer`, branch `master`.
- Final independent gate: `python -m unittest discover -s tests -v` **190/190 passed**, with no release-blocking finding.
- Docker Hub tags `latest` and `sha-b97486f` share digest `sha256:375d54eb8497fc3c23a36e0199e42feeda87e971d199045e7010910174d2e550`; the image carries the full source revision label.
- `/home/simeone/openai-limit-optimizer/docker-compose.yml` on `homelab.lan` pins that digest. The recreated daemon is healthy, non-root, read-only, has no published ports and retains the private `/data` bind mount.
- Persistent runtime schedule override is `09:30` in `Europe/Rome`. At verification the current cooldown made the 2026-09-15 activation unreachable, so the conservative planner selected the 2026-09-16 09:30 target (04:30 local activation) rather than forcing or advancing an attempt.
- No live inference was forced. Exact provider reset timing remains unguaranteed; observe the next natural idle transition without relaxing policy.

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
