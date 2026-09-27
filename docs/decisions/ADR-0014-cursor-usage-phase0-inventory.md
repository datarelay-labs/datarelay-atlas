# ADR-0014: Cursor usage Phase 0 inventory

Status: Accepted
Date: 2026-09-27

## Context

Issue #78 asks for the smallest read-only Atlas slice that shows which Cursor
workers are resident, which have active or recent inference, and where an
operator-supplied Cursor Usage Events CSV shows heavy turns. Engineering
System hook receipts, context compaction, and provider routing stay outside
this slice. Atlas must not stop a session, invoke `/clear`, or retain prompt,
transcript, environment, or credential text.

## Minimal design gate

1. **Goal** — Emit a content-free worker inventory and a deterministic
   usage-event summary, plus one warning recommendation.
2. **Non-goals** — Session cleanup, admission control, dependency-graph
   concurrency, model routing, hook recording, billing estimates, production
   deployment, and any mutation of Cursor or GitHub.
3. **Affected public contract** — `python3 -m atlas usage inventory`,
   `usage summarize --csv`, and `usage report`. JSON schema version 1.
   Worker states are only `RUNNING_AUTHORIZED`, `IDLE_REUSABLE`,
   `DUPLICATE_WORKTREE`, `TERMINAL_WORK_SURVIVOR`, and `ORPHAN_OR_UNKNOWN`.
   Recommendations are only `CONTINUE`, `SUMMARIZE_RECOMMENDED`,
   `CHECKPOINT_CLEAR_RECOMMENDED`, `YIELD_BUDGET`, and `HUMAN_REQUIRED`.
4. **State / migration impact** — No persisted schema. CSV and optional
   packet facts are read and discarded. Raw command lines are not stored.
5. **Security / operations impact** — Missing or conflicting process,
   identity, or packet evidence becomes `ORPHAN_OR_UNKNOWN`. Optional CSV
   columns stay null. Unknown CSV headers and mistyped cells fail closed.
   Cache-read ratio is descriptive and does not drive the recommendation.
   The commands do not signal, kill, or resume processes.
6. **Architecture boundary** — `atlas.cursor_usage` owns classification,
   CSV normalization, and the advisor. It reuses `parse_persist_list` and
   `PersistSession` from the work controller. `list_persist_trust_processes`
   stays the controller's resume-argv check and is not the inventory source.
   Host-worker resume descriptors stay unchanged.
7. **Acceptance / regression criteria** — Fixture tests cover resident
   versus unknown evidence, duplicate worktrees, CSV rejection, independent
   aggregates, and a CLI summarize path. A runnable persist process stays
   `inference_activity=UNKNOWN`. The live report command is read-only.

## Decision

Ship the three read-only commands from `atlas.cursor_usage`. Persist-list
rows are resident workers, not token spend. The host-proven restore argv is
`node --use-system-ca <index.js> --cursor-persist-restore <32 lowercase hex> <session id>`.
The hex token is discarded. The session id is kept only when it is that final
argument, matches the `cursor-…-<10 hex>-<1 hex>-<6 hex>` persist-list shape,
and is already present in `agent persist list`. Any other restore-shaped argv
is ambiguous and yields no session id. Process presence is runtime evidence only.
Scheduler busy/quiescent state is a separate `runtime` field. Without a
trusted generation lifecycle, `inference_activity` stays `UNKNOWN`. Usage
metrics come only from an operator-supplied CSV. The advisor reports a
warning and does not act.
