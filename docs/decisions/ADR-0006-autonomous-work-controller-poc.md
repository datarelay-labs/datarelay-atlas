# ADR-0006: Autonomous Work Controller PoC (v0)

Status: Accepted
Date: 2026-09-21

## Context

DRAtlas needs a smallest self-dogfood loop: persist one engineering workstream,
accept a Cursor completion event, run an independent OpenAI audit, and either
stop (`PASS` / `HUMAN_REQUIRED`) or dispatch a fresh Cursor `/resume` iteration
on `REWORK`. GitHub AI Work Packets remain canonical coordination state; chat
sessions are disposable.

Phase 1 already selected local filesystem JSON under `.atlas-data/` (ADR-0005).
`.engineering/project.yaml` still advertised `persistent_state: false`, which
conflicts with that durable boundary and with this controller's state file.

Installed Cursor CLI `2026.09.18-9a7762b` exposes `agent persist list|attach|stop`
and documents interactive `agent persist` start + `/detach`. Prompt-create forms
such as `agent persist /resume` are not a reliable create path on this build
(`agent help persist` lists only list/attach/stop). Non-interactive `agent -p`
is not an acceptable silent substitute for persistent/observable rework
sessions.

## Minimal design gate

1. **Goal** — One local DRAtlas workstream can complete → audit → PASS/REWORK/
   HUMAN_REQUIRED with bounded automatic rework dispatch.
2. **Non-goals** — Temporal/n8n/multi-tenant orchestration; production UI;
   public webhooks; Phase 2 retrieval/MCP/Web; institutionalizing tmux;
   replacing GitHub Work Packets; autonomous coding-agent replacement.
3. **Affected public contract** — Operator CLI under `python -m atlas work-controller …`,
   completion-event JSON fields, controller state file layout, replaceable
   audit/dispatch/work-packet adapters.
4. **State / migration impact** — New Atlas-owned file
   `<data-root>/work-controller.json` with `schema_version: 1`. Reconcile
   `operations.persistent_state: true` in `.engineering/project.yaml`.
5. **Security / operations impact** — No secrets in Git/state/events; no shell
   execution from Issue/event bodies; dispatch only through validated
   repository→worktree mappings and fixed command argv; Telegram is
   observational only; OpenAI credentials runtime-env only.
6. **Architecture boundary** — Controller owns state transitions. Audit,
   Cursor session start, Work Packet mutation, and notifications are adapter
   ports. Canonical Engineering System resume command remains `/resume`
   (`.cursor/commands/resume.md`); do not invent a divergent slash command.
7. **Acceptance / regression criteria** — Deterministic tests for idempotency,
   stale/invalid HEAD rejection, PASS, REWORK dispatch, retry exhaustion, and
   restart reconciliation; operator dogfood runbook for first live self-test.

## Decision

1. **Reuse ADR-0005 data root** for controller state:
   `<data-root>/work-controller.json`.
2. **States**: `IDLE`, `AWAITING_AUDIT`, `AUDITING`, `REWORK_DISPATCHED`,
   `PASSED`, `HUMAN_REQUIRED`.
3. **Completion event** (machine-readable) must include at least:
   `event_id`, `workstream`, `issue_number`, `branch`, `head`, `attempt`.
4. **Identity gate** before advance: registered workstream, matching branch,
   matching expected HEAD, attempt consistency; reject otherwise.
5. **Idempotency**: duplicate `event_id` returns the prior outcome without
   re-auditing or re-dispatching.
6. **Audit port** returns normalized `PASS` | `REWORK` | `HUMAN_REQUIRED`
   (plus concise findings). Production uses an OpenAI HTTP adapter; tests use
   deterministic fakes. Credentials never enter durable state.
7. **REWORK**: update the same Work Packet via adapter (findings + next action),
   then dispatch a **fresh** Cursor session in the validated worktree using the
   fixed resume surface `/resume`. Never attach/reuse unrelated sessions.
8. **Cursor dispatch adapter (v0)**:
   - Preferred long-term native mechanism: interactive `agent persist` in the
     validated worktree (survives disconnect; manage with list/attach/stop).
   - Documented CLI drift: prompt-create via `agent persist <prompt>` is not
     reliable on the pinned CLI; do not silently fall back to `agent -p`.
   - v0 default implementation records a planned argv and delegates process
     spawn to an injected runner so tests stay deterministic; operators follow
     the runbook for the live persist attach path until Cursor restores a
     stable create-with-prompt contract.
9. **Retry bound**: configurable `max_attempts` (default 3). Exhaustion →
   `HUMAN_REQUIRED`.
10. **Restart reconciliation**: unfinished `AUDITING`/`AWAITING_AUDIT` fail
    closed to re-run the last accepted event once; unknown/corrupt schema
    versions fail closed.
11. **Telegram/notify** adapters are optional observers and must not gate
    transitions.

## Consequences

- Dogfood loop can be proven without new infrastructure dependencies.
- Persistence metadata matches ADR-0005 reality.
- Cursor CLI adapter drift is explicit; tmux remains a bootstrap exception only,
  not a product dependency.
- Later production orchestration (if any) must ADR a migration from this local
  schema rather than silently forking contracts.
