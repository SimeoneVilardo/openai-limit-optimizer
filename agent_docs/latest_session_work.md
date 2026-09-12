# Latest Session Work

Deployment: `openai_limit_optimizer_20260912`. Updated: 2026-09-12.

## Verified final handoff

- Code: `app/` modular configuration, JSONL Codex transport, strict policy, durable journal, ephemeral send and CLI; Dockerfile with official Codex asset checksum; image-only `compose.yaml`; Italian README.
- Independent command: `python -m unittest discover -s tests -t .` — **132 passed**, 78 unit and 54 acceptance. No live model generation.
- Acceptance includes active/fixed/ambiguous windows skipping, moving-full-window fixtures, auth/model/effort gates, malformed inputs, restart cooldown, journal corruption, lock exclusion, streamed fake subprocess IO, cancellation and orphan process cleanup.
- Read-only live discovery: Codex 0.154.0, ChatGPT login present on desktop, active five-hour window, `gpt-5.6-luna` available. Desktop auth remains untouched by deployment.
- Ops rebuilt the committed code with pinned asset checksum, pushed all three image tags and deployed the digest recorded in `project_progress.md`. Independent verification matched all seven app source hashes, image revision, nonroot user, read-only root, private data, no ports/socket, and restart policy.
- User completed dedicated device login after enabling ChatGPT security's device-code authorization. Auth file stat verified 0600, owner 65532:65532; directory 0700. No credential contents were read or copied to the repo/image.
- Post-login check (daemon stopped): `{"ok":true,"allow":false,"effective_allow":false,"reason":"resets-fixed","cooldown_remaining_s":0}`. Then ops started the daemon; independent tester confirmed healthy heartbeat and active-window skip, no send/would-send.
- Runtime defaults verified: polling 600s, confirmation 30s, cooldown 18000s, model `gpt-5.6-luna`, effort `low`, prompt `Answer only with "hi"`.

## Worker continuation lanes

- Companion: `ses_f68ea71c5ffeg1l5KPYmXZlfGG`.
- App executor: `ses_f68e774a7ffehPfPUqdfxSYBX3`.
- Operations executor: `ses_f68e8c9b8ffe0Gk7OoS6zqowgv`.
- Tester: `ses_f68dffa48ffeccoVrgVqae0T45`.
- Archivist: `ses_f68e9e48effepcE8tUnpRAoyjf`.

## Continuation

Main owns Git and the three state documents; source release is pushed and runtime verified. Closure documentation is committed separately from the image's source revision. No production workers remain active. Next meaningful validation is the natural inactive-window transition/send and long-term OAuth renewal; neither is verified and neither must be claimed. Stop the daemon before invoking check/login because it owns the state lock for its full lifetime. Keep the state journal intact across upgrades.
