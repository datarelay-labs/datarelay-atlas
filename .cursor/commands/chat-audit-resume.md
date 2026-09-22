---
name: chat-audit-resume
description: Resume continuous Chat code audit from the durable Audit Control Packet
---
Resume one bounded continuous repository audit slice from durable GitHub/local checkpoint state.

Use maximum available reasoning/context.
Do not use parallel sub-agents.
Work sequentially in a single agent context.

A Chat conversation is an execution instance, not durable workflow state.
Do not invent audit progress from prior conversation text.

1. Verify local execution environment (shell/Git). If unusable, STOP with `ENVIRONMENT_BLOCKER`.
2. Verify local repository identity:
   - git rev-parse --show-toplevel
   - git remote get-url origin
   - git branch --show-current
   - git rev-parse HEAD
   - git status --short --branch
3. Read `AGENTS.md`, then `.engineering/project.yaml`. Load only additional Engineering System context required for this audit slice.
4. Load the durable Audit Control Packet for this repository:
   - Prefer the GitHub-backed Audit Control Packet when configured.
   - Otherwise use local `ATLAS_DATA_ROOT` / `.atlas-data/chat-audit.json` via:
     `python -m atlas chat-audit show`
5. Independently verify repository / branch / HEAD against the packet. Stale or mismatched identity fails closed.
6. Execute exactly one bounded audit slice:
   `python -m atlas chat-audit run-slice`
   or the equivalent controller path for the configured adapters.
7. Persist the checkpoint before the slice terminates. Never mark PASS from truncated/incomplete evidence.
8. If the slice produces an implementation finding, create/update the matching `[AI Work]` packet and stop cleanly. Do not modify product code from Chat. Cursor remains the implementation engine.
9. If session rollover is required, ensure the checkpoint is durable first; browser UI text is never canonical.
10. Stop after one slice unless the packet's next action explicitly requires an immediate machine-only continuation that does not expand scope.
