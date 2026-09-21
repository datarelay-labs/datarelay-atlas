# Athena PoC Migration Classification

Source snapshot: `datarelay-labs/athena@atlas-poc-20260921`  
HEAD: `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`

The PoC is four commits ahead of upstream. This document is the initial ownership classification; implementation migration still requires affected-code review and tests.

## Commit inventory

### `4327edf` — harden MCP and project-scoped search

Touched core vector search, indexer endpoints/client, MCP auth/server/tools, tests and dependency metadata.

**Initial classification: SPLIT / REVIEW REQUIRED**

- Project-scoped retrieval and provenance-aware tool behavior are Atlas requirements.
- Generic auth correctness, vector/indexer fixes, and reusable MCP hardening may belong in the Athena fork or upstream.
- Do not wholesale copy this commit into Atlas. Separate product contract from generic engine changes first.

### `4e99fc7` — sync canonical GitHub knowledge into Athena

Added `knowledge-sources.json` and `scripts/sync-github-knowledge.*`.

**Initial classification: ATLAS-OWNED CONCEPT**

The canonical-source sync model, project namespace, repository/ref/path/blob provenance, and GitHub/OpenSpec authority model are Atlas product behavior.

The current code is tightly coupled to Athena/Wiki.js APIs and may be reused as migration input, but the durable source/provider contract belongs in Atlas.

### `5e84ba6` — pin actions and enforce dependency audit

Changed Athena CI only.

**Initial classification: GENERIC FORK / UPSTREAM CANDIDATE**

This is build/supply-chain hardening rather than Atlas product behavior. Keep it out of Atlas unless Atlas independently needs equivalent controls.

### `f38e20e` — document KB governance and clean auth lint

Added Engineering Knowledge governance documentation and removed an auth lint issue.

**Initial classification: SPLIT**

- Governance principles belong in Atlas canonical docs/architecture.
- Generic auth/lint cleanup belongs in the Athena fork/upstream if still applicable.

## Migration rules

1. Migrate **contracts before code**: define Atlas provider, project, source, provenance, retrieval, and lifecycle interfaces before extracting implementation.
2. Never make Wiki.js/Athena page identity a public Atlas contract unless explicitly accepted.
3. Preserve exact provenance fields used by the PoC where they remain useful: project, repository, ref, source path, source revision.
4. Reuse PoC tests as regression evidence when moving behavior, but adapt them to the Atlas-owned boundary.
5. Keep generic Athena fixes separately reviewable so upstream updates remain manageable.

## First extraction targets

1. source/provider configuration schema
2. project registry identity and namespace
3. canonical GitHub source fetch + immutable revision metadata
4. derived projection contract
5. retrieval result provenance contract
6. MCP-facing Atlas context contract

Actual runtime framework/language choices remain a design decision and should not be inferred from the PoC merely because Athena currently uses Bun/TypeScript.
