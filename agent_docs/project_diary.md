# Project Diary

## 2026-09-12 — conservative automation design

- Greenfield repository; initialized the six-document framework before implementation.
- Codex `account/rateLimits/read` obtains usage without inference. Its percentage is rounded and there is no explicit inactive flag. Rejected `0%` alone, a rounded five-hour display, and expired/null timestamps as sufficient send conditions.
- Selected two full-window readings with a moving reset, backend permission for included usage, strict schema validation and a durable pre-attempt five-hour cooldown. This favors missed opportunities over unwanted sends; live idle semantics remain unverified.
- Use a dedicated persistent OAuth session rather than copying desktop refresh tokens; independent runners must not race token rotation. Provider revocation may still require re-login.
- Integration review caught gaps that initial mock tests missed: ineffective command locking, missing device-login flag, unsupported-effort validation, blocking/unsafe process IO, misleading health, permissive journal/schema parsing and container startup. Added real subprocess acceptance tests and focused repairs before release.
- An empty bind mount hides image-created `/data/codex`; startup now creates it privately before read-only authentication checks.
- Local Docker socket is unavailable to the user. Build on homelab through `DOCKER_HOST=ssh://homelab.lan`, preserving SSH verification and using registry credentials from the local client without copying credential stores.
