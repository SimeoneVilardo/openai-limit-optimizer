# Project Diary

## 2026-09-15 — schedule release and homelab activation

- Re-ran an independent 190-test release gate before publication; no blocking defects were found.
- Pushed source revision `b97486f` to `master`, built the amd64 image on the homelab Docker Engine and published immutable `sha-b97486f` plus `latest` at digest `sha256:375d54eb8497fc3c23a36e0199e42feeda87e971d199045e7010910174d2e550`.
- Updated the existing image-only Compose deployment while preserving private auth/journal data. A first pin rewrite incorrectly let Perl interpret `@sha256`; Compose rejected the resulting nonexistent tag before container recreation. Escaping `@` fixed the pin and the controlled retry completed.
- Set the persistent runtime override to `09:30 Europe/Rome`. Live verification confirmed healthy daemon, exact revision/digest, non-root read-only confinement, no ports and secure schedule/state permissions.
- The pre-existing cooldown made today's activation miss the 60-second grace, so selecting tomorrow's target was the expected fail-closed result. No inference was forced to manufacture acceptance evidence.

## 2026-09-14 — recovery completed and full-loop acceptance

- Treat terminal workflow errors as evidence to investigate, not an excuse to abandon the user's feature. Sanitized logs identified a provider-header timeout misclassified as unknown; a separately delegated repair and independent gate enabled controlled same-child continuation. Workflow implementation details are canonical in the global OpenCode framework.
- Require full daemon-loop fake-clock tests for wakeup/timing claims. Isolated `_cycle` calls at chosen timestamps did not prove the poll600 wake path; `once=True` did not exercise corrupt-start recovery across iterations. Exact-boundary assertions must not allow an early send.
- A preconfirmation straddling an active→idle reset can correctly fail policy. Permit one fresh pair when it fits the target grace, retaining all safety checks; never retry an inference attempt. Bound that retry to the original boundary plan so ordinary intermediate denials cannot hold the client until a target hours away.
- Final independent suite is green (190 total). Automated fake-client evidence does not establish live backend idle semantics or exact reset timing. Source acceptance is separate from image release/deployment and from activating a changed OpenCode plugin.

## 2026-09-14 — scheduling implementation and acceptance hold

- Keep reset scheduling separate from the conservative upstream idle detector and durable attempt journal: scheduling can veto an attempt, never authorize one past the safety gates. Intermediate attempts are useful only if their full configured cooldown fits before the next activation.
- Runtime overrides need a separate lock/file so commands can operate without stopping the daemon or interfering with OAuth/journal locking. `clear` persistently disables targets; `reset` restores environment configuration. Read-only journal inspection for `schedule show` is permitted to make feasibility reporting useful.
- Local target occurrences use IANA rules; subtract five hours in UTC elapsed seconds. Skip nonexistent local times and use only the first ambiguous occurrence. Timing remains best effort, not a backend reset guarantee.
- Planner tests and mocked policy acceptance missed integration defects: corrupt-start initialization, failure heartbeat masking, stale check/dry-run decisions, confirmation timing, excessive target enumeration and filesystem aliases. Executor repaired these; final independent regression coverage is still required. Do not equate a green preexisting suite with verified repairs.
- Terminal tester dispatcher `unknown` stopped the worker (confirmed). Preserve work and block acceptance instead of locally retrying providers or substituting main-agent testing on Heavy.

## 2026-09-14 — reset scheduling entry blocked

- Heavy Companion launch failed with terminal dispatcher control error `stop_unconfirmed`, not a confirmed provider quota failure. Do not retry locally, override the fallback chain, or substitute main-agent production/testing for required delegation.
- Feature request retained in progress/handoff; no scheduling design or implementation accepted. Existing conservative send gates and durable cooldown must remain constraints when work resumes.

## 2026-09-12 — conservative automation design

- Greenfield repository; initialized the six-document framework before implementation.
- Codex `account/rateLimits/read` obtains usage without inference. Its percentage is rounded and there is no explicit inactive flag. Rejected `0%` alone, a rounded five-hour display, and expired/null timestamps as sufficient send conditions.
- Selected two full-window readings with a moving reset, backend permission for included usage, strict schema validation and a durable pre-attempt five-hour cooldown. This favors missed opportunities over unwanted sends; live idle semantics remain unverified.
- Use a dedicated persistent OAuth session rather than copying desktop refresh tokens; independent runners must not race token rotation. Provider revocation may still require re-login.
- Integration review caught gaps that initial mock tests missed: ineffective command locking, missing device-login flag, unsupported-effort validation, blocking/unsafe process IO, misleading health, permissive journal/schema parsing and container startup. Added real subprocess acceptance tests and focused repairs before release.
- An empty bind mount hides image-created `/data/codex`; startup now creates it privately before read-only authentication checks.
- Local Docker socket is unavailable to the user. Build on homelab through `DOCKER_HOST=ssh://homelab.lan`, preserving SSH verification and using registry credentials from the local client without copying credential stores.
- Device-code login requires enabling the corresponding ChatGPT security setting first. User hit this explicit provider error, enabled it and successfully authenticated; document the prerequisite before the command.
- Final acceptance separates delivered/running service from unobserved optimization effectiveness: verified authenticated active-window skip, never forced an inference to make an end-to-end test appear complete.
