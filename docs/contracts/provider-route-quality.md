# Provider Route Verified Outcome Evidence v1

This contract records provider-neutral route quality only after a Work Packet outcome has attributable verification evidence. It supports the Provider Capacity Broker roadmap objective of comparing cost and owner time per verified solved Work Packet before route policy changes.

## Authority

Authority is EVIDENCE_ONLY and broker influence is NONE.

Route-quality evidence does not make a route eligible, change a rank, select a route, invoke a provider, switch a provider, authorize an effect, or infer undocumented quota or cost behavior.

## Observation identity

Every observation binds route/provider/runtime/usage-mode/adapter identity to one repository, Work Packet issue, exact 40-hex subject HEAD and task kind.

The verified outcome records PASS, REWORK, HUMAN_REQUIRED or FAILED plus first-pass result, audit finding count, rework count, owner intervention count, wall time and quota interruptions.
A first-pass observation must be PASS with zero reworks. A REWORK observation must record at least one rework.

Cost is either UNKNOWN or OBSERVED. OBSERVED cost requires a bounded evidence reference and immutable SHA-256 digest. Atlas never guesses cost from subscription names.

Verification always requires a bounded source reference, immutable SHA-256 digest and UTC observation time. Raw prompts, transcripts, credentials and source bodies are not part of this contract.

## Aggregation

A published snapshot deterministically groups observations by exact route identity and exposes sample count; verified PASS count/rate; first-pass count/rate; audit finding, rework, owner-intervention and quota-interruption totals; wall-time total/average; observed cost count/total; and unknown-cost count.

Rates are basis points to avoid floating-point ambiguity.

The snapshot is a rebuildable derived cache generated from bounded observation files. It is not canonical engineering state and is excluded from durable backup authority.

## Product surfaces

- Provider Web UI: /providers
- CLI: atlas providers quality-show
- CLI publish: atlas providers quality-publish --observation PATH
- MCP: get_provider_route_quality

No surface changes provider broker behavior.
