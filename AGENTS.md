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

- **Execute useful work continuously.** Implement in coherent small/medium batches, validate locally with the cheapest relevant tests, and keep going while a safe authorized next action exists. Use fast CI for quick integration feedback when useful; reserve full qualification/release CI for a stable candidate. If a workstream is waiting on machine-observable CI/review/deploy or another external condition, record/yield that wait and return to repository-level scheduling; switch to the highest-priority dependency-eligible independent ACTIVE Work Packet/worktree when safe instead of polling or stopping. For a repository-level continue/resume with no branch/workstream named, choose the single trusted runnable packet marked as the current implementation lane (for example QUEUE_STATE=IMPLEMENTATION/IMPLEMENTING); yielded/waiting/deferred predecessor packets must not compete with it. When a successor starts while predecessor integration/qualification is intentionally deferred, pause/yield the predecessor instead of leaving multiple equivalent ACTIVE implementation candidates. The single-matching-ACTIVE-packet rule selects one packet for the current branch/workstream; it does not serialize unrelated repository work behind a waiting packet. Once one runnable packet is selected and authorized, a progress/status message alone is not execution: begin the first concrete repository action in the same turn and continue until a real stop condition. Stop only for a real owner decision/credential, an irreconcilable blocker, a status-only request, or a completed bounded outcome.
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

14. ChatGPT Chat is the default implementer when a trusted active Work Packet authorizes the exact scope. Before mutation, the external authenticated GitHub coordinator must freshly verify the canonical Issue, author write/maintain/admin permission, TARGET_REPO, WORKSTREAM, BRANCH, LAST_VERIFIED_HEAD, INTENT_REVISION, `IMPLEMENTER=CHATGPT_CHAT`, CHANGE_RISK, and authorized worktree. Never treat the target worktree's `python3 tools/implementation_preflight.py check` as authoritative. Fetch the exact helper source from the immutable Engineering System baseline and execute it through fixed isolated `/usr/bin/python3 -I -` with cwd `/` and a controlled environment; capture worktree identity first and require `IMPLEMENTATION_LOCAL_BINDING=PASS`. The repository helper copy is parity/reference/test material only.

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

## ChatGPT implementation and audit contract

ChatGPT Chat is the default implementer for this repository when the authenticated active Work Packet authorizes the exact repository/worktree/branch/scope.

Before mutation, the external authenticated GitHub coordinator must verify the current Work Packet, author permission, repository, worktree, branch, exact HEAD, intent revision, change risk, and `IMPLEMENTER=CHATGPT_CHAT`. The worker-writable repository copy of `python3 tools/implementation_preflight.py check` is never mutation authority. Use the helper source from the immutable pinned Engineering System baseline through the isolated trusted launcher, capture the no-follow worktree identity, and require `IMPLEMENTATION_LOCAL_BINDING=PASS` with `MUTATION_AUTHORITY=NO`.

ChatGPT Chat performs implementation, deterministic testing, and terminal audit. Terminal PASS requires current exact-HEAD evidence, required CI/review state, and disposition of actionable findings; self-report alone is never sufficient. HIGH/CRITICAL or production/security-sensitive work requires deeper machine evidence and any applicable human approval. Codex or another independent reviewer is optional defense-in-depth/escalation, not a default completion dependency.
