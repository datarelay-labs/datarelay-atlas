# Atlas Surface Reconciliation Contract

## Scope

This gate reconciles the exact release candidate's implemented browser surface against the Atlas product charter, architecture, roadmap, and registered routes. It is a human-equivalent release gate, not CI contract validation.

## Required execution

- Executor/final auditor: **ChatGPT itself**. ChatGPT directly acts as the applicable real user persona and drives the actual Atlas browser surface. Coding agents, alternate models, wrappers, scripted replays, CI jobs, and automated harnesses are supporting evidence only and cannot produce gate PASS.
- Surface: the actual Atlas browser UI launched from the exact candidate.
- Browser: a real Chromium/Chrome process; source inspection, API calls, jsdom, or static HTML parsing do not substitute.
- Candidate: execute from a clean immutable checkout (or equivalently content-addressed release artifact) of the exact Git commit, record that commit/artifact identity before launch, and reject dirty-worktree, untracked-file, or different-candidate evidence. `git rev-parse HEAD` alone is not sufficient candidate binding.
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

## Engineering System User Acceptance v2

- Start feature-first and black-box-first. The acting ChatGPT persona discovers Atlas from the public browser surface before source/route/test inspection; implementation knowledge is auditor-only after public evidence is frozen.
- A finding is not a stop condition. Preserve evidence and continue every safe independent route/state. Do not patch source or this contract during the frozen discovery pass. After safe coverage is exhausted, freeze the complete finding set, batch-remediate, and rerun from the beginning.
- Reconcile capability → discoverable route/control → persona goal → state/empty/error/recovery semantics → user-visible result. Every mandatory capability/route receives an explicit disposition; blocked/partial/not-run never becomes PASS.
- Retain exact candidate HEAD, committed contract digest, browser identity, route/scenario ledger, findings ledger, and a ledger-derived summary. Release PASS requires 100% applicable capability/public-surface coverage and zero mandatory FAIL/PARTIAL/BLOCKED or unresolved blocking finding.
- Static/API/source inspection is supporting evidence only. A real Chromium/Chrome process driven by ChatGPT remains mandatory for the user action.
