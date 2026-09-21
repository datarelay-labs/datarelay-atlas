# ADR-0005: Phase 1 Persistence Model

Status: Accepted
Date: 2026-09-21

## Context

Phase 1 requires Atlas-owned durable state for project registration and
canonical source configuration, plus rebuildable derived projections.
Architecture already states that durable storage schemas require an ADR before
persistent state is introduced. ADR-0003 froze provenance semantics; this ADR
selects the minimal Phase 1 persistence model for the current self-hosted,
single-organization product.

Athena used Postgres/pgvector/Bun/Wiki.js. Those choices are historical
migration evidence only (ADR-0004) and must not be inherited without product
justification.

## Minimal design gate

1. **Goal** — Persist one project's registry/configuration and rebuildable
   projections with clear backup/migration implications and no secret storage.
2. **Non-goals** — Multi-tenant SaaS storage; production HA; semantic/vector
   indexes; backup/restore automation beyond documenting operator implications;
   Human UI; replacing GitHub as canonical state.
3. **Affected public contract** — Atlas-owned registry layout, projection store
   layout, and operator data-root configuration. Provenance field semantics
   remain ADR-0003.
4. **State / migration impact** — Introduces versioned local durable files under
   a configurable data root. Schema version is explicit so later migrations can
   fail closed.
5. **Security / operations impact** — Credentials stay outside Git and outside
   registry/projection files (`GITHUB_TOKEN` / runtime env only). Backup of the
   data root copies configuration and derived projections, never tokens.
6. **Architecture boundary** — Canonical GitHub artifacts remain authoritative.
   Registry/config is Atlas-owned durable state. Projections are derived and
   rebuildable from registry + authenticated fetch.
7. **Acceptance / regression criteria** — Registry register/show/list and source
   add/list work; sync/rebuild retain immutable revision provenance;
   deterministic unchanged re-sync; explicit fetch failure; no Athena runtime
   dependency; operator docs describe backup/migration implications.

## Decision

1. **Local filesystem JSON store** is the Phase 1 durable model.
   - Default data root: `.atlas-data/` (gitignored), overridable by
     `ATLAS_DATA_ROOT` or CLI `--data-root`.
   - Registry file: `<data-root>/registry.json` with `schema_version`.
   - Projection store: `<data-root>/projections/` (rebuildable derived files +
     `projections.json` metadata).
2. **Separate state classes**:
   - Canonical: GitHub repository artifacts selected by source configuration.
   - Atlas-owned durable: project identity, repository mapping, refs, Engineering
     System metadata path, source configuration, enabled flags.
   - Derived/rebuildable: projected documents and sync metadata.
3. **Do not introduce Postgres/pgvector/Bun/Wiki.js** for Phase 1.
4. **Secrets** never enter registry, projections, provenance, or Git.
5. **Backup implication**: copy the data root directory. Restoring registry
   without re-sync restores configuration; projections may be stale until
   rebuild/sync.
6. **Migration implication**: bump `schema_version` with an explicit reader
   that rejects unsupported versions fail-closed until a migration exists.
7. **Projector identity** for Phase 1 rebuild keys is
   `atlas.projection/v1` (name + version embedded in projection metadata).

## Consequences

- Phase 1 can demonstrate one-project register → sync → rebuild without
  infrastructure-heavy dependencies.
- Operators can back up/move a single directory.
- Later production persistence (if needed) must ADR a migration from this local
  schema rather than silently forking contracts.
- Athena storage choices remain non-normative.
