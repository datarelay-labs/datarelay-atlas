# DataRelay Atlas — Canonical Product Roadmap

Status: Canonical roadmap
Last realigned: 2026-10-03

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

# Post-v0.1.0 — Usage before new capability

Atlas Core v0.1.0 is released. Post-release execution prioritizes **recurring real use and attributable evidence flow** before adding new agent capabilities.

## Verified Continuous Engineering Memory

Confirmed product direction: Atlas should reduce repeated owner explanation and repeated context reconstruction across approved AI clients without becoming a generic chat archive or duplicating Engineering System authority.

Canonical engineering truth remains GitHub/OpenSpec/code/tests/ADR/CI and Engineering System. Atlas memory is provider-neutral derived context and may never outrank current canonical state.

Confirmed capability roadmap:

1. **Automatic Task Context Bootstrap** — one bounded current project/repository/workstream bootstrap for approved AI clients.
2. **Continuous Canonical/Event Ingestion** — reconcile approved GitHub, Engineering System, CI/test/release/runtime evidence without owner “save this” instructions.
3. **Scoped Automatic Memory Candidate Extraction** — bounded non-authoritative OWNER_PREFERENCE, VALIDATED_FINDING, LESSON_LEARNED, RUN_SUMMARY, FUTURE_IDEA and REFERENCE_FACT candidates.
4. **Provenance/Citation-bound Memory** — retain source identity/revision/digest where available.
5. **Recall-time Current-state Revalidation** — current canonical state wins; stale/contradicted/unavailable/superseded memory stays explicit.
6. **Dedup + Consolidation + Supersession + TTL** — prevent unbounded duplicate memory and expire by type/policy.
7. **Progressive / JIT Context Retrieval** — return a small context index first and fetch deeper source/evidence only when needed.
8. **User Review / Correct / Forget / Pin** — owner control for derived/personal memory without indirect canonical mutation.
9. **Memory & Context Effectiveness Evals** — measure recall, stale/irrelevant injection, repeated-owner-explanation count, context size/cost and downstream first-pass success.

Implementation order:

- **M1 — Context Bootstrap + Revalidation + JIT retrieval**;
- **M2 — Continuous ingestion + candidate memory store**;
- **M3 — Consolidation / temporal validity / TTL**;
- **M4 — User memory controls**;
- **M5 — Memory effectiveness evals**.

Explicit non-goals: full raw chat archive, procedural/rule learning that duplicates Engineering System, external memory SaaS runtime dependency, mandatory new graph/vector database, automatic canonical promotion, whole-memory prompt injection, or provider-specific memory silos.

## U1 — Portfolio operational adoption

Status: **COMPLETE via #256**.

- prod Atlas now registers Engineering System, DataRelay Link, DataRelay Control, and DataRelay Grant in addition to Atlas and Personal Knowledge;
- selected public GitHub sources sync with immutable source revisions and preserve canonical-vs-derived separation;
- cross-project engineering retrieval and Human UI portfolio visibility pass on production;
- DataRelay Control adoption metadata remains available through live read-only adoption inspection, while its secret-like metadata body is intentionally excluded from persisted knowledge projection;
- post-adoption backup/restore passes with six registered projects.

This closes the first utilization gap: Atlas now has real portfolio state to serve rather than only its own repository.

## U2 — Engineering System evidence federation

Status: **COMPLETE via #261 / PR #262**.

Engineering System remains authoritative for how repositories work. Atlas should consume its non-sensitive outputs instead of recreating them.

Initial evidence families to federate when repositories actually emit them:

- provider-neutral efficiency telemetry and outcome reports;
- behavior-eval results;
- exact-HEAD trust-evidence receipts;
- runtime evidence / runtime-contract state;
- Work Packet and lifecycle state already represented by the Engineering System/GitHub authority model.

Federation rules:

