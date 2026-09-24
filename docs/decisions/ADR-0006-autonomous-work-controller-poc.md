# ADR-0006: Autonomous Work Controller PoC (v0)

Status: Accepted
Date: 2026-09-21

## Context

DRAtlas needs a smallest self-dogfood loop: persist one engineering workstream,
accept a Cursor completion event, run an independent OpenAI audit, and either
stop (`PASS` / `HUMAN_REQUIRED`) or dispatch a fresh Cursor `/work-resume`
iteration on `REWORK`. GitHub AI Work Packets remain canonical coordination
state; chat sessions are disposable.

Phase 1 already selected local filesystem JSON under `.atlas-data/` (ADR-0005).
`.engineering/project.yaml` still advertised `persistent_state: false`, which
conflicts with that durable boundary and with this controller's state file.

Installed Cursor CLI supports persistent session create via
`agent persist --force --trust <prompt>` when run under a PTY with cwd set to
the validated worktree. `--force` follows `persist` and is Run Everything, so a
fresh unattended session does not stop at shell approval. `agent --force persist`
is an unknown command. `--trust` still
trusts the workspace, and the prompt remains `/work-resume`. The incorrect argv
form `agent --workspace <path> --trust persist` does not create a prompted
session. Non-interactive `agent -p` / `--print` is not an acceptable silent
substitute for persistent/observable rework sessions. Built-in Cursor `/resume`
is not the Engineering System resume workflow; the canonical slash command is
`/work-resume` (`.cursor/commands/work-resume.md`). Before that spawn, the
dispatcher runs `tools/cursor-resource-preflight.py` from the checkout named by
`ENGINEERING_SYSTEM_ROOT`, or the file named by
`ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT`. Exit 0 `PASS` or `WARN` may
spawn. Any other result is `BLOCK`: no new session, and existing sessions are
not stopped.

## Minimal design gate

1. **Goal** — One local DRAtlas workstream can complete → audit → PASS/REWORK/
   HUMAN_REQUIRED with bounded automatic rework dispatch that starts a fresh
   observable Cursor session executing `/work-resume` without human keystrokes.
2. **Non-goals** — Temporal/n8n/multi-tenant orchestration; production UI;
   public webhooks; Phase 2 retrieval/MCP/Web; institutionalizing tmux as
   business state; replacing GitHub Work Packets; autonomous coding-agent
   replacement.
3. **Affected public contract** — Operator CLI under `python -m atlas work-controller …`,
   completion-event JSON fields, local completion inbox layout, controller
   state file layout, replaceable audit/dispatch/work-packet adapters,
   canonical `/work-resume` surface.
4. **State / migration impact** — Atlas-owned file
   `<data-root>/work-controller.json` with `schema_version: 1`, plus local
   `<data-root>/completion-inbox/` and `completion-processed/` for hook
   ingestion. Reconcile `operations.persistent_state: true` in
   `.engineering/project.yaml`.
5. **Security / operations impact** — No secrets in Git/state/events; no shell
   execution from Issue/event bodies; dispatch only through validated
   repository→worktree mappings and fixed command argv; Telegram is
   observational only; OpenAI credentials runtime-env only and never persisted.
6. **Architecture boundary** — Controller owns state transitions. Audit,
   Cursor session start, Work Packet mutation, and notifications are adapter
   ports. Canonical Engineering System resume command is `/work-resume`
   (`.cursor/commands/work-resume.md`); do not redefine `/resume` as canonical.
7. **Acceptance / regression criteria** — Deterministic tests for idempotency,
   stale/invalid HEAD rejection, PASS, REWORK dispatch argv, launcher isolation,
   OpenAI request/poll/verdict mapping, inbox drain, retry exhaustion, and
   restart reconciliation; operator dogfood runbook for first live self-test.

## Decision

1. **Reuse ADR-0005 data root** for controller state:
   `<data-root>/work-controller.json`.
2. **States**: `IDLE`, `AWAITING_AUDIT`, `AUDITING`, `REWORK_DISPATCHED`,
   `PASSED`, `HUMAN_REQUIRED`.
3. **Completion event** (machine-readable) must include at least:
   `event_id`, `workstream`, `issue_number`, `branch`, `head`, `attempt`.
4. **Local completion ingestion**: Cursor completion hooks write JSON into
   `<data-root>/completion-inbox/`; `drain-inbox` processes them. Telegram is
   not on the control path.
5. **Identity gate** before advance: registered workstream, matching branch,
   matching expected HEAD, attempt consistency; reject otherwise. Also verify
   exact local worktree identity (toplevel/origin/branch/HEAD) before audit or
   dispatch; detached HEAD fails closed.
6. **Idempotency**: duplicate `event_id` returns the prior outcome without
   re-auditing or re-dispatching.
7. **Audit port** returns normalized `PASS` | `REWORK` | `HUMAN_REQUIRED`
   (plus concise findings). **Default production auditor** is Codex CLI using
   the owner's ChatGPT-plan login. The controller gathers a deterministic
   evidence bundle (git, Work Packet, tests, CI) and invokes
   `codex exec -s read-only --ignore-user-config` with
   apps/browser/computer/shell/plugins/hooks/multi-agent disabled
   so Codex judges only that bundle. Codex local shell is not required. OpenAI
   Responses API remains an optional fallback adapter only and is not required
   for PoC PASS. `fixed` is explicit offline/test mode only and must never
   silently default production completions to PASS. Tests use deterministic
   fakes/transports. Credentials never enter durable state.
8. **REWORK**: update the same Work Packet via adapter (findings + next action),
   then dispatch a **fresh** Cursor session in the validated worktree using the
   fixed resume surface `/work-resume`. Never attach/reuse unrelated sessions.
9. **Cursor dispatch adapter (v0)**:
   - Native create argv: `agent persist --force --trust /work-resume` with
     `cwd=<validated-worktree>`, spawned under a PTY/`script` transport.
     `--force` follows `persist` and is Run Everything. A resource-preflight
     `BLOCK` or an unavailable preflight refuses the spawn.
   - Durable success requires a new `agent persist list` session for the target
     worktree. A target process is diagnostic only and is not dispatch success.
     Do not stop/attach/modify unrelated sessions.
   - PTY/tmux usage is transport only, not controller business state.
   - Do not fall back to `agent -p`.
10. **Retry bound**: configurable `max_attempts` (default 3). Exhaustion →
    `HUMAN_REQUIRED`.
11. **Restart reconciliation**: unfinished `AUDITING`/`AWAITING_AUDIT` fail
    closed to re-run the last accepted event once; unknown/corrupt schema
    versions fail closed.
12. **Telegram/notify** adapters are optional observers and must not gate
    transitions.
13. **ChatGPT Work** is optional and outside this PoC's completion criteria;
    general ChatGPT remains the human escalation path.

## Consequences

- Dogfood loop can be proven without new infrastructure dependencies.
- Persistence metadata matches ADR-0005 reality.
- `/work-resume` remains aligned with Engineering System resume semantics.
- Codex CLI (ChatGPT plan) is the default independent auditor; OpenAI API is
  optional only.
- Cursor launcher transport is explicit and testable without touching unrelated
  sessions.
- Later production orchestration (if any) must ADR a migration from this local
  schema rather than silently forking contracts.
