# Provider capability contract

Atlas represents provider/runtime features as descriptive evidence. A capability
descriptor never grants permission, trust, budget, routing, mutation, or
execution authority.

## Descriptor v1

`provider_capability_descriptor` identifies:

- `provider`: provider family;
- `runtime`: execution surface;
- `usage_mode`: billing/auth usage class, not a credential;
- `adapter`: Atlas adapter identity;
- `capabilities`: bounded allowlisted capability states;
- `authority=EVIDENCE_ONLY`.

Capability states are `SUPPORTED`, `UNSUPPORTED`, or `UNKNOWN`. `CODE_REVIEW`
is the first normalized capability and, when supported, must declare the
`audit_result_v1` result contract.

The initial real descriptors are:

- `CodexAuditProvider`: `codex / codex_cli / chatgpt_plan`;
- `BoundedResponsesAuditProvider`: `openai / responses_api / api`;
- generic fallback `AuditPort`: `generic / audit_port / caller_supplied`.

No provider command, model, credential, endpoint, or transport configuration is
part of this contract.

## CODE_REVIEW result v1

Atlas normalizes an existing `AuditResult` to
`provider_capability_result` with:

- provider/runtime/usage-mode/adapter attribution;
- `capability=CODE_REVIEW`;
- `result_contract=audit_result_v1`;
- outcome `PASS`, `REWORK`, or `HUMAN_REQUIRED`;
- caller-supplied exact `subject_head`;
- caller-supplied bounded `evidence_ref`;
- `authority=EVIDENCE_ONLY`.

Raw audit findings are intentionally excluded. Provider-specific findings remain
inside the existing audit execution path and are not copied into capability
evidence.

Two providers given the same `AuditResult`, `subject_head`, and `evidence_ref`
therefore produce the same cross-provider semantics after attribution fields are
removed.

## Fail-closed rules

Atlas rejects unknown or duplicate capabilities, unsupported result contracts,
invalid capability states, invalid verdicts, non-exact subject SHAs, additional
fields, path-shaped identity/evidence values, and secret-like identity/evidence
values.

A descriptor that reports `CODE_REVIEW=UNSUPPORTED` or `UNKNOWN` cannot
normalize a CODE_REVIEW result.

## Authority boundary

Engineering System policy resolves before capability presence. Atlas exact-head,
Trust Plane, permission, budget, and `HUMAN_REQUIRED` gates remain authoritative.

This contract does not select providers, rank capabilities, score routes, invoke
models, perform failover, or implement Capacity Broker behavior. Those concerns
remain outside this slice and #55 remains the routing/capacity workstream.
