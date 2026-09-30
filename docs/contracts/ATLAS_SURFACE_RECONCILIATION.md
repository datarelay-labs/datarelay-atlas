# Atlas Surface Reconciliation Contract

## Scope

This gate reconciles the exact release candidate's implemented browser surface against the Atlas product charter, architecture, roadmap, and registered routes. It is a human-equivalent release gate, not CI contract validation.

## Required execution

- Executor: ChatGPT Chat or another owner-authorized human-equivalent browser executor.
- Surface: the actual Atlas browser UI launched from the exact candidate.
- Browser: a real Chromium/Chrome process; source inspection, API calls, jsdom, or static HTML parsing do not substitute.
- Candidate: record the exact Git commit and reject evidence from a different candidate.
- Environment: isolated Atlas data root with representative engineering and personal/reference sources.
- Evidence must record browser engine/version, candidate SHA, route/action, expected result, actual result, and cleanup truth.

## Reconciliation inventory

The executor must inspect every implemented public browser route and state in the candidate, including at minimum:

1. / — overview counts, project inventory, enabled/disabled state, navigation.
2. /projects/<project_id> — project identity, source/projection state, lifecycle evidence, project-scoped search.
3. /search — cross-project attributable search and project isolation.
4. Empty registry and empty-search states.
5. Unknown project and unknown route.
6. Invalid/oversized query.
7. Corrupt or invalid local projection/lifecycle evidence must fail closed without becoming a false 404/PASS.
8. Engineering vs personal/reference source classification.
9. Security response headers and absence of write controls.
10. Confirm POST/write attempts are rejected.

Any implemented browser route omitted from this inventory at execution time is a reconciliation failure until the contract is updated.

## Pass rule

PASS requires all current browser-visible capabilities to be accounted for and exercised on the same candidate. Missing, stale, inaccessible, misleading, or source-truth-divergent behavior is REWORK, not an inferred PASS.