- validate against the Engineering System schema/revision that produced the evidence;
- retain repository, workstream, exact subject HEAD, timestamp, provenance, and evidence authority;
- expose current / stale / unknown / unavailable distinctions across projects;
- retain only bounded evidence metadata; never ingest prompts, transcripts, raw tool payloads, credentials, or private reasoning;
- remain read-only/evidence-only: federation grants no permission, execution, merge, release, or deployment authority.

Exit: Atlas can answer portfolio-level questions such as which projects have current exact-HEAD evidence, where evidence is stale/missing, and how verified engineering outcomes change over time without becoming a competing methodology source.

## U3 — Approved-client consumption

Atlas value is not established merely because the MCP server is healthy.

- make Atlas retrieval available in the owner's daily approved MCP client where that client supports the existing authenticated read surface;
- distinguish server readiness from client connection/availability;
- record successful real retrieval from an approved client as utilization evidence;
- do not build a generic Atlas chat UI merely because one client is not connected.

This is primarily an integration/adoption step, not a new source-of-truth layer.

## U4 — Measurement-driven automation

#55 Provider Capacity Broker and #56 Decision Plane remain measurement-gated.

- prefer new normalized evidence from live Work Packets and federated Engineering System outputs over repeated manual historical backfill;
- do not invent provider capacity, cost, ranking, replay, or Decision Plane evidence;
- expand active routing/decision policy only after comparable real outcomes justify it.

## Ownership rule — Engineering System vs Atlas

**Engineering System defines how a repository must work. Atlas observes, correlates, measures, and serves what repositories currently prove.**

Atlas must not create competing versions of:

- the Work Packet / session-continuity standard;
- context epoch/compiler/canary/economics contracts;
- skills/hooks/permission authority;
- behavior-eval framework;
- runtime/release/security authority.

Atlas may index and aggregate the resulting attributable evidence, detect stale/contradictory/unknown state, and expose bounded context to humans and approved AI clients.

Current post-release execution order:

1. U1 portfolio operational adoption — COMPLETE;
2. U2 Engineering System evidence federation;
3. U3 approved-client consumption — may proceed in parallel where the client surface is available;
4. accumulate real comparable engineering outcomes;
5. advance #55/#56 only from measured evidence;
6. evaluate optional/provider-specific experiments only after the above loop is useful in daily work.

# Portfolio Commercial Validation Projection

This section is a **non-authoritative Atlas portfolio projection** of commercial-validation hypotheses and evidence. It is not the canonical product roadmap for DataRelay Link, DataRelay Control, or DataRelay Grant.

Product decisions remain owned by each product's canonical repository. Atlas may aggregate attributable commercial evidence and propose prioritization context, but an owning-repository decision always wins if this projection is stale or contradictory. GitHub/OpenSpec/code/tests/ADR/CI remain authoritative for engineering state, and Engineering System remains authoritative for engineering methodology.

## Shared commercial evidence contract

Every product track records:

- **Problem** — the concrete user/business pain being tested.
- **Buyer** — the role or organization expected to approve spend.
- **Urgency** — why the problem is worth solving now.
- **Existing Alternative** — what the target user does today.
- **Willingness to Pay (WTP)** — observed budget/price signal; unknown until evidenced.
- **Distribution** — how the product can realistically reach the buyer.
- **Evidence** — attributable customer conversation, PoC/request, procurement action, paid use, renewal/referral, or equivalent market signal.
- **Moat** — defensibility hypothesis such as workflow depth, integration cost, know-how, IP, or accumulated evidence.

Commercial validation is tracked on **independent evidence dimensions**, not one forced linear stage:

- **Problem evidence:** `IDEA -> HYPOTHESIS -> CUSTOMER_CONFIRMED -> REPEATED_PROBLEM`
- **Product/user evidence:** `UNVALIDATED -> PILOT_VALIDATED -> USER_VALIDATED -> REPEATABLE_USE`
- **Commercial evidence:** `WTP_UNKNOWN -> PROCUREMENT_SIGNAL -> PAID_SIGNAL -> REPEATABLE_REVENUE`
- **Engineering readiness:** derived from the owning repository's roadmap/release evidence; it is not a commercial stage.

