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

## Optional owner-operated browser execution and ChatGPT evidence audit

- **Opt-in:** the currently authenticated owner must explicitly select `HUMAN_OWNER_WITH_CHATGPT_AUDIT` for the particular candidate HEAD, gate and run; default is `AI_DIRECT`. ChatGPT publishes the exact immutable candidate source/archive, committed contract SHA-256, isolated non-production preview URL(s), run ID and scope before the owner begins. Do not use production/customer data or expose the loopback-only preview publicly.
- **Real actions:** the owner personally uses Chrome/Chromium for Mission A, B, C and persistence/recovery. Document each scenario's UTC timestamp, action intent, observed screen text/status, failed/recovery variants and real-effect readback; capture screen evidence without tokens, keys or private sources. Specifically verify that a genuine browser POST/write attempt is rejected and no persistent authoritative state changed. Source inspection, CI, arbitrary API calls and scripted browser replays do not replace the human's actions.
- **Evidence:** preserve owner instruction, owner observation and independent ChatGPT audit as separate provenance-bound references under this same GitHub repository; include exact candidate HEAD, contract SHA, run ID, Chrome version/target/process observation, per-scenario IDs/expected/observed/results, screenshot SHA-256 or readback references, findings and clean run-owned resource shutdown. ChatGPT cross-checks actual owner identity/provenance and evidence; GitHub links or caller-authored JSON flags alone cannot prove the human acted.
- **Fail closed:** if owner identity or execution provenance is unavailable, a scenario is not observed, a different HEAD is served, a readback disagrees, or prior findings are unresolved, retain BLOCKED/UNVERIFIED. Product quality requires a new complete clean confirmation pass and independent ChatGPT audit. Structural validator success is **not** human attestation, release approval, or candidate-freeze authority.

## Pass rule

PASS requires all missions on the same exact candidate, browser-process evidence, no API/source-inspection substitution for browser actions, and recorded cleanup truth. Any browser-visible failure, stale candidate, hidden API fallback, fabricated lifecycle PASS, or authority confusion is REWORK.

## Engineering System User Acceptance v2

- **Default AI_DIRECT:** ChatGPT itself performs each real-browser mission and audits the result. **With explicit owner opt-in for this candidate/run**, `HUMAN_OWNER_WITH_CHATGPT_AUDIT` lets the authenticated owner personally perform the real-browser actions while ChatGPT remains the independent final evidence auditor. This does not override the runtime selection, turn an automated replay into a human action, or authorize release. Coding agents, alternate models, scripted replays, CI and automated harnesses cannot substitute for either acting user.
- Run mission-first, black-box, and real-effect. The persona starts without source/test answer-key knowledge, follows browser-visible discovery/guidance, and verifies the real rendered/derived outcome and authority labeling.
- Include realistic mistakes/recovery for applicable missions: invalid or empty search, unknown project/route, stale or invalid lifecycle evidence, reload/new context, unavailable derived source, and rejected write attempts. Recovery must be understandable from the product surface rather than hidden implementation knowledge.
- A finding is not a stop condition. Continue every safe independent mission, freeze findings at pass end, batch-remediate, and restart invalidated Full User E2E from the beginning on the new candidate.
- Repeat state-sensitive missions across meaningful reload/new-context/source-state variants when one success could hide stale-state or persistence defects. Maintain run-owned cleanup and report environment/tooling blockage honestly.
- Retain machine-readable scenario/findings ledgers and derive the summary from them. Release PASS requires 100% applicable mission/real-effect coverage, zero mandatory FAIL/PARTIAL/BLOCKED, zero unresolved blocking finding, and cleanup PASS.
- The final clean Full User E2E and final clean Surface Reconciliation must bind to the **same exact HEAD**. If E2E remediation changes the public surface or contract, rerun Surface Reconciliation. Only after each gate is executed in its explicitly selected real-user mode and ChatGPT has independently audited both complete clean gates may a subsequent authorized release lifecycle consider product-quality closure; structural JSON PASS or the owner's self-reported PASS alone cannot freeze or release a candidate.
