# Provider Capacity Attribution v1

This contract records provider-authoritative identity facts about where capacity
belongs. It is evidence only and grants no broker, route-selection, provider
transport, credential, session, or transition authority.

The four facts are execution surface, allowance domain, shared allowance, and
charging mode.

Every fact is either UNKNOWN or OBSERVED. UNKNOWN never carries a value. Any
OBSERVED fact requires top-level evidence authority PROVIDER_AUTHORITATIVE, a
bounded source_ref, an immutable 64-hex source_digest, and a UTC observed_at
timestamp. These fields make the asserted facts traceable and independently
re-checkable.

Runtime logic deliberately does not hard-code provider/product pool names or
subscription assumptions. Values are bounded labels or a boolean
shared_allowance observation. If a provider fact cannot be proven from
provider-authoritative evidence with verifiable source provenance, it stays
UNKNOWN. UNVERIFIED evidence carries no source_ref, source_digest, or
observed_at.

Route candidates may carry this contract optionally. When present, its provider
identity must match both capability and capacity evidence. Broker eligibility
and ranking do not consume these facts in v1.
