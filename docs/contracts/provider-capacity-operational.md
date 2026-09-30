# Provider Capacity Operational Evidence

Version 1 is an evidence-only provider-neutral contract for operational facts that do not belong in the existing capacity-input or capacity-attribution contracts.

It records three independent UNKNOWN/OBSERVED facts:

- reset semantics: window mode and duration only; the existing capacity-input `reset_at` remains the raw reset timestamp and is not duplicated here;
- health: only provider-authoritative operational/degraded/outage/maintenance state;
- latency: an explicit metric, measured boundary, and integer milliseconds.

Every OBSERVED fact requires `PROVIDER_AUTHORITATIVE` evidence with bounded source kind/reference, immutable SHA-256 source digest, and UTC observation time. Local request success, client wall-clock guesses, undocumented/private endpoint scraping, and vendor subscription assumptions are not admitted as provider-authoritative facts.

UNKNOWN facts carry no values. UNVERIFIED evidence cannot carry source provenance.

A route candidate may carry this object only when its provider identity exactly matches the capability/capacity provider. The object is preserved as evidence but does not affect broker eligibility, ranking, selected route, fallback order, transition authorization, or provider effects in v1.
