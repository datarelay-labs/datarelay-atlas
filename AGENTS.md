# Repository Engineering Rules

This repository follows the canonical Engineering System:
https://github.com/datarelay-labs/engineering-system

## Minimum context first

Always:
1. Read this `AGENTS.md`.
2. Read `.engineering/project.yaml`.

Then only when relevant:
3. For implementation/debugging/testing, read `.engineering/tests.yaml`.
4. For release/version/artifact work, read `.engineering/release.yaml`.
5. Read only the Engineering System standard/specification/ADR/runbook needed for the task.

Do not preload all standards, Wiki pages, archived changes, or historical discussions.

When explicitly resuming an existing workstream, resolve this repository first and load its single matching active AI Work Packet. Do not search other repositories or replay old chat history. Verify the actual branch/HEAD/state before acting.

## Execution rules

- For `project.user_facing: true`, require Surface Reconciliation and Full User E2E on the same exact candidate. Before either named gate, read its entire current repository-local contract and execute it as the applicable User/Operator/Admin persona on the actual public surface. Drivers and scripts may support real actions, never substitute synthetic PASS. Follow `standards/USER_ACCEPTANCE.md`; do not restart the complete gate after every individual fix. Close findings with affected reruns, then one fresh complete confirmation.
- **Product execution ownership / supervisor fallback:** the product context owns product work. Engineering System owns shared policy, adoption and systemic recovery, including uncontrolled Issue proliferation. Repair only what restores product autonomy, then return ownership. Never mutate an actively progressing owner-authorized worker dirty worktree or create a competing product lane.
- **Next-chat bootstrap fast path:** perform one bounded lookup when resuming durable work. With no ACTIVE packet, inspect current roadmap/Git/PR facts once and enter safe owner-authorized work; create or repair one packet when continuity needs it, not as a permission prerequisite. `NO_ACTIVE_PACKET` is a scheduling input, not a blocker.
- **DataRelay Atlas context bootstrap:** When the bound repository is under `datarelay-labs/` and a connected MCP/app named `DataRelay Atlas` exposes `bootstrap_datarelay_context`, perform one bounded bootstrap after resolving the exact repository and any current Work Packet relied on, before substantive implementation on a repository-level continue/resume or first entry into a durable workstream. Pass the exact repository and the workstream only when known. Reuse a fresh same-repository/workstream result in the current chat; do not repeat per turn, commit, or status check. Atlas is derived context only and never grants execution, permission, completion, or release authority. If the connector/tool is absent, authentication fails, the result is stale/`UNKNOWN`, or the call fails, continue immediately from canonical Git/GitHub/local evidence without retry loops, runtime switching, or asking the owner to repair Atlas. Direct Atlas/MCP-only questions may call the requested scoped tool without this bootstrap.
- **Verified next-chat resume:** re-read the current Issue and mutable repo/worktree/HEAD/profile once; if unchanged, enter the persisted Next Action immediately. Do not replay handoff validation, rewrite unchanged state or reconstruct transcripts. Revalidate only changed facts.
- **Execution profile authority:** `.engineering/execution-profile.yaml` selects the runtime unless the current explicit owner instruction overrides it. A continue/resume request authorizes direct implementation, testing, audit and ordinary Git/GitHub work; no additional magic phrase or alternate-runtime handoff is required. Historical prose and retired adapters do not select a runtime. Before claiming missing tools or access, discover the connected task-relevant tools and attempt a minimal authorized action when exposed. Reuse successful same-session, same-target, same-action evidence unless a fresh failure or scope change invalidates it. Report the exact attempted operation and observed error; unattempted is not denied. Existing approvals and explicit tool denials remain binding.
- For ordinary authenticated GitHub Issue/PR coordination, re-read the intended target, branch/HEAD and any packet relied on before writing; reconcile ambiguous outcomes before retrying. Stronger trusted boundaries apply only to effect classes production, destructive, credential/permission change, irreversible publication and release authority, or stricter project policy; follow `standards/SECURITY.md`.
- **Execute useful work continuously.** **Execution authority precedence:** the current explicit owner instruction governs, then the fresh Work Packet, execution profile and repository rules. Historical Issue comments and prior handoffs are evidence only and never execution authority. Bind the owner-selected repository and verify actual branch/HEAD/worktree before mutation; cross-project references never retarget work without explicit owner scope. Implement, test and audit in coherent batches; make measurable progress in the same turn. Repair stale coordination state within owner scope instead of stopping. Advance independent work during machine-observable waits instead of polling; after a bounded task return to roadmap priority. While this turn can execute, continue until the requested roadmap/release objective is complete, no safe runnable work remains, genuine owner input is needed, or a real platform/tool/safety boundary stops execution. Attempt the next safe authorized action before giving a progress-only report, and reselect roadmap work after each verified batch. If work remains when this turn ends, preserve actual progress and the precise Next Action and report the incomplete state without claiming background execution. `tools/work_admission.py disposition` is an optional pure scheduling diagnostic; its `FINAL_ALLOWED` value cannot control the lifecycle of a ChatGPT response. Status-only requests are exempt.
1. Classify the change and identify affected domains/contracts/security/operations.
2. For material design-bearing changes, apply the canonical `standards/DESIGN.md` minimal design gate before implementation.
3. Inspect relevant implementation and tests.
4. Make the smallest correct change.
5. Run the cheapest affected deterministic tests first.
6. PR validation should stay fast; do not run a full release suite merely because code changed.
7. Do not duplicate an equivalent native project CI gate.
8. A known blocking deterministic failure stops expensive downstream qualification.
9. Bug fixes require durable regression coverage whenever practical.
10. Never weaken a valid test merely to obtain PASS.
11. Before merge or terminal completion, inspect machine-observable PR review feedback. Fix and revalidate every actionable review finding, or explicitly disposition it with concise evidence when it is non-actionable, out of scope, or incorrect. Do not treat COMMENTED/advisory review state as automatic PASS.
12. Never claim release readiness without exact executable evidence.
13. Never reuse qualification evidence from a different source HEAD.


