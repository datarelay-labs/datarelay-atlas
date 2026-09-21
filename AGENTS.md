# DataRelay Atlas Repository Engineering Rules

This repository follows the canonical Engineering System:
https://github.com/datarelay-labs/engineering-system

## Minimum context first

Always:
1. Read this `AGENTS.md`.
2. Read `.engineering/project.yaml`.

Then only when relevant:
3. For implementation/debugging/testing, read `.engineering/tests.yaml`.
4. For release/version/artifact work, read `.engineering/release.yaml`.
5. Read only the Engineering System standard, Product Charter, specification, ADR, or runbook needed for the task.

Do not preload all standards, Wiki pages, archived changes, or historical discussions.

When explicitly resuming an existing workstream, resolve this repository first and load its single matching active AI Work Packet. Verify the actual branch/HEAD/state before acting.

## DataRelay Atlas product invariants

1. GitHub/OpenSpec/code/tests/ADR/CI are canonical engineering state. Atlas knowledge projections are derived and rebuildable.
2. `datarelay-labs/engineering-system` is the canonical methodology. Atlas may apply, observe, validate, visualize, or automate that methodology but must not silently fork or redefine it.
3. DataRelay Atlas is the product boundary. Athena is a replaceable integration/runtime dependency, not the product identity or canonical source of truth.
4. Provenance must be preserved for derived engineering knowledge: repository, ref, source path, and source revision where available.
5. The initial product scope is self-hosted and single-organization. Multi-tenant SaaS, generic enterprise search, Git hosting, CI/CD replacement, issue-tracker replacement, and general-purpose chat/RAG are out of scope unless explicitly approved.
6. Prefer existing GitHub and project-native capabilities over custom infrastructure.
7. Product decisions belong in canonical repository artifacts. Do not promote uncertain AI discussion into accepted product truth.

## Execution rules

1. Classify the change and identify affected domains/contracts/security/operations.
2. For material design-bearing changes, apply canonical `standards/DESIGN.md` before implementation.
3. Inspect relevant implementation and tests.
4. Make the smallest correct change.
5. Run the cheapest affected deterministic tests first.
6. Keep ordinary PR validation fast; full qualification belongs near release.
7. Do not duplicate an equivalent native/shared gate.
8. A blocking deterministic failure stops expensive downstream qualification.
9. Bug fixes require durable regression coverage whenever practical.
10. Never weaken a valid test merely to obtain PASS.
11. Before merge or terminal completion, inspect machine-observable PR review feedback and fix/revalidate or explicitly disposition every actionable finding.
12. Never claim release readiness without exact executable evidence or reuse evidence from another HEAD.
13. For production-impacting incidents, enter the canonical Operations lifecycle and preserve evidence before mutation.

If mandatory engineering context is missing or contradictory, stop implementation and report the configuration defect instead of guessing.

Tool-specific adapters may adapt syntax but must not weaken these rules.
