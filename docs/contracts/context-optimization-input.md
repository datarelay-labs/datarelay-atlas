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

The accepted source is `context-canary-eligibility-report` v1 or v2 with
`decision=ELIGIBLE`. v1 remains a backward-compatible comparability-only
source. v2 additionally carries a content-free `run_set_digest` that binds the
exact measured experiment identity. Atlas requires:

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
- report-level arm and record counts agree with the arm records;
- v2 reports carry a 64-hex `run_set_digest` produced from repository,
  task kind, exact source head, and the sorted arm/case/telemetry-run identities.

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

This distinction is intentional. Both the legacy v1 and exact-bound v2 base
paths consume Engineering System P0.75-A comparability evidence and therefore
leave quality non-inferiority `UNKNOWN`.

A second read-only binding may consume the bounded Engineering System P0.75-B
`context-shadow-equivalence-report` **v2**. Atlas revalidates the base input
and requires the base to have v2 exact-run evidence. It then requires exact
agreement on repository, task kind, Engineering System head,
provider/model/reasoning/toolset profile, `run_set_digest`, arm IDs, per-arm
run counts, arm count, observation count, and control-arm membership. It also
requires every arm's material-action total to equal the declared control arm's
total, as an additional consistency check on the upstream equivalence claim.
A legacy v1 canary input or shadow report cannot advance the quality gate.
Only after these checks does Atlas emit:

- `quality_noninferiority=SHADOW_ACTION_EQUIVALENT`;
- bounded `quality_evidence` identifying the P0.75-B v2 report facts,
  including repository, task kind, source head, and `run_set_digest`.

`SHADOW_ACTION_EQUIVALENT` means only that the measured verified-solved
canary cases followed the same bounded material action/target trace as the
declared control arm. It is not a general semantic-equivalence or production
promotion certificate. Deployment security and runtime support remain
independent `UNKNOWN` gates, and active control remains
`NOT_ELIGIBLE_FOR_ACTIVE_CONTROL`.

A third read-only binding may consume the final Engineering System P1-A
`context-learned-canary-admission-report` v1. Atlas strictly revalidates the
bounded candidate identity, the exact requirement-key set, and the deterministic
requirement/blocker relationship. It does not repeat Engineering System's
trusted runtime-evidence verification.

For `SETUP_ALLOWED`, Atlas records `learned_canary_evidence` but leaves
`data_egress_eligibility=UNKNOWN` and `runtime_capability=UNKNOWN`. For
`CANARY_READY` only, Atlas may emit the deliberately narrow states:

- `data_egress_eligibility=LOCAL_CANARY_EGRESS_DENY_VERIFIED`;
- `runtime_capability=LEARNED_COMPRESSOR_LOCAL_CANARY_READY`.

These states describe only the admitted local learned-compressor canary. They
are not production eligibility and do not select or execute a compressor. If
shadow-quality evidence is already present, Atlas reconstructs and revalidates
the exact #119 binding before preserving `SHADOW_ACTION_EQUIVALENT`.

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

To bind final Engineering System P1-A learned-canary admission evidence:

```bash
PYTHONPATH=. python3 -m atlas usage context-learned-bind \
  --context-input context-input.json \
  --admission-report learned-canary-report.json
```

All commands read only bounded local JSON and print normalized content-free
evidence. They perform no network, provider, GitHub, model, compressor, or
session mutation.
