# ADR-0003: Source, Provider, and Provenance Contracts

Status: Accepted
Date: 2026-09-21

## Context

Phase 1 (Project Registry & Canonical Sync) cannot proceed safely without an
explicit, implementation-neutral contract for:

- project identity / namespace
- provider identity and authentication boundary
- canonical source configuration
- repository + ref + path selection
- immutable source revision / blob identity
- projection/rebuild identity
- provenance on every derived record and retrieval result
- canonical-vs-derived semantics
- deterministic re-sync/rebuild expectations
- failure/unknown semantics when canonical data cannot be fetched

The Athena Engineering Knowledge PoC demonstrated useful fields
(`project`, `repository`, `ref`, `source_path`, Git blob SHA) but also coupled
them to Wiki.js page paths. ADR-0001 already established that Athena/Wiki.js
must not become required public product concepts. ADR-0002 preserved Athena
source without vendoring it into Atlas.

This ADR freezes the minimum durable Atlas contracts before any Atlas-owned
persistent schema or Phase 1 runtime implementation.

## Minimal design gate

1. **Goal** — Define the minimum durable source/provider/provenance contracts that unblock Phase 1 sync/rebuild work without selecting a runtime framework.
2. **Non-goals** — Phase 1 runtime implementation; multi-tenant SaaS; replacing GitHub; making Wiki.js identity public; choosing Bun/TypeScript because the PoC used them.
3. **Affected public contract** — Atlas project/source configuration, derived-record provenance, retrieval provenance, and sync failure semantics.
4. **State / migration impact** — Future persistent-state-adjacent. This ADR accepts the contract semantics first; durable storage schemas must conform later and require their own migration/backup design when implemented.
5. **Security / operations impact** — Provider credentials remain outside Git and outside provenance payloads. Sync must fail closed to `unknown`/`error` rather than inventing canonical content.
6. **Architecture boundary** — Atlas owns the contracts. Knowledge-engine integrations (including Athena) are replaceable projectors/indexers that must preserve Atlas provenance fields.
7. **Acceptance / regression criteria** — Canonical contract document + machine-checkable schema fixture validate; roadmap Phase 0 item checked; architecture/THIRD_PARTY/migration docs remain consistent.

## Decision

1. Atlas adopts the implementation-neutral contracts in
   [`docs/contracts/source-provider-provenance.md`](../contracts/source-provider-provenance.md)
   and the schema fixture
   [`docs/contracts/source-provider-provenance.schema.json`](../contracts/source-provider-provenance.schema.json).
2. Every derived record and retrieval hit MUST carry provenance sufficient to return to the canonical GitHub (or future provider) object: at least `project_id`, `provider`, `repository`, `ref`, `source_path`, and immutable `source_revision` when available.
3. Canonical authority remains GitHub/repository artifacts per ADR-0001. Derived projections are rebuildable and never silently override canonical content.
4. Provider authentication is a runtime secret boundary, not part of stored provenance claims.
5. When canonical fetch fails or revision identity cannot be established, Atlas records explicit `error`/`unknown` sync state and MUST NOT present fabricated canonical content as current.
6. Projection identity is a derived rebuild key (project + source configuration + source revision + projector version/parameters), not a Wiki.js page id.
7. Athena/Wiki.js path identities may appear only as private integration mapping, never as the Atlas public provenance primary key unless a future ADR explicitly accepts that.

## Consequences

- Phase 1 can implement registry/sync against a stable contract.
- PoC sync code can be reused as migration input after removing Wiki-public identity.
- Future knowledge-engine replacements must map into the same provenance fields.
- Packaging/legal review of Athena/Wiki.js remains independent (see third-party audit); this ADR does not authorize redistribution choices.
