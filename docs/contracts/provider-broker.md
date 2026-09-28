# Provider Capacity Broker v1

Status: read-only planning foundation for roadmap #55
Authority: `ADVISORY_ONLY`

Machine-checkable schemas:
- [`provider-route-candidate.schema.json`](provider-route-candidate.schema.json)
- [`provider-broker-plan.schema.json`](provider-broker-plan.schema.json)
- [`provider-transition-plan.schema.json`](provider-transition-plan.schema.json)

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

A positive `OBSERVED` `remaining_capacity` is also ineligible unless the
capacity evidence window can prove freshness. The observation instant is
`evidence.window_end`. Planning takes an explicit `evaluated_at` UTC timestamp
and a `max_evidence_age_seconds` integer from 0 through 366 days. The planner
does not read a wall clock. A missing window is `REMAINING_CAPACITY_UNBOUND`.
A `window_end` after `evaluated_at` is `REMAINING_CAPACITY_FUTURE`. A positive
age greater than `max_evidence_age_seconds` is `REMAINING_CAPACITY_STALE`.
An age equal to the maximum remains eligible when every other gate passes.
`UNKNOWN` and zero remaining capacity keep their existing reasons and do not
gain a freshness reason.

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

## Failover transition planning

The transition layer is also read-only and `ADVISORY_ONLY`. It accepts the
same bounded route candidates plus a current route, an allowlisted failure
reason, optional previously failed route ids, and a route-attempt ceiling.
The emitted `attempt` is the count of failed routes including the current
failure. Reaching or exceeding `max_attempts` is evidence for
`HUMAN_REQUIRED`, never permission for another transition.

Before recommending anything, Atlas removes only the previously failed routes
and reruns the canonical broker with the same explicit freshness boundary.
The declared current route must be that plan's selected eligible route. Atlas
then marks the current route failed and reruns the same broker over the
remaining candidates. A stale or future fallback cannot be recommended.

The result is one of:

- `TRANSITION_RECOMMENDED / ELIGIBLE_FALLBACK` when a newly replanned eligible
  fallback exists below the attempt ceiling;
- `HUMAN_REQUIRED / ATTEMPT_LIMIT_REACHED` when the bounded route-attempt
  ceiling has been reached;
- `HUMAN_REQUIRED / NO_REMAINING_ROUTES` when every configured route has
  already failed;
- `HUMAN_REQUIRED / NO_ELIGIBLE_FALLBACK` when routes remain but all are
  currently ineligible under the existing policy/capability/capacity gates.

The transition plan records only bounded current/prior/aggregate failed route
ids, failure classification, attempt counts, and the remaining broker plan.
Its validator requires `failed_route_ids` to equal the sorted union of
`prior_failed_route_ids` and the current `from_route_id`, forbids failed
routes from reappearing in the remaining plan, and requires the embedded broker
plan to use the same strategy and required capability as the enclosing
transition. A request whose prior failures have already reached
`max_attempts`, or a serialized plan whose attempt exceeds that ceiling, fails
closed.

The allowlisted failure reason is bounded advisory input for this planning
slice; it is not promoted to provider-authoritative quota/health evidence. The
planner never performs the transition. Provider invocation and any later effect
authorization require a separate exact-state boundary.

Read-only CLI example:

```bash
PYTHONPATH=. python3 -m atlas usage provider-transition-plan \
  --candidates candidates.json \
  --required-capability CODE_REVIEW \
  --current-route codex-primary \
  --failure-reason QUOTA_EXHAUSTED \
  --max-attempts 3 \
  --evaluated-at 2026-09-28T00:00:00Z \
  --max-evidence-age-seconds 0
```

## Safety

- Provider and capacity identity must match exactly.
- Route ids and output identities reject secret-like values.
- Raw prompts, transcripts, findings, source/tool output, local paths, and
  sensitive authentication material are not copied into the plan.
- An empty plan is invalid because planning requires at least one candidate.
- No transport, provider adapter, Cursor process, GitHub mutation, or session
  control dependency exists in the broker module.
