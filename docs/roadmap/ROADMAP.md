# DataRelay Atlas — Initial Roadmap

This roadmap defines bounded product phases, not delivery dates. Each phase must satisfy the Engineering System design/validation gates before the next phase expands scope.

## Phase 0 — Product Foundation

- Engineering System 1.6 adoption
- Product Charter and architecture boundary
- repository and documentation foundations
- dependency/license compatibility audit
- evaluate and classify existing Athena PoC assets
- define source/provider and provenance contracts

Exit: canonical product boundaries are clear and the repository can be safely resumed by AI agents.

## Phase 1 — Project Registry & Canonical Sync

- register a project/repository
- read Engineering System adoption metadata
- configure canonical source paths
- authenticated GitHub synchronization
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
