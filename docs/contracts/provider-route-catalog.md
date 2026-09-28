# Provider Route Catalog v1

Status: read-only configuration foundation for roadmap #55
Authority: `CONFIGURATION_ONLY`

Machine-checkable schema:
[`provider-route-catalog.schema.json`](provider-route-catalog.schema.json)

Example:
[`fixtures/provider-route-catalog.example.json`](fixtures/provider-route-catalog.example.json)

## Purpose

The catalog separates **configured route identity** from dynamic eligibility.
It contains only a bounded route id, one registered Atlas adapter identity, and
an `ENABLED` / `DISABLED` configuration state.

A catalog entry does **not** contain or grant:

- provider/runtime/usage-mode/capability claims;
- policy, trust, budget, usage-mode, blast-radius, or WIP gates;
- capacity, quota, reset, health, or latency facts;
- model names or model selection;
- endpoint or transport configuration;
- API keys, OAuth/session credentials, cookies, or tokens;
- provider execution or failover authority.

## Canonical adapter binding

Only registered real Atlas CODE_REVIEW adapters are configurable in v1:

- `CodexAuditProvider`;
- `BoundedResponsesAuditProvider`.

Atlas derives the canonical provider capability descriptor by calling the
existing provider-capability registry. The catalog cannot supply or override
`provider`, `runtime`, `usage_mode`, `capabilities`, or result-contract
fields.

The generic `AuditPort` / `FixedAuditAdapter` fallback remains a testing or
caller-supplied abstraction and is intentionally not a configured live route.

Catalog v1 also requires adapter identities to be unique. Because v1 has no
model/profile/endpoint identity, two route ids backed by the same adapter would
represent indistinguishable execution capacity and could create false
failover redundancy. A future profile-aware contract may relax this only after
that distinguishing identity is explicit and attributable.

## Candidate materialization

A broker candidate is materialized from four independent inputs:

1. enabled configured route;
2. canonical descriptor derived from its adapter;
3. provider-capacity evidence;
4. dynamic Atlas-owned eligibility gates and preference ranks.

The existing provider-route candidate validator remains authoritative. A
capacity input whose provider does not match the derived descriptor fails
closed.

`ENABLED` means only that the route may be considered for materialization. It
does not mean the route is policy/trust/capacity eligible. `DISABLED` routes
cannot materialize a broker candidate.

## Read-only CLI

```bash
PYTHONPATH=. python3 -m atlas usage provider-route-catalog \
  --input provider-route-catalog.json
```

The command validates and prints normalized catalog metadata only. It performs
no provider/model invocation, network access, credential lookup, route
selection, failover, or session mutation.

## Relationship to broker and transitions

The catalog is upstream of the existing advisory broker:

```text
route catalog
  -> registered adapter descriptor
  -> dynamic capacity + gates + ranks
  -> provider route candidate
  -> advisory broker plan
  -> advisory transition plan
```

Broker and transition authority remain `ADVISORY_ONLY`. Any future effectful
provider invocation requires a separate exact-state authorization boundary.
