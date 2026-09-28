# ADR-0015: Prod usage control-plane read-only reconciliation

Status: Accepted
Date: 2026-09-28

## Context

Issue #80 extends ADR-0014 after Engineering System context-epoch semantics became
canonical. Phase 0 can observe resident Cursor workers and provider Usage Events
CSV, but it cannot distinguish a completed resident worker from an active one
without canonical Work Packet state, and prod-atlas cannot centrally consume
content-free observations from approved development hosts.

The first production dogfood slice stays read-only. GitHub remains canonical for
Work Packet and PR lifecycle, Engineering System owns context-epoch semantics,
and provider Usage Events CSV remains authoritative for billed/token facts.
Atlas must not infer per-session billing, execute remote host actions, or retain
prompt, transcript, tool payload, environment, or credential content.

## Minimal design gate

1. **Goal** — Produce a concise operator report that combines bounded worker
   snapshots, exact GitHub lifecycle facts, bounded context-epoch advice, and
   optional provider usage facts without performing a control action.
2. **Non-goals** — Remote command execution, session stop/kill, automatic context
   clearing or summarization, model/provider switching, billing attribution,
   browser-cookie scraping, and a new monitoring/telemetry stack.
3. **Public contract** — `atlas usage inventory` may emit a host-labelled
   content-free snapshot. `atlas usage report` may import repeated
   `--worker-snapshot` files, use `--github-reconcile`, explicitly add
   `--repository` targets, and consume one `--context-facts` JSON document.
   Existing CSV summarization remains unchanged.
4. **Authority and reconciliation** — Only Work Packets authored by a GitHub
   collaborator with write/maintain/admin permission can become canonical facts.
   Repository, branch, packet status, issue open/closed state, LAST_VERIFIED_HEAD,
   PR state, and exact PR HEAD are reconciled. Contradiction, stale exact-head
   evidence, ambiguity, or malformed metadata never becomes an assumed match.
5. **Snapshot contract** — Snapshot schema version 1 is JSON-only, bounded to
   1 MiB and 100 workers, requires a bounded host identifier and timezone-aware
   observation timestamp, and accepts only the published content-free worker
   fields. Imported `inference_activity` must remain `UNKNOWN`.
6. **Context contract** — Atlas accepts only the allowlisted provider-neutral
   context-epoch fact keys. Unknown fields, including raw content-like fields,
   fail closed. Engineering System decides the epoch action; Atlas maps CLEAR to
   `CLEAR_RECOMMENDED` and never executes it.
7. **Recommendation precedence** — Current worker ambiguity, duplicate worktrees,
   or terminal resident work requires `HUMAN_REQUIRED` and overrides context
   advice. Historical usage/burst values remain descriptive and never select a
   control action.
8. **State and operations** — This slice adds no persisted Atlas schema. Snapshot,
   GitHub, context, and usage inputs are read and discarded. Output carries the
   current observation time and imported snapshot observation times so evidence
   freshness is visible. GitHub reconciliation is read-only.
9. **Output compactness** — The report summarizes total canonical/noncanonical
   observations but returns detailed reconciliation rows only for open packets
   and branches relevant to observed workers. Historical unrelated rows do not
   dominate the operator surface.
10. **Acceptance** — Focused and full tests must prove strict snapshot validation,
    exact-head GitHub reconciliation, context-field rejection, recommendation
    precedence, no destructive session primitive, and deterministic central
    aggregation. Production completion also requires snapshots from at least two
    approved development hosts, exact-head CI, independent audit, and prod smoke.

## Decision

Use Atlas's existing CLI, Work Packet adapter, and Phase 0 worker classifier as
the control-plane boundary. Development hosts export content-free snapshots;
prod-atlas imports them and re-evaluates lifecycle against current GitHub facts.
No SSH, Remote Desktop, Cursor control, or provider-private endpoint capability
is added to the product.

A resident process remains only residency/runtime evidence. Busy scheduler state
does not prove active inference. Imported snapshots cannot upgrade that claim.
GitHub exact-head mismatch is uncertainty, not a best-effort alias. Context
pressure can recommend checkpoint, summarize, or clear, but Atlas remains a
warning-only observer in this slice.
