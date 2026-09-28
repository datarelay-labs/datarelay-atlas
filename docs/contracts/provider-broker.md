# Provider Capacity Broker v1

Status: read-only planning foundation for roadmap #55
Authority: `ADVISORY_ONLY`

Machine-checkable schemas:
- [`provider-route-candidate.schema.json`](provider-route-candidate.schema.json)
- [`provider-broker-plan.schema.json`](provider-broker-plan.schema.json)

Example fixtures:
- [`fixtures/provider-route-codex.example.json`](fixtures/provider-route-codex.example.json)
- [`fixtures/provider-broker-plan.example.json`](fixtures/provider-broker-plan.example.json)

## Purpose

The broker combines the existing provider capability descriptor and provider
capacity-input contracts with explicit Atlas-owned eligibility gates. It emits
a deterministic advisory plan only; it does not invoke a provider, switch a
model, mutate a session, perform failover, or grant execution authority.

## Candidate contract

Each route has a bounded `route_id`, one validated capability descriptor, one
validated capacity input for the same provider, six explicit gates, and two
integer preference ranks. Lower rank values are preferred.

The gates are `policy`, `trust`, `budget`, `usage_mode`, `blast_radius`, and
`wip`. Each is exactly `ALLOW`, `DENY`, or `UNKNOWN`.

## Eligibility

A route is ineligible when any explicit gate is `DENY` or `UNKNOWN`, when the
required capability is absent, `UNSUPPORTED`, or `UNKNOWN`, when provider
identity differs between capability and capacity evidence, or when remaining
capacity is `UNKNOWN` or observed as zero.

Capacity facts are never estimated. Unknown capacity stays `UNKNOWN` and cannot
be made eligible by an attractive preference rank.

## Strategies

`CAPABILITY_FIRST` orders eligible routes by `capability_preference`, then
`stewardship_preference`, then `route_id`.

`STEWARDSHIP` reverses the first two preference dimensions and keeps `route_id`
as the deterministic final tie-breaker. Input order never changes the plan.

`selected_route_id` and `fallback_route_ids` are planning evidence only. A
consumer must still pass the authoritative Engineering System, exact-head,
trust, permission, budget, and HUMAN_REQUIRED gates before any effect.

## Safety

- Provider and capacity identity must match exactly.
- Route ids and output identities reject secret-like values.
- Raw prompts, transcripts, findings, source/tool output, local paths, and
  sensitive authentication material are not copied into the plan.
- An empty plan is invalid because planning requires at least one candidate.
- No transport, provider adapter, Cursor process, GitHub mutation, or session
  control dependency exists in the broker module.
