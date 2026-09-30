# Provider Capacity Attribution v1

This contract records provider-authoritative identity facts about where capacity
belongs. It is evidence only and grants no broker, route-selection, provider
transport, credential, session, or transition authority.

The four facts are execution surface, allowance domain, shared allowance, and
charging mode.

Every fact is either UNKNOWN or OBSERVED. UNKNOWN never carries a value. Any
OBSERVED fact requires top-level evidence authority PROVIDER_AUTHORITATIVE and a
UTC observed_at timestamp.

Runtime logic deliberately does not hard-code provider/product pool names or
subscription assumptions. Values are bounded labels or a boolean
shared_allowance observation. If a provider fact cannot be proven from
provider-authoritative evidence, it stays UNKNOWN.

Route candidates may carry this contract optionally. When present, its provider
identity must match both capability and capacity evidence. Broker eligibility
and ranking do not consume these facts in v1.
