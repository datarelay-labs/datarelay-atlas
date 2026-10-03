# Atlas Full User E2E Contract

## Scope

This gate proves complete user missions through the actual Atlas browser UI on the exact candidate after Surface Reconciliation passes.

## Required environment

- real Chromium/Chrome process;
- clean immutable checkout (or equivalently content-addressed release artifact) of the exact candidate, with candidate identity recorded before launch; dirty/untracked bytes invalidate the evidence;
- isolated Atlas data root;
- at least two registered projects;
- at least one successful engineering projection;
- at least one personal/reference projection when that source class is supported by the candidate;
- bounded validated GitHub lifecycle snapshot where lifecycle display is under test;
- no production mutation.

## Mission A — navigate engineering knowledge

1. Open the Atlas overview.
2. Confirm project/source counts and select a project.
3. Inspect project identity and source/projection state.
4. Search for known engineering content.
5. Verify source repository/path/revision and DERIVED labeling.
6. Navigate back without losing server state.

## Mission B — cross-project retrieval

1. Open cross-project search.
2. Search a term present in one project and absent from another.
3. Verify only attributable hits appear.
4. Open the owning project from the result grouping.
5. Confirm project-scoped search remains isolated.

## Mission C — authority and failure truth

1. Verify personal/reference knowledge is visibly non-authoritative.
2. Verify missing lifecycle/release evidence is UNKNOWN, never PASS.
3. Load a valid bounded lifecycle snapshot and verify observed Work Packet/PR evidence.
4. Replace it with invalid evidence and verify the UI reports UNAVAILABLE rather than trusting it.
5. Request an unknown project and verify 404.
6. Exercise a write/POST attempt and verify rejection.
7. Verify source-derived markup renders as text rather than executable HTML.

## Persistence / recovery

Reload the browser and open a fresh browser context. Registry/projection/lifecycle state must remain consistent with the same data root. The browser UI must not create canonical state or browser-local authority.

## Pass rule

PASS requires all missions on the same exact candidate, browser-process evidence, no API/source-inspection substitution for browser actions, and recorded cleanup truth. Any browser-visible failure, stale candidate, hidden API fallback, fabricated lifecycle PASS, or authority confusion is REWORK.

## Engineering System User Acceptance v2

- **ChatGPT itself is the executor and final auditor.** ChatGPT assumes the applicable Atlas user persona and performs each mission through the real browser. Coding agents, alternate models, scripted replays, CI, and automated harnesses cannot substitute for the user gate.
- Run mission-first, black-box, and real-effect. The persona starts without source/test answer-key knowledge, follows browser-visible discovery/guidance, and verifies the real rendered/derived outcome and authority labeling.
- Include realistic mistakes/recovery for applicable missions: invalid or empty search, unknown project/route, stale or invalid lifecycle evidence, reload/new context, unavailable derived source, and rejected write attempts. Recovery must be understandable from the product surface rather than hidden implementation knowledge.
- A finding is not a stop condition. Continue every safe independent mission, freeze findings at pass end, batch-remediate, and restart invalidated E2E from the beginning on the new candidate.
- Repeat state-sensitive missions across meaningful reload/new-context/source-state variants when one success could hide stale-state or persistence defects. Maintain run-owned cleanup and report environment/tooling blockage honestly.
- Retain machine-readable scenario/findings ledgers and derive the summary from them. Release PASS requires 100% applicable mission/real-effect coverage, zero mandatory FAIL/PARTIAL/BLOCKED, zero unresolved blocking finding, and cleanup PASS.
- The final clean Full User E2E and final clean Surface Reconciliation must bind to the **same exact HEAD**. If E2E remediation changes the public surface or contract, rerun Surface Reconciliation. Only after ChatGPT directly executes and finally audits both clean gates may the release Work Packet record terminal product-quality closure and freeze that HEAD.
