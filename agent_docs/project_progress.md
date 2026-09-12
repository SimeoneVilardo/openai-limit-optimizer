# Project Progress

## Deployment `openai_limit_optimizer_20260912` — release preparation

Updated: 2026-09-12. Route: Heavy, Go workers (Free quota exhausted).

- Implemented configurable Python daemon and pinned Codex 0.154.0 Docker runtime.
- Independent code acceptance: **132 tests passed** (78 unit, 54 acceptance), including real fake-process transport, cleanup, locking and no-send regressions.
- Homelab preflight image built successfully on amd64; release rebuild with latest repairs and checksum is pending.
- Git remote is public `SimeoneVilardo/openai-limit-optimizer`, target branch `master`; first commit/push pending.
- Image publication and deployment at `/home/simeone/openai-limit-optimizer` pending.
- Dedicated OpenAI device login is required before authenticated operation; desktop credentials must not be copied.

## Acceptance boundary

Live reads confirmed an active Codex window and model availability. Live inactive-window behavior and a conditional send have **not** been observed. The conservative moving-full-window detector is tested with fixtures, not an explicit upstream idle flag. No live inference was performed.

## Next milestone

Commit/push reviewed source; rebuild/push versioned image using homelab Docker and client-side registry credentials; deploy image-only Compose, perform dedicated login and independent runtime checks. Preserve fail-closed behavior if authentication or idle evidence is unavailable.
