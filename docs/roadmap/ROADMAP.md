# DataRelay Atlas — Initial Roadmap

This roadmap defines bounded product phases, not delivery dates. Each phase must satisfy the Engineering System design/validation gates before the next phase expands scope.

## Phase 0 — Product Foundation

- [x] Engineering System 1.6 adoption
- [x] Product Charter and architecture boundary
- [x] repository and documentation foundations
- [x] create Data Relay Athena upstream-tracking fork and pin source snapshots
- [x] preserve the validated Athena Engineering Knowledge PoC at an immutable commit/tag
- [x] complete dependency/license compatibility audit beyond Athena's top-level Apache-2.0 license
- [x] classify and migrate Athena PoC changes into Atlas-owned vs generic fork-owned code
- [x] define source/provider and provenance contracts
- [x] supersede long-term Athena external-runtime strategy (ADR-0004)
- [x] absorb required PoC capabilities into Atlas-owned code with independence gate

Exit: canonical product boundaries are clear, Athena is not an Atlas runtime dependency, and the repository can be safely resumed by AI agents.

Phase 0 evidence:

- license audit: `integrations/athena/DEPENDENCY-LICENSE-AUDIT.md`
- PoC ownership: `integrations/athena/POC-MIGRATION.md`
- provenance contracts: ADR-0003 + `docs/contracts/source-provider-provenance.md`
- absorption/retirement: ADR-0004 + `docs/migration/athena-capability-inventory.md` + `docs/migration/athena-retirement-checklist.md`
- open redistribution review items remain documented for any future image reuse (Wiki.js AGPL-3.0; Bun LGPL-linked components) and do not reintroduce an Athena-repo dependency

## Phase 1 — Project Registry & Canonical Sync

- register a project/repository
- read Engineering System adoption metadata
- configure canonical source paths
- authenticated GitHub synchronization (Atlas-owned `atlas/` library as starting implementation)
- source revision/provenance retention
- project-scoped namespace
- deterministic re-sync/rebuild behavior

Exit: one project can be registered and its selected canonical knowledge is reproducibly projected.

## Phase 2 — Knowledge Retrieval & MCP

- exact/keyword retrieval
- semantic retrieval
- provenance-bearing responses
- project-scoped retrieval
- cross-project retrieval with explicit scope
- authenticated HTTPS MCP
- Cursor and ChatGPT integration
- human browsing/navigation

Exit: humans and AI clients retrieve the same current, attributable engineering context.

## Phase 3 — Lifecycle Intelligence

- PR/CI/test/release state normalization
- Engineering System compliance visibility
- current workstream/AI Work Packet visibility
- stale/unknown state distinction
- validation/release evidence navigation

Exit: Atlas can show what a project knows and where it is in the engineering lifecycle.

## Phase 4 — Derived Engineering Intelligence

- concepts/entities
- cross-project links
- decision backlinks
- contradiction detection
- knowledge-gap detection
- unanswered-question tracking
- optional derived summaries

Exit: synthesis improves navigation and reasoning without becoming a competing source of truth.

## Phase 5 — Production Hardening

- production deployment contract
- backup/restore and restore testing
- upgrade/rollback
- observability/health
- security review
- dependency/SBOM/provenance policy
- multi-project operational E2E

Exit: self-hosted single-organization production release is supportable.

## Deferred unless explicitly approved

- multi-tenant SaaS
- hosted commercial service
- generic enterprise search across arbitrary business systems
- Git hosting
- CI/CD replacement
- issue tracker replacement
- autonomous coding-agent replacement
- generalized RAG/chat platform