Rules:

- `HYPOTHESIS` is not evidence.
- Positive feedback without a concrete action is not `PAID_SIGNAL`.
- Free PoC/use may advance product/user evidence without advancing commercial evidence.
- A paid commitment may advance commercial evidence without claiming broad product validation.
- Paid evidence must preserve the actual price/commitment context; discounts do not silently redefine list-price assumptions.
- Atlas may store bounded provenance/references to commercial evidence, but should not ingest unnecessary sensitive customer content.
- A commercial stage never overrides security, correctness, exact-HEAD validation, release, or owner-approval gates.
- These tracks do not commit DataRelay to hosted SaaS, multi-tenancy, a pricing model, or an external product launch.

## DataRelay Link — Commercial Projection

**Owning product authority:** `datarelay-labs/datarelay-link`, current product roadmap #45.

**Atlas projection:** Problem = `HYPOTHESIS`; Product/User = `UNVALIDATED`; Commercial = `WTP_UNKNOWN`.

- **Problem hypothesis:** teams need secure, low-friction remote access/service exposure without expanding inbound firewall/public-service footprint or relying on brittle manual tunnel operations.
- **Buyer hypothesis:** security, network, infrastructure/platform operations, and managed-service teams.
- **Urgency hypothesis:** remote administration and controlled external access are recurring operational needs where setup friction and exposed ingress create cost/risk.
- **Existing Alternative:** VPN/ZTNA, bastion/jump hosts, reverse tunnels, firewall/NAT publishing, vendor remote-access products, or manual SSH/tunnel workflows.
- **WTP:** `UNKNOWN / TO_VALIDATE`.
- **Distribution hypothesis:** founder/SE industry network, security/network partners, MSP/MSSP relationships, and targeted early-access deployments.
- **Evidence:** `TO_VALIDATE` — do not treat internal development success, feature completeness, or friendly feedback as market proof.
- **Moat hypothesis:** integrated access-policy + relay workflow, zero-touch/managed-host operations, deployment know-how, and future cross-product integration. Commercial defensibility remains `TO_VALIDATE`.

Next commercial exit: obtain attributable customer confirmation of the problem and buyer, then request a concrete paid or procurement commitment before broad feature expansion.

## DataRelay Control — Commercial Projection

**Owning product authority:** `datarelay-labs/datarelay-control`, current v1 product roadmap #91.

**Atlas projection:** Problem = `HYPOTHESIS`; Product/User = `UNVALIDATED`; Commercial = `WTP_UNKNOWN`.

- **Problem hypothesis:** teams managing many security/data integrations need one operational control plane for routing, transformation, delivery health, governance, and connector lifecycle instead of fragmented per-integration tooling.
- **Buyer hypothesis:** security operations/platform engineering, integration operations, MSSP/MDR platform teams, and technical owners responsible for reliable data delivery.
- **Urgency hypothesis:** integration sprawl creates recurring diagnosis, mapping, delivery, and change-governance cost.
- **Existing Alternative:** custom scripts, SIEM/vendor-native pipelines, generic ETL/iPaaS, point connectors, and manual operational runbooks.
- **WTP:** `UNKNOWN / TO_VALIDATE`.
- **Distribution hypothesis:** existing security ecosystem relationships, partner-led deployments, and focused pilots around an expensive integration/operations pain.
- **Evidence:** `TO_VALIDATE` — engineering roadmap completion and browser/user E2E are product-quality evidence, not commercial demand evidence.
- **Moat hypothesis:** security-domain connector semantics, route/transform/delivery observability, governance depth, and accumulated connector/operational knowledge. Commercial defensibility remains `TO_VALIDATE`.

Next commercial exit: identify one repeated integration/operations pain with a named buyer and convert a bounded pilot into a paid signal.

## DataRelay Grant — Commercial Projection

**Owning product authority:** `datarelay-labs/datarelay-grant`. The repository is currently a pre-release product definition; no canonical commercial roadmap is assumed by Atlas.