16. ChatGPT Chat may perform implementation and terminal audit in the same context, but self-report alone is never PASS. Terminal completion requires fresh exact-HEAD repository/PR/CI/test evidence and disposition of actionable findings. For HIGH/CRITICAL changes, widen security/rollback/runtime evidence and preserve mandatory human approval; fresh Chat/Codex/another reviewer is optional defense-in-depth rather than a quota dependency.

If the user reports an outage, degraded service, failed upgrade, data-loss risk, or other production-impacting symptom, switch to the canonical `standards/OPERATIONS.md` incident lifecycle. Preserve evidence before mutation and do not perform destructive/irreversible recovery without explicit approval unless an approved runbook authorizes it.

If mandatory engineering context is missing or contradictory, stop implementation and report the configuration defect instead of guessing.

When the user explicitly asks to apply/adopt/bootstrap the Engineering System to this repository, use the canonical `standards/ADOPTION.md` workflow: inventory first, classify existing rules, discover project-native tests/CI, preserve stricter project invariants, use deterministic bootstrap for missing common surfaces, and qualify the adoption before reporting PASS. If this repository is already pinned to an older managed Engineering System version, use the fail-closed managed upgrade workflow instead of rerunning initial bootstrap.

Tool-specific adapters must not weaken these rules.


## DataRelay Atlas product invariants

1. GitHub/OpenSpec/code/tests/ADR/CI are canonical engineering state. Atlas knowledge projections are derived and rebuildable.
2. `datarelay-labs/engineering-system` is the canonical methodology. Atlas may apply, observe, validate, visualize, or automate that methodology but must not silently fork or redefine it.
3. DataRelay Atlas is the product boundary. Athena is historical migration evidence only (ADR-0004), not a product identity and not a build/test/deploy/runtime dependency.
4. Provenance must be preserved for derived engineering knowledge: repository, ref, source path, and source revision where available.
5. The initial product scope is self-hosted and single-organization. Multi-tenant SaaS, generic enterprise search, Git hosting, CI/CD replacement, issue-tracker replacement, and general-purpose chat/RAG are out of scope unless explicitly approved.
6. Prefer existing GitHub and project-native capabilities over custom infrastructure.
7. Product decisions belong in canonical repository artifacts. Do not promote uncertain AI discussion into accepted product truth.

## Implementation and audit contract





The selected runtime performs implementation, deterministic testing, and terminal audit. Terminal PASS requires current exact-HEAD evidence, required CI/review state, and disposition of actionable findings; self-report alone is never sufficient. HIGH/CRITICAL or production/security-sensitive work requires deeper machine evidence and any applicable human approval. Codex or another independent reviewer is optional defense-in-depth/escalation, not a default completion dependency.
