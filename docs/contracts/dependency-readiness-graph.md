# Dependency / Readiness Graph v1

Status: #53 Slice A read-only planner
Authority: canonical GitHub/Engineering System state must be normalized before input

Machine-checkable schema:
[`dependency-readiness-graph.schema.json`](dependency-readiness-graph.schema.json)

Example fixture:
[`fixtures/dependency-readiness-graph.example.json`](fixtures/dependency-readiness-graph.example.json)

## Purpose

This contract lets Atlas answer which bounded Work Packet nodes are ready without
mutating GitHub or dispatching Cursor. It replaces neither canonical Work Packet
state nor the existing AWC safety gates.

The planner consumes normalized machine facts only. Issue bodies, prompts,
transcripts, tool output, provider billing data, and model conversation state
are not graph inputs.
## Node facts

Each node supplies:
- stable `node_id` and GitHub issue number;
- repository, Git-ref-safe branch, and exact 40-hex HEAD;
- packet status and queue state;
- typed dependency edges (`node_id` + v1 relation `REQUIRES_COMPLETE`);
- explicit resource keys;
- authority state;
- owner/HUMAN_REQUIRED gates;
- deterministic numeric priority.

Atlas also derives an implicit `repo_branch:<repository>@<branch>` resource,
so two nodes cannot be selected concurrently for the same branch even when the
caller omits a custom resource key.

## Readiness rules

- `COMPLETE`: trusted completed node with no queue contradiction.
- `ACTIVE`: trusted current active node; it consumes WIP and resources.
- `READY`: queued PAUSED node whose dependencies are all COMPLETE and whose
  trust/owner/human/resource gates pass.
- `BLOCKED`: dependency, resource, packet, or WIP limit prevents selection.
- `HUMAN_REQUIRED`: stale/ambiguous/untrusted authority, unknown dependency,
  cycle, owner gate, explicit human gate, or contradictory active lifecycle.
An ACTIVE resource conflict or ACTIVE WIP count above `max_wip` makes the
whole plan HUMAN_REQUIRED and selects no new node. Cycles, any non-TRUSTED
authority state, and unknown dependencies also stop all new selection because
the planner cannot prove the graph/resource picture is complete. Known local
owner/HUMAN_REQUIRED gates block their own node; they do not authorize it.

Within a healthy graph, candidates are ordered by:
1. lower numeric priority;
2. lexical `node_id`.

Selection reserves each node's resources immediately, so same-batch conflicts
serialize deterministically. Independent nodes may be selected together only
while explicit WIP slots remain.

## Plan provenance

Every output node preserves the input `repository`, `branch`, and exact `head`
alongside `node_id` and `issue_number`. This keeps multi-repository plans
attributable even when different repositories use the same issue number.
Titles, issue bodies, source text, and conversation content are not emitted.

## GitHub reconciliation

The optional GitHub reconciliation gate re-reads each graph node's bounded
canonical Work Packet identity before planning:

- repository and issue number;
- branch and exact 40-hex HEAD;
- packet status and queue state.

Only trusted `[AI Work]` packet authors may produce canonical facts. Exact
matches remain eligible for normal planning. Lifecycle identity drift marks the
node `STALE`; unreadable, malformed, missing, or untrusted packet facts mark it
`UNTRUSTED`. Either condition makes the graph fail closed. An authority state
that was already non-`TRUSTED` in the graph is never promoted by a later read.

Issue title/body, prompts, transcripts, and other free-form content are parsed
only as needed for trust/metadata verification and are never emitted as facts
or plan output.

## Safety boundary

Slice A/B remain read-only planning:
- no GitHub mutation;
- no Cursor/session process action;
- no provider/model routing;
- no quota estimation;
- no bypass of owner/HUMAN_REQUIRED state.

A reconciled plan is observation evidence, not mutation authority. Any future
graph-driven effect must re-read mutable canonical state at its own effect
boundary. Repeated evaluation of unchanged graph and GitHub facts is
idempotent and deterministic.

## CLI

```bash
PYTHONPATH=. python3 -m atlas readiness plan --graph graph.json
PYTHONPATH=. python3 -m atlas readiness github-plan --graph graph.json
```

`plan` performs no network access. `github-plan` performs authenticated
read-only GitHub/permission lookups and then emits the same bounded plan shape.
