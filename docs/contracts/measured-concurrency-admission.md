# Measured Concurrency Admission v1

Atlas roadmap #58 uses the existing Dependency / Readiness Graph as the only node-readiness and resource-conflict planner. This slice adds provider-neutral execution-slot admission and measured join evidence without enabling multi-worker dispatch.

## Authority boundary

The admission plan is `ADVISORY_ONLY` with `NO_DISPATCH_AUTHORITY`.

It may:
- consume one validated dependency/readiness graph;
- consume bounded execution-slot observations;
- apply global graph WIP, per-project WIP, max-parallel-admission, slot gates, and slot-evidence freshness;
- deterministically bind graph-selected independent nodes to eligible slots;
- record measured outcomes after an external execution.

It may not:
- activate a Work Packet;
- dispatch a worker;
- invoke a provider;
- change provider/model/runtime state;
- relax readiness graph dependency/resource/trust gates;
- bypass the existing single-effect authorization path.
## Execution slots

A slot represents one currently observed provider-neutral execution opportunity.

Each slot carries:
- stable slot and worker identities;
- provider/runtime and optional route attribution;
- state: AVAILABLE / BUSY / UNAVAILABLE / UNKNOWN;
- trust, budget, rate, quota, and blast-radius gates;
- bounded evidence reference and observation timestamp.

Only AVAILABLE slots with every gate ALLOW and evidence inside the configured freshness window are eligible. UNKNOWN is not ALLOW.
## Admission

Atlas first computes the existing dependency/readiness plan. The admission layer considers only `selected_node_ids` from that plan.

The first slice additionally enforces:
- `max_parallel_admission`;
- one explicit `max_wip` per repository represented in the graph;
- currently ACTIVE nodes consume project WIP;
- one eligible slot per admitted node;
- deterministic assignment order;
- exact repository/issue/branch/HEAD attribution on every assignment.

A graph-selected node that cannot be admitted remains visible with a bounded reason such as PROJECT_WIP_LIMIT, MAX_PARALLEL_ADMISSION, or NO_EXECUTION_SLOT.
## Measured join / reconciliation

A run observation must bind the exact current admission `plan_digest` and provide exactly one outcome for every admitted assignment.

Node/slot/worker/provider attribution must exactly match the plan.

Outcomes:
- COMPLETE;
- FAILED;
- HUMAN_REQUIRED.

The normalized run result is PASS, PARTIAL, FAILED, or HUMAN_REQUIRED. Atlas also records wall time, summed node duration, and a work/wall ratio as measurement evidence. These metrics do not prove correctness; exact-head/test/audit truth remains outside this contract.

## Product surfaces

- Web UI: `/concurrency`
- CLI: `atlas concurrency show`
- CLI snapshot publish: `atlas concurrency publish --snapshot <json>`
- CLI measured run: `atlas concurrency record-run --observation <json>`
- authenticated MCP: `get_concurrency_admission`

The local snapshot and run ledger are rebuildable measurement caches and are excluded from durable Atlas backups.
