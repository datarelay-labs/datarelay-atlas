# Readiness Single-Effect Authorization v1

Status: #53 single-effect safety gate
Effect scope: exactly one future work activation/dispatch intent
Execution authority: none

Machine-checkable schema:
[`readiness-single-effect-authorization.schema.json`](readiness-single-effect-authorization.schema.json)

Example:
[`fixtures/readiness-single-effect-authorization.example.json`](fixtures/readiness-single-effect-authorization.example.json)

## Purpose

This contract sits between read-only readiness planning and any future effect
path. It emits `ALLOW` or `DENY` for one bounded effect intent but never
performs GitHub mutation, Cursor/session action, model/provider routing, or
dispatch.

An `ALLOW` is observation evidence only. A future mutator must recompute this
authorization at its own effect boundary; retaining or replaying an older
authorization does not grant authority.

## Authorization gate

The GitHub authorization path:

1. reads the bounded readiness graph;
2. refuses authorization immediately when `max_wip != 1`;
3. performs two independent fresh GitHub-reconciled readiness plans;
4. requires those two complete plan objects to be identical;
5. requires `graph_state=READY`;
6. derives active occupancy from `ACTIVE` plan nodes when the graph is `READY`,
   requires it to match `active_count`, validates `available_slots` and selected-node
   count, and rejects any unselected node still marked `READY`;
7. requires exactly one selected node;
8. requires that selected node to still be `READY` and selected;
9. emits only the selected node's content-free provenance and a SHA-256 digest
   of the canonical stable plan.

The selected provenance is limited to:
- node id;
- repository;
- issue number;
- branch;
- exact 40-hex HEAD.

No Work Packet body/title, prompt, transcript, tool output, credential, resource
detail, or model/provider fact crosses this authorization boundary.

## DENY behavior

DENY is fail-closed. Stable plans include a plan digest when one is available.
No selected-node provenance is returned for DENY.

Representative reasons:
- `MAX_WIP_NOT_ONE`;
- `RECONCILIATION_ERROR`;
- `PLAN_CHANGED_BETWEEN_READS`;
- `PLAN_INVALID`;
- `GRAPH_NOT_READY` plus stable graph reason tokens;
- `SELECTED_NODE_COUNT_NOT_ONE`.

Owner/HUMAN_REQUIRED, dependency, resource, trust, and lifecycle gates therefore
cannot be bypassed by this layer.

## CLI

```bash
PYTHONPATH=. python3 -m atlas readiness github-authorize --graph graph.json
```

This command is read-only. It does not activate, resume, stop, dispatch, edit,
or otherwise mutate the selected work.

## Non-goals

- multi-worker effect authorization (#58);
- automatic activation or dispatch;
- stored/replayable capability tokens;
- provider/model routing;
- quota or billing inference;
- replacement of the canonical Work Packet or AWC safety boundaries.
