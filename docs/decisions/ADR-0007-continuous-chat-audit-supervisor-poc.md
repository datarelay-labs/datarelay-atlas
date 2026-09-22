# ADR-0007: Continuous Chat Audit & Session Supervisor PoC (v0)

Status: Accepted
Date: 2026-09-22

## Context

DRAtlas needs ChatGPT Chat mode as a resumable continuous code-audit control
plane. Chat conversations time out and roll over; they must not be durable
workflow state. GitHub remains canonical. Issue #20 is the ACTIVE Work Packet
for this PoC. ChatGPT Work and Stagehand browser automation are optional and
must not become core dependencies.

ADR-0005 selected local filesystem JSON under `.atlas-data/`. ADR-0006 owns the
Cursor completion → independent audit → rework loop. This ADR adds a separate
Chat-mode-first continuous-audit planner whose durable checkpoint can be
resumed by a fresh Chat with only a stable resume instruction.

## Minimal design gate

1. **Goal** — Prove continuous repository auditing survives Chat timeouts and
   conversation rollover via a durable Audit Control Packet, bounded delta-first
   slices, fail-closed evidence rules, and an optional browser-managed rollover
   path that never treats UI text as canonical truth.
2. **Non-goals** — Generic enterprise scheduler; replacing GitHub/CI/tests;
   indefinitely growing Chat; Stagehand/browser as a required core dependency;
   product-code implementation by Chat; automatic merge; OpenAI API key on the
   default path.
3. **Affected public contract** — Audit Control Packet schema; operator CLI
   under `python -m atlas chat-audit …`; stable Chat resume instruction
   (`.cursor/commands/chat-audit-resume.md`); session-supervisor states;
   replaceable checkpoint / unit-executor / handoff / rollover adapter ports.
4. **State / migration impact** — Atlas-owned file
   `<data-root>/chat-audit.json` with `schema_version: 1` is derived cache /
   offline-test evidence only. Canonical coordination for live Chat resume is
   the GitHub-backed Audit Control Packet (Issue body markers via adapter).
   Finding handoff success requires GitHub `[AI Work]` create/update; local
   JSON is not a canonical success signal. Unsupported schema versions fail
   closed.
5. **Security / operations impact** — No secrets in checkpoints/Git; no shell
   execution from packet bodies; truncated/incomplete evidence cannot PASS;
   stale HEAD/run-key mismatches fail closed; Stagehand remains gated on Issue
   #19 go/no-go.
6. **Architecture boundary** — Chat Audit Controller owns slice planning,
   checkpoint transitions, and session-supervisor states. Unit execution,
   GitHub packet I/O, implementation handoff, and browser rollover are adapter
   ports. AWC (ADR-0006) remains the Cursor completion/rework loop and is not
   redefined here.
7. **Acceptance / regression criteria** — Deterministic tests for first-run
   init, delta-only second run, no-change idempotency, timeout resume,
   duplicate-invocation safety, stale HEAD fail-closed, truncated evidence
   reject, finding handoff, resume-from-packet-only, fake rollover without
   mutating audit truth, and absence of hard Stagehand dependency.

## Decision

1. **Audit Control Packet** is the durable audit checkpoint. Minimum fields:
   `target_repository`, `target_branch`, `last_audited_sha`,
   `current_target_sha`, `audit_status`, ordered `audit_queue`,
   `current_unit` / cursor, `open_findings`, `next_action`,
   `last_completed_slice`, `idempotency_run_key`, plus session supervisor
   state. Schema version is explicit.
2. **Default audit mode is delta-first**: scope is
   `last_audited_sha..current_target_sha`. When HEADs match, take a cheap
   no-change path. Full-repo audit is an explicit mode only.
3. **One bounded slice per invocation**: initialize or load checkpoint; verify
   repository/branch/HEAD; select exactly one audit unit; persist before the
   slice terminates; never PASS from incomplete/truncated evidence.
4. **Initial audit units** (ordered): `changed_code`, `affected_contracts`,
   `affected_tests_ci`, `security_impact`, `docs_spec_drift`, and
   `release_readiness` only when explicitly requested.
5. **Statuses**: `IDLE`, `IN_SLICE`, `AWAITING_EVIDENCE`, `SLICE_COMPLETE`,
   `PASSED`, `FINDINGS`, `FAILED_CLOSED`. Session states: `ACTIVE`,
   `STALLED`, `TIMEOUT`, `ROLLOVER_REQUIRED`, `RESUMED`.
6. **Idempotency**: duplicate invocations with the same
   `idempotency_run_key` + unit + target SHA return the prior outcome without
   double-advancing the queue.
7. **Finding handoff**: open findings create/update a GitHub `[AI Work]` Issue
   via adapter (idempotent by finding_id marker); Chat must not modify product
   code. Local handoff JSON is offline/test cache only and is not success.
8. **Session supervisor** is browser-provider-independent. A fake/recording
   rollover provider proves `ROLLOVER_REQUIRED → RESUMED` while leaving
   canonical audit fields unchanged except session state. Stagehand is an
   optional future adapter gated on Issue #19.
9. **Stable resume instruction** is `.cursor/commands/chat-audit-resume.md`
   (`/chat-audit-resume`). A fresh Chat loads only GitHub/local checkpoint
   state; conversation history is non-canonical.
10. **Reuse ADR-0005 data root** for the local derived checkpoint cache
    `chat-audit.json`. Live Chat coordination mirrors the same schema through
    the GitHub Issue adapter without forking field semantics.

## Consequences

- Continuous Chat audit can dogfood without OpenAI API keys or browser
  automation on the default path.
- Timeout/rollover becomes an explicit state transition rather than lost chat
  context.
- Later Stagehand or ChatGPT Work integrations must attach as adapters and
  must not redefine checkpoint semantics.
- AWC remains responsible for Cursor implementation rework; this PoC audits
  and hands off, it does not implement product code.
