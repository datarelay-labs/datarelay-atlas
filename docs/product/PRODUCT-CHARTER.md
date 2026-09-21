# DataRelay Atlas — Product Charter

Status: Initial product definition
Product: DataRelay Atlas
Repository: `datarelay-labs/datarelay-atlas`

## Product statement

DataRelay Atlas is an AI-assisted Engineering Knowledge & Lifecycle Platform that connects methodology, project state, durable engineering knowledge, and AI context across software projects.

Its purpose is to let humans and AI agents answer four questions from current, attributable evidence:

1. What projects and engineering assets exist?
2. What is the current lifecycle and delivery state?
3. What is canonical, and why was it decided?
4. What context does an AI agent need to work correctly without replaying old conversations?

## Goals

- Apply and expose the canonical Data Relay Labs Engineering System across registered projects.
- Register projects and map repositories, specifications, ADRs, tests, releases, runbooks, and evidence.
- Ingest canonical engineering knowledge into a searchable derived layer with source provenance.
- Provide project-scoped and cross-project retrieval for Cursor, ChatGPT, and other MCP-capable AI clients.
- Surface lifecycle state, knowledge gaps, contradictions, and stale/unknown context without inventing missing rationale.
- Keep the system useful for a solo AI-assisted engineer while leaving a path to broader product use.

## Non-goals

Atlas is not initially:
- a Git hosting service
- a CI/CD engine
- an issue tracker or Jira replacement
- a coding agent or IDE replacement
- a general-purpose chat UI
- a generic enterprise search platform
- a Confluence/Notion replacement
- a multi-tenant SaaS platform

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
- a solo or small engineering owner using ChatGPT/Cursor/GitHub across multiple projects

Future:
- engineering teams that want self-hosted engineering knowledge/lifecycle context for humans and AI agents

## Initial deployment model

- self-hosted
- single organization
- GitHub as the first repository provider
- authenticated source synchronization
- web management/navigation UI
- HTTPS MCP access
- Atlas-owned knowledge projection/retrieval (Athena repository is not a runtime dependency)

## Product success for MVP

MVP is successful when a new/existing project can be registered and Atlas can:
- identify its Engineering System adoption state
- map canonical engineering sources
- synchronize attributable knowledge
- search exact and semantic context
- answer project-scoped questions with provenance
- expose current lifecycle/validation/release state where GitHub evidence exists
- provide the same trusted context to human UI and MCP clients

## Scope discipline

New capabilities must demonstrate direct value to engineering context, lifecycle visibility, knowledge quality, or AI execution correctness. Avoid expanding into already-solved infrastructure categories without an explicit product decision.
