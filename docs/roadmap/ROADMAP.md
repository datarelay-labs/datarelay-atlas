# DataRelay Atlas — Canonical Product Roadmap

Status: Canonical roadmap
Last realigned: 2026-10-01

This roadmap defines product completion by **product layer and end-to-end outcome**, not by feature count or delivery date.

The Product Charter is authoritative for product scope. GitHub Issue #10 may retain chronological roadmap history and experiments, but historical additions do not override the current Core / Automation Extension / Experimental boundary.

## Product roadmap model

    Atlas Core
      Knowledge Plane
          +
      Lifecycle Plane
          +
      AI Context & Trust Plane
          +
      Human Interface & Operations
          |
          +----> complete product

    Optional Automation Extension
      dependency/readiness + handoff/audit + concurrency
      provider capability/capacity + bounded decision support
          |
          +----> consumes Atlas Core trust/state

    Experimental / Optional
      Stagehand semantic A/B, context compression,
      provider optimization expansion, personal-knowledge expansion,
      provider-specific research, unrelated research labs

Rule: an Automation Extension or Experimental item cannot become a Core release blocker unless an explicit product decision promotes it.

# Atlas Core

## Phase 0 — Product Foundation

Completed foundation:

- Engineering System adoption
- product charter and architecture boundary
- repository/documentation foundations
- Athena PoC preservation, license/ownership audit, capability absorption, and runtime independence
- source/provider/provenance contracts
- canonical-vs-derived authority boundary

Exit: Atlas owns its product/runtime contracts and can be resumed without Athena as a runtime dependency.

Evidence retained from the original roadmap:

- license audit: `integrations/athena/DEPENDENCY-LICENSE-AUDIT.md`
- PoC ownership: `integrations/athena/POC-MIGRATION.md`
- provenance contracts: ADR-0003 + `docs/contracts/source-provider-provenance.md`
- absorption/retirement: ADR-0004 + `docs/migration/athena-capability-inventory.md` + `docs/migration/athena-retirement-checklist.md`
- redistribution review notes remain historical evidence and do not reintroduce an Athena runtime dependency

## Phase 1 — Project Registry & Canonical Sync

Completed foundation:

- project/repository registration
- Engineering System adoption metadata
- canonical source configuration
- authenticated GitHub synchronization
- source revision/provenance retention
- project-scoped namespaces
- deterministic re-sync/rebuild
- durable state contract

Exit: a project can be registered and its selected canonical knowledge can be reproducibly projected with provenance.

Phase 1 qualification remains implementation + deterministic tests + operator E2E against `datarelay-labs/datarelay-atlas`; roadmap text alone is never release evidence.

## Phase 2 — Knowledge Retrieval, MCP & Human Navigation

Core capabilities:

- exact/keyword retrieval
- semantic retrieval
- provenance-bearing responses
- project-scoped retrieval
- cross-project retrieval with explicit scope
- authenticated HTTPS MCP
- approved MCP-client integration; no specific coding-agent runtime is a Core requirement
- Atlas-owned Human UI for attributable project/source/projection/search/lifecycle navigation
- remote exposure/authentication hardening where production deployment requires it

Search/Ask contract:

- Atlas Core owns retrieval, provenance, integrity, scope, and context packaging.
- Generic LLM answer synthesis is performed by the client unless a future bounded Atlas-native synthesis contract is explicitly approved.

Exit: a human and an approved MCP client can retrieve the same current, attributable context from the same candidate.

Human UI evidence remains anchored in `atlas/web_ui.py`, `tests/test_web_ui.py`, `docs/contracts/ATLAS_SURFACE_RECONCILIATION.md`, and `docs/contracts/ATLAS_FULL_USER_E2E.md`. Implementation presence alone does not establish browser release readiness.

## Phase 3 — Lifecycle Intelligence

Core capabilities:

- PR/CI/test/release state normalization
- Engineering System compliance visibility
- current Work Packet/workstream visibility
- observed vs inferred vs unknown state
- stale/different-HEAD evidence distinction
- validation/release evidence navigation
- human-equivalent user-test lifecycle visibility where required: Surface Reconciliation vs Full User E2E, required/configured/executed status, exact evidence HEAD, and stale/different-HEAD distinction
- distinguish CI/static contract configuration from actual public-user-surface execution evidence; never infer execution PASS from contract presence

Exit: Atlas shows both what a project knows and where the current candidate is in its engineering lifecycle.

## Phase 4 — Core Derived Engineering Intelligence

Required Core derived capabilities:

- cross-project links with explicit provenance/scope
- decision backlinks
- contradiction detection
- stale/unknown context detection
- knowledge-gap detection

Optional derived capabilities:

- richer concept/entity extraction
- unanswered-question workflows
- generated summaries beyond bounded navigation needs

Exit: derived intelligence improves navigation and reasoning without becoming a competing source of truth.

## Phase 5 — Production Hardening

Core capabilities:

- production deployment contract
- health/observability
- backup/restore and restore testing
- upgrade/rollback
- security review
- dependency/SBOM/provenance policy
- multi-project operational E2E
- production restart/recovery evidence
- loopback-only production Human UI with SSH-authenticated operator access
- before any user-facing Atlas production release: mandatory actual-browser Surface Reconciliation and Full User E2E on the same exact release candidate under ADR-0016 and the active Engineering System release standard

