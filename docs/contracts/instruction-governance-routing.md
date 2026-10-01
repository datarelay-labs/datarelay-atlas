# Instruction Governance Candidate Routing v1

This is a read-only routing projection over the latest validated Instruction Governance audit.

Mapping is deterministic:

- NO_CHANGE -> NO_CHANGE
- REJECTED -> REJECTED
- HUMAN_REQUIRED -> HUMAN_REQUIRED
- CANARY_READY -> CANARY_PR_REQUIRED

Before any route other than HUMAN_REQUIRED is exposed, Atlas rechecks the target Git HEAD and managed-surface inventory digest against the audit. Drift, malformed candidate state, or missing evidence fails closed to HUMAN_REQUIRED.

Authority is ROUTING_ADVISORY_ONLY and mutation authority is NONE. CANARY_PR_REQUIRED does not create a PR, run a canary, modify a managed surface, change a default branch, or invoke a provider. It means a later separately authorized canary/PR/adoption step is required.

The route preserves exact audit, Engineering System revision, model/profile/harness, behavior-result, candidate-change digest and evaluation reference attribution.