**Atlas projection:** Problem = `HYPOTHESIS`; Product/User = `UNVALIDATED`; Commercial = `WTP_UNKNOWN`.

- **Problem hypothesis:** organizations need a reusable approval/governance layer for sensitive or privileged actions instead of embedding inconsistent approval logic inside every product/workflow.
- **Buyer hypothesis:** security/IT governance, platform owners, compliance-sensitive operations, and product teams that need auditable human approval.
- **Urgency hypothesis:** privileged automation and AI-assisted operations increase the need for explicit, attributable approval boundaries.
- **Existing Alternative:** ticketing/manual approvals, chat/email sign-off, workflow engines, IAM/PAM approval features, or bespoke application logic.
- **WTP:** `UNKNOWN / TO_VALIDATE`.
- **Distribution hypothesis:** cross-sell/attach to DataRelay workflows plus targeted security/governance design partners.
- **Evidence:** `TO_VALIDATE` — existing IP and implementation assets are not themselves proof of customer demand or WTP.
- **Moat hypothesis:** approval-platform IP, reusable approval semantics, auditability, and cross-product integration. Market defensibility remains `TO_VALIDATE`.

Next commercial exit: validate that the approval problem is independently budget-worthy, not only useful as a bundled feature of Link/Control.

## DataRelay Atlas — Commercial Track

**Owning product authority:** this repository and its canonical Atlas roadmap.

**External commercial posture:** Problem = `HYPOTHESIS`; External Product/User = `UNVALIDATED`; Commercial = `WTP_UNKNOWN`.

Atlas remains **internal/dogfood-first**. Internal usage can validate product utility and operating leverage, but it must not be counted as external buyer/WTP evidence.

- **Problem hypothesis:** AI-assisted engineering teams repeatedly reconstruct context and struggle to correlate canonical repository state, lifecycle evidence, provenance, and cross-project knowledge.
- **Buyer hypothesis:** engineering/platform leadership and teams operating governed AI-assisted software delivery.
- **Urgency hypothesis:** increasing AI-agent/tool usage raises context-reconstruction cost and trust/audit requirements.
- **Existing Alternative:** GitHub search, issue/project tooling, docs/wiki/RAG, manual handoff notes, AI-client memory, and bespoke engineering dashboards.
- **WTP:** `UNKNOWN / TO_VALIDATE`.
- **Distribution hypothesis:** first prove sustained internal portfolio value; external discovery is a separate later decision.
- **Evidence:** internal dogfood/engineering utilization is allowed as **product-utility evidence only**; external customer confirmation and paid evidence are currently `TO_VALIDATE`.
- **Moat hypothesis:** Engineering System integration, provenance/trust boundaries, lifecycle evidence federation, and verified continuous engineering memory. External commercial defensibility remains `TO_VALIDATE`.

Next commercial exit: do not pursue paid external validation until internal recurring use demonstrates a stable, explainable value proposition worth testing with external engineering teams.

## Portfolio commercial review rule

When choosing between otherwise valid roadmap items, Atlas may surface the strongest available commercial evidence, but it must preserve the distinction between:

- **engineering readiness** — can we safely/reliably build and release it?
- **product validation** — does the workflow solve the intended user problem?
- **commercial validation** — will a real buyer commit money/resources for it?

The desired portfolio behavior is to invest more heavily only as evidence advances, while retaining a bounded exploration budget for new hypotheses.

For Link, Control, and Grant, Atlas must treat these entries as projections and reconcile them against the owning repository before using them for prioritization. A missing or stale owning-repository commercial contract remains `UNKNOWN`, not implied approval.

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
- Engineering System remains authoritative for context epoch/compiler/canary/economics behavior; Atlas must not implement a competing generic context compiler.
- Future provider-native compaction, tool-search, skill-loading, or third-party compression experiments are adapters/optimizations beneath that contract.
- Atlas may correlate those experiments with attributable context telemetry and verified outcomes across runs/projects.
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
