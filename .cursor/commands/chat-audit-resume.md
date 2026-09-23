---
name: chat-audit-resume
description: Resume continuous Chat code audit from the durable Audit Control Packet
---
Resume one bounded continuous repository audit slice from durable GitHub checkpoint state.

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
4. Load the durable Audit Control Packet for this repository from GitHub
   Contents API blob-SHA CAS (Issue number keys the path; Issue body is not
   the mutation surface):
   - `python -m atlas chat-audit show --repository <owner/repo> --checkpoint-issue <N> --worktree "$PWD"`
   - Local `ATLAS_DATA_ROOT` / `.atlas-data/chat-audit.json` is cache only.
5. Independently verify repository / branch / HEAD against the packet. Stale or mismatched identity fails closed. Exact 40-char SHAs only.
6. Execute exactly one bounded audit slice:
   `python -m atlas chat-audit run-slice --repository <owner/repo> --checkpoint-issue <N> --worktree "$PWD"`
   or the equivalent controller path for the configured adapters.
7. Persist the GitHub checkpoint before the slice terminates. Never mark PASS from truncated/incomplete evidence.
8. If the slice produces an implementation finding, create/update the matching GitHub `[AI Work]` packet and stop cleanly. Local handoff JSON is not success. Do not modify product code from Chat. Cursor remains the implementation engine.
9. If session rollover is required, ensure the checkpoint is durable on GitHub first; browser UI text is never canonical.
10. Stop after one slice unless the packet's next action explicitly requires an immediate machine-only continuation that does not expand scope.
