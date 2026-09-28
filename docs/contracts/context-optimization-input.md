# Context Optimization Input v1

Status: Atlas #76 observation-only governor input
Source: Engineering System context-canary eligibility report

Machine-checkable schema:
[`context-optimization-input.schema.json`](context-optimization-input.schema.json)

Example:
[`fixtures/context-optimization-input.example.json`](fixtures/context-optimization-input.example.json)

## Purpose

Atlas consumes the bounded factual output of the Engineering System context
optimization canary without importing its benchmark harness. The boundary
revalidates exact-head attribution, provider/profile identity, verified outcome,
context volume, provider-measured token/cache/cost totals, and effort counters.

The v1 output is evidence only. It is deliberately **not** an optimizer
selection or control authorization.

## Trust boundary

The accepted source is `context-canary-eligibility-report` v1 with
`decision=ELIGIBLE`. Atlas requires:

- one exact 40-hex Engineering System head across the report and every arm;
- a fully known provider/model/reasoning/toolset profile;
- a canonical repository and allowlisted task kind;
- at least two unique optimizer arms and at most 256 bound records;
- every arm has the same run count/case-set cardinality and all runs verified solved;
- exact context-byte/reduction consistency;
- provider cost and cost-per-solved-task are measured decimal facts;
- usage, effort, rework, and cost aggregates stay within the upstream
  per-record telemetry bounds multiplied by each arm's run count;
- rework totals agree with PR/CI/review rework counters;
- report-level arm and record counts agree with the arm records.

Unknown fields, duplicate JSON keys, oversized inputs, malformed numbers,
credential-shaped labels, and unsupported source identities fail closed.

## Output semantics

Atlas emits `context_optimization_input` with:

- `source_evidence.comparability=ELIGIBLE`;
- `quality_noninferiority=UNKNOWN`;
- `data_egress_eligibility=UNKNOWN`;
- `runtime_capability=UNKNOWN`;
- `active_control=NOT_ELIGIBLE_FOR_ACTIVE_CONTROL`;
- `control_mode=OBSERVE_ONLY`.

This distinction is intentional. The base v1 path consumes Engineering System
P0.75-A comparability evidence and therefore leaves quality non-inferiority
`UNKNOWN`.

A second read-only binding may consume the bounded Engineering System P0.75-B
`context-shadow-equivalence-report`. Atlas revalidates the base input and then
requires exact agreement on Engineering System head, provider/model/reasoning/
toolset profile, arm IDs, per-arm run counts, arm count, observation count, and
control-arm membership. Only then does it emit:

- `quality_noninferiority=SHADOW_ACTION_EQUIVALENT`;
- bounded `quality_evidence` identifying the P0.75-B report facts.

`SHADOW_ACTION_EQUIVALENT` means only that the measured verified-solved
canary cases followed the same bounded material action/target trace as the
declared control arm. It is not a general semantic-equivalence or production
promotion certificate. Deployment security and runtime support remain
independent `UNKNOWN` gates, and active control remains
`NOT_ELIGIBLE_FOR_ACTIVE_CONTROL`.

Atlas therefore does not emit a winner, rank, score, or recommendation and
does not choose COMPRESS, CLEAR, YIELD, ROUTE, or any session/model action.

## CLI

```bash
PYTHONPATH=. python3 -m atlas usage context-canary-input --input report.json
```

To bind a separately produced P0.75-B shadow-equivalence report:

```bash
PYTHONPATH=. python3 -m atlas usage context-shadow-bind \
  --context-input context-input.json \
  --shadow-report shadow-report.json
```

Both commands read only bounded local JSON and print normalized content-free
evidence. They perform no network, provider, GitHub, model, or session
mutation.
