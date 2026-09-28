# Approved Provider Route Set v1

Status: static route configuration contract for Roadmap #55
Authority: `CONFIGURATION_ONLY`

Machine-checkable schema:

- [`provider-route-set.schema.json`](provider-route-set.schema.json)

Example fixture:

- [`fixtures/provider-route-set.example.json`](fixtures/provider-route-set.example.json)

## Purpose

The route set declares which provider/runtime/usage-mode/adapter identities an
operator has configured for Atlas to consider. It also declares which
capabilities each route may serve and the static capability/stewardship
preference ranks used by the existing Provider Capacity Broker.

Configuration is not provider execution authority and is not evidence that a
route is currently healthy, funded, trusted, within quota, or otherwise
eligible.

The example fixture is contract evidence only. It does not claim that any
credential, billing plan, provider capacity, or production route is currently
available.

## Static route identity

Each route contains only bounded non-secret facts:

- `route_id`;
- `enabled`;
- provider;
- runtime;
- usage mode;
- adapter;
- allowed capability names;
- capability/stewardship preference ranks.

Routes and allowed capability names are normalized into deterministic sorted
order. Duplicate route ids and duplicate/unknown capabilities fail closed.

Credentials, API keys, bearer tokens, cookies, prompts, transcripts, local
paths, and provider responses are not part of this contract.

## Strict evidence binding

`bind_configured_provider_route()` combines one enabled static route with the
existing dynamic contracts:

1. validate the approved route set;
2. require the requested route id to exist and be enabled;
3. require the requested capability to be allowed by configuration;
4. validate the provider capability descriptor;
5. require provider/runtime/usage-mode/adapter identity to exactly match the
   configured route;
6. require every descriptor capability reported as `SUPPORTED` to be inside
   the configured capability allowlist;
7. require the requested capability to be both allowed and `SUPPORTED`;
8. validate provider capacity evidence and require its provider to match;
9. accept the six existing dynamic broker gates without changing their values;
10. use only the configured preference ranks;
11. construct and revalidate the existing `provider_route_candidate`.

The route configuration therefore cannot turn `UNKNOWN` or `DENY` into
`ALLOW`. Dynamic policy, trust, budget, usage-mode, blast-radius, and WIP gate
facts remain Atlas-owned evidence and retain the semantics of the existing
Provider Capacity Broker.

## Registered live-adapter materialization

`materialize_registered_provider_route_candidate()` is the canonical v1 path
for turning an approved route into a candidate for a real registered Atlas
adapter. It accepts no caller-supplied capability descriptor and no rank
override.

The function:

1. validates the same approved route set;
2. requires the route to exist and be enabled;
3. requires its configured adapter to be one of the registered real v1
   adapters: `CodexAuditProvider` or `BoundedResponsesAuditProvider`;
4. derives the capability descriptor with the existing adapter registry;
5. delegates to `bind_configured_provider_route()`, which rechecks configured
   provider/runtime/usage-mode/adapter identity, capability allowlisting,
   provider-matched capacity evidence, dynamic gates, and configured ranks.

A generic `AuditPort` route may still exist in static configuration for
testing or caller-supplied abstractions, but it cannot use the registered-live
materialization path. Future real adapters must be added explicitly rather than
being accepted because a caller supplied a syntactically valid descriptor.

## Safety boundary

- `CONFIGURATION_ONLY` never becomes `ADVISORY_ONLY` or execution authority
  by itself.
- Disabled/unconfigured routes cannot be bound.
- An arbitrary descriptor cannot substitute another provider, runtime, usage
  mode, or adapter under an approved route id.
- Config rank values are the only ranks that enter the candidate.
- No provider/model call, health probe, quota lookup, credential use, session
  mutation, or provider transition occurs in this module.
- Real health/reset/latency facts and any effect-side transition authorization
  remain later Roadmap #55 layers.
