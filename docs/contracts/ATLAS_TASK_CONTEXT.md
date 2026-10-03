# Atlas Task Context Contract

Status: Accepted implementation contract
Version: 1
Kind: `atlas_task_context`

## Minimal design gate

1. **Goal** — Give an approved AI client one small, current, attributable bootstrap for a registered project/repository so work can start without replaying old chat or manually rediscovering Atlas state.
2. **Non-goals** — No chat archive, memory-candidate persistence, procedural learning, canonical mutation, answer synthesis, new vector/graph database, provider-specific behavior, or production deployment.
3. **Affected public contract** — New read-only CLI `atlas task-context show` and MCP tool `get_task_context`. A caller supplies exactly one of `project_id` or `repository` and may supply one bounded `workstream` hint.
4. **State / migration impact** — None. The response is rebuilt per call from the existing registry, projections, lifecycle snapshot, Engineering System observation and evidence federation store.
5. **Security / operations impact** — `atlas.read` only. No secrets, source bodies, transcripts or credentials are copied into the bootstrap. The response is bounded to 24 KiB and contains only validated metadata plus references for deeper retrieval.
6. **Architecture boundary** — GitHub/Engineering System remain canonical. Atlas composes existing read authorities and does not redefine lifecycle, evidence, search or provenance semantics. Workstream is a query/evidence hint unless current evidence independently binds it.
7. **Acceptance / regression criteria** — Project/repository resolution is deterministic; current/stale/unknown/unavailable state is inherited from existing lifecycle authority; current workstream evidence requires exact-current evidence; source text is not embedded; CLI and MCP semantics match; existing tests remain green.

## Currentness

`currentness.state` is derived only from the existing normalized GitHub lifecycle channel:

- `OBSERVED` → `CURRENT`
- `STALE` → `STALE`
- `UNKNOWN` → `UNKNOWN`
- `UNAVAILABLE` → `UNAVAILABLE`

A workstream string never creates authority. `request.workstream_binding` is:
- `NOT_REQUESTED` — no workstream supplied;
- `QUERY_HINT_ONLY` — supplied for retrieval but no current Engineering System evidence independently binds it;
- `CURRENT_EVIDENCE_MATCH` — at least one evidence family is `CURRENT` and its latest record names that exact workstream.

## Progressive retrieval

The bootstrap returns no full canonical source bodies. `knowledge_refs` contains at most four validated **engineering** projection references; Personal Knowledge is intentionally excluded from automatic M1 bootstrap and remains an explicit separate source class until later scoped-memory policy is implemented. `jit_retrieval` points the client to existing tools such as `search_project`, `get_project_intelligence`, `get_engineering_evidence`, and `get_provenance`.

Clients should fetch deeper source content only when the task requires it. Current canonical state always outranks a remembered or previously retrieved fact.
