# Atlas Surface Reconciliation Contract

## Scope

This gate reconciles the exact release candidate's implemented browser surface against the Atlas product charter, architecture, roadmap, and registered routes. It is a human-equivalent release gate, not CI contract validation.

## Required execution

- Executor/final auditor: **ChatGPT itself**. ChatGPT directly acts as the applicable real User/Operator/Admin persona and drives the actual Atlas browser surface. Another agent/model, wrapper, scripted replay, CI job, or automated harness is supporting evidence only and cannot declare this gate PASS.
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

## Engineering System User Acceptance v2 — mandatory execution semantics

This contract inherits the portable semantics from `datarelay-labs/engineering-system@fb431381ef4c49851fc40b683e8bad22607e7e0c/standards/USER_ACCEPTANCE.md`.

- ChatGPT directly executes and finally audits the gate as a real persona; automation is supplemental only.
- Execute feature-first and black-box-first. The acting persona discovers the browser product from visible navigation, controls, state, errors, and guidance before the auditor uses routes/source/tests as an omission oracle.
- A finding never ends the pass by itself. Preserve it and continue every safe independent surface. Do not patch source/contract during the frozen pass; freeze all findings, batch-remediate, then rerun from the beginning on the new candidate.
- Retain machine-readable scenario/findings ledgers and derive the summary from them. Blocked/partial/not-run work is not PASS.
- Release PASS requires complete applicable capability/public-surface coverage, zero mandatory FAIL/PARTIAL/BLOCKED, zero unresolved blocking finding, and exact committed contract/candidate evidence.

