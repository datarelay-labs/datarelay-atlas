# DataRelay Atlas — Product Charter

Status: Canonical product definition
Product: DataRelay Atlas
Last realigned: 2026-10-01

## North star

DataRelay Atlas is the **trusted engineering context and lifecycle control plane** that gives humans and AI clients the same current, attributable view of engineering state.

Atlas Core organizes and validates engineering knowledge and lifecycle evidence. An optional Automation Extension may use that trusted state to coordinate AI-assisted engineering work, but automation is not required for Atlas Core to be a complete product.

## Product statement

DataRelay Atlas is an AI-assisted Engineering Knowledge & Lifecycle Platform.

It connects methodology, project state, durable engineering knowledge, lifecycle evidence, and AI context across software projects so humans and AI clients can answer four questions from current evidence:

1. What projects and engineering assets exist?
2. What is the current lifecycle and delivery state?
3. What is canonical, attributable, stale, contradictory, or unknown?
4. What bounded context does an AI client need to work correctly without replaying old conversations?

## Product layers

### Atlas Core — required product

Atlas Core consists of four product planes.

#### 1. Knowledge Plane

- project/repository registry
- canonical source mapping and authenticated synchronization
- rebuildable projections with provenance
- exact, semantic, project-scoped, and explicitly scoped cross-project retrieval
- core derived intelligence: cross-project links, decision backlinks, contradiction detection, stale/unknown detection, and knowledge-gap detection

#### 2. Lifecycle Plane

- Engineering System adoption/compliance visibility
- current Work Packet / PR / CI / test / release evidence
- observed vs inferred vs unknown state separation
- exact-candidate validation/release evidence
- human-equivalent user-surface gate visibility where a project requires it

#### 3. AI Context & Trust Plane

- authenticated HTTPS MCP retrieval
- scoped, attributable context packages
- canonical-vs-derived separation
- exact revision / provenance identity
- durable resume and bounded audit evidence where Atlas participates in an engineering workflow
- provider-neutral trust and instruction-governance contracts

#### 4. Human Interface & Operations

- human browsing/navigation of the same trusted state exposed to MCP clients
- production health, backup/restore, upgrade/rollback, observability, and security evidence
- release qualification for user-facing Atlas surfaces

### Automation Extension — optional product extension

The Automation Extension may coordinate work using Atlas Core state:

- dependency/readiness graph and execution admission
- durable Work Packet handoff and independent verification
- bounded parallelism / concurrency control
- provider capability and capacity evidence
- policy-bounded route selection and failover
- bounded decision-model assistance

Automation must consume Atlas Core trust boundaries; it must not redefine canonical project truth. Failure or absence of an optional automation capability must not make Atlas Core incomplete unless a future explicit product decision promotes that capability into Core.

### Experimental / optional capabilities

Examples include:

- live Stagehand semantic browser comparison
- provider-cost/quota optimization strategies beyond measured evidence
- Decision Plane expansion beyond proven bounded classes
- Personal Knowledge / Second Brain source expansion
- context-compression experiments
- provider-specific research and historical Cursor compatibility/evidence
- unrelated research labs built on Atlas knowledge primitives

Experimental work requires its own success criteria and must not silently become a Core release blocker.

## Search vs Ask

Atlas owns **retrieval, provenance, scope, integrity, and bounded context packaging**.

Atlas does not require a general-purpose chat/answer-generation surface for Core completion. A ChatGPT or other approved MCP client may synthesize an answer from Atlas context. Any future Atlas-native answer synthesis must be introduced as an explicit bounded contract with provenance and failure semantics; it must not blur canonical and derived state.

## Human UI boundary

The current Atlas Human UI is primarily a **read-mostly/read-only observability and navigation surface**.

Canonical project/source/policy mutation remains in explicit operator interfaces such as GitHub and Atlas CLI unless a separate management-write contract is approved. A richer web management UI is therefore not implicitly required for Core completion.

## Goals

- Apply and expose the canonical Data Relay Labs Engineering System across registered projects.
- Register projects and map repositories, specifications, ADRs, tests, releases, runbooks, and evidence.
- Ingest canonical engineering knowledge into a searchable derived layer with source provenance.
- Provide project-scoped and cross-project retrieval for ChatGPT and other approved MCP-capable AI clients.
- Surface lifecycle state, knowledge gaps, contradictions, stale context, and unknowns without inventing missing rationale.
- Give the Human UI and MCP clients the same trusted engineering context.
- Keep Atlas useful for a solo AI-assisted engineer while preserving a path to team and broader product use.

## Non-goals

Atlas Core is not:

- a Git hosting service
- a CI/CD engine
- an issue tracker or Jira replacement
- a coding agent or IDE replacement
- a general-purpose chat UI
- a generic enterprise search platform
- a Confluence/Notion replacement
- a multi-tenant SaaS platform
- an autonomous coding-agent replacement
- a provider-quota optimizer that must always choose the cheapest route

## Canonical authority

The authority model follows the Engineering System:

1. executable code/config/schema for runtime truth
2. accepted product/specification artifacts for intended behavior
3. repository engineering metadata
4. ADRs for durable architecture/security/persistence/public-contract decisions
5. runbooks/RCA for operations/incidents
6. Atlas-derived knowledge for search, navigation, synthesis, and AI retrieval

Derived knowledge must never silently override canonical Git/GitHub state.

## Initial users

Primary:

- a solo or small engineering owner using ChatGPT, GitHub, and approved engineering tools across multiple projects

Future:

- engineering teams that want self-hosted engineering knowledge/lifecycle context for humans and AI clients

## Initial deployment model

- self-hosted
- single organization
- GitHub as the first repository provider
- authenticated source synchronization
- read-mostly human navigation/observability UI
- HTTPS MCP access
- Atlas-owned knowledge projection/retrieval
- Athena repository is not a runtime dependency

## Core product completion

Atlas Core is complete when one representative project journey can be demonstrated end to end on an exact candidate:

1. register a project and identify its Engineering System adoption state;
2. map and synchronize canonical engineering sources with attributable revisions;
3. retrieve exact and semantic project context and explicitly scoped cross-project context with provenance;
4. show the same trusted context through the Human UI and an approved MCP client;
5. show current lifecycle/validation/release state while distinguishing observed, stale, contradictory, and unknown evidence;
6. surface the minimum Core derived intelligence needed to navigate decisions, contradictions, and knowledge gaps without becoming a competing source of truth;
7. survive the production operating journey: deploy, health check, backup/restore test, upgrade/rollback, and restart while preserving/rebuilding the intended state;
8. for user-facing releases, pass required exact-candidate machine gates plus actual-browser Surface Reconciliation and Full User E2E under the active Engineering System release standard.

A checklist of optional Automation Extension or experimental features is not a substitute for this end-to-end Core acceptance journey.

## Scope discipline

Every new capability must be classified before implementation:

- **CORE** — required to satisfy the Core completion journey.
- **AUTOMATION_EXTENSION** — coordinates work using Core state but is not a Core release blocker.
- **EXPERIMENTAL_OPTIONAL** — requires measured evidence before promotion.

New work must demonstrate direct value to engineering context, lifecycle visibility, knowledge quality, trust, or a separately approved automation outcome. Avoid expanding into already-solved infrastructure categories or provider-specific complexity without an explicit product decision.