Exit: the self-hosted single-organization Core product is supportable in production.

## Atlas Core finish line

A Core release candidate must prove this one end-to-end journey:

1. register a representative project;
2. synchronize canonical sources and retain attributable revisions;
3. retrieve exact + semantic project context and explicitly scoped cross-project context;
4. return provenance and canonical-source identity for retrieved context;
5. present materially equivalent trusted context in Human UI and an approved MCP client;
6. present current lifecycle/release state, including stale/unknown/contradictory evidence;
7. surface Core decision backlinks / contradictions / knowledge gaps;
8. complete deploy -> health -> backup/restore test -> upgrade/rollback -> restart/recovery;
9. pass the active Engineering System exact-candidate machine gates plus actual-browser Surface Reconciliation and Full User E2E on the same exact release candidate.

Only this journey, plus required security/compliance gates, determines Core product completion.

Deterministic integrated pre-release evidence is defined by `docs/contracts/ATLAS_CORE_PRODUCT_E2E.md`. It validates the integrated Core planes but never substitutes for the exact-candidate actual-browser Surface Reconciliation / Full User E2E gates.

# Automation Extension

Automation extends Atlas Core; it does not define Atlas Core.

## A1 — Durable AI engineering handoff & trust loop

- GitHub Work Packet as durable coordination authority
- Chat-primary/provider-neutral implementation handoff
- independent verification/audit
- bounded rework/pass/handback state
- no provider-specific runtime may become hidden execution authority

Current related roadmap: #143 and the existing Work Controller / audit foundations.

## A2 — Dependency/readiness & bounded concurrency

- dependency/readiness graph
- execution admission
- conflict serialization
- bounded parallel ready-node selection
- measured concurrency rather than broad autonomous scale-out

Current related roadmap:

- #53 Dependency / Readiness Graph — COMPLETE
- #58 Measured Multi-agent Concurrency — COMPLETE

## A3 — Provider capability & capacity evidence

- normalized provider capability descriptors
- policy/trust/budget eligibility
- observed health/reset/latency/quota evidence where legitimately available
- attributable route outcome evidence
- fail closed when provider policy or capability is unknown

Current related roadmap:

- #54 Provider Capability Adapter — COMPLETE
- #55 Provider Capacity Broker — MEASUREMENT_READY

Rule: do not add synthetic routing strategies merely to complete a checklist. Expand #55 only when real measured evidence justifies a policy decision.

## A4 — Bounded Decision Plane

- candidate set is prepared by deterministic policy/capability/trust gates
- a cheap decision model may rank only eligible options
- deterministic post-validation remains authoritative
- shadow -> replay -> canary -> limited active -> measured expansion

Current related roadmap:

- #56 Decision Plane — MEASUREMENT_READY

Rule: acceptance requires real dogfood evidence of equal-or-better verified success plus useful cost/time reduction. No speculative expansion is required.

## A5 — Instruction / harness governance

- model/provider-aware instructions
- trust-bound context validity
- explicit transition effects and revalidation
- no stale semantic audit reuse across materially changed execution identity

Current related roadmap:

- #57 Model-Aware Instruction Governance — COMPLETE

# Experimental / Optional Roadmap

These capabilities may be valuable, but are not Core release blockers.

## Browser semantic verification

- #19 Stagehand live semantic A/B remains HUMAN_REQUIRED on an approved model credential; resolving that external credential dependency is not a Core release prerequisite.
- Existing deterministic browser verification remains useful; the live semantic comparison is an experiment, not a Core completion gate.

## Personal Knowledge Plane

- #59 implementation is complete at roadmap level.
- Personal knowledge remains a distinct non-authoritative source class.
- Expansion into a broad Second Brain is optional and must not weaken engineering-source authority or become a Core release prerequisite.

## Context compression / provider-specific optimization

- #76 remains DEFERRED.
- Cursor-specific usage/runtime research is not a current product requirement.
- Context compression may be evaluated as a pluggable optimization beneath Atlas trust boundaries.
- Provider plan price/quota observations are time-sensitive measurements, not product constants.

## Research labs

- Domain-specific research projects such as #144 Empirical Trading Research Lab may reuse Atlas knowledge/evidence primitives.
- They are separate products/experiments and do not belong on the Atlas Core critical path.

# Scope classification rule

Every new roadmap issue must declare one of:

- PRODUCT_LAYER=CORE
- PRODUCT_LAYER=AUTOMATION_EXTENSION
- PRODUCT_LAYER=EXPERIMENTAL_OPTIONAL

A new issue without a declared layer cannot redefine the Core finish line.

# Deferred unless explicitly approved

- multi-tenant SaaS
- hosted commercial service
- generic enterprise search across arbitrary business systems
- Git hosting
- CI/CD replacement
- issue tracker replacement
- autonomous coding-agent replacement
- generalized RAG/chat platform
- mandatory provider-quota/cost optimizer
- provider-specific execution runtime as a Core dependency
