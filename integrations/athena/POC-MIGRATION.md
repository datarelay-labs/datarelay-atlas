# Athena PoC Migration Classification

Source snapshot: `datarelay-labs/athena@atlas-poc-20260921`
HEAD: `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`
Upstream baseline: `6720b948744d42f1332f86f9a8157ff588e40d6e`
Delta: four commits ahead of upstream

Disposition vocabulary:

| Code | Meaning |
|---|---|
| `ATLAS-OWNED` | Product contract/behavior belongs in `datarelay-atlas` |
| `ATHENA-FORK-OWNED` | Generic Athena improvement retained on a focused fork branch |
| `UPSTREAM-CANDIDATE` | Suitable to propose to `jannismilz/athena` after fork validation |
| `DROP` | Do not carry forward into Atlas or fork `main` |
| `SPLIT` | Exact boundary documented below |

Rules honored:

- do not wholesale merge the PoC branch into Athena fork `main`
- Atlas product contracts must not depend on Wiki.js/Athena page identity
- generic Athena fixes remain separately reviewable from Atlas product behavior
- historical PoC evidence remains immutable

## Commit inventory (summary)

### `4327edf` — harden MCP and project-scoped search

**SPLIT** — generic auth/search hardening vs Atlas project-scope product need.

### `4e99fc7` — sync canonical GitHub knowledge into Athena

**ATLAS-OWNED** concept and migration input; do not land on fork `main`.

### `5e84ba6` — pin actions and enforce dependency audit

**ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE**.

### `f38e20e` — document KB governance and clean auth lint

**SPLIT** — governance principles → Atlas; unused-var cleanup → fork.

## Per-file / per-capability disposition

| Path | Capability | Disposition | Boundary / action |
|---|---|---|---|
| `knowledge-sources.json` | Canonical source list with Wiki path mapping | **SPLIT** | Atlas owns source/provider config semantics (`project`, `repository`, `ref`, `source_path`, revision). `wiki_path` is PoC projection detail → **DROP** from Atlas public contract. |
| `scripts/sync-github-knowledge.ts` | GitHub fetch + Wiki page upsert + reindex | **SPLIT** | GitHub fetch + provenance rendering concepts → **ATLAS-OWNED**. Wiki.js client upsert/reindex path → migration input only; not an Atlas public API. |
| `scripts/sync-github-knowledge.test.ts` | Sync validation | **ATLAS-OWNED** | Reuse as regression evidence when Atlas sync is implemented; adapt away from Wiki identity. |
| `docs/RICK_ENGINEERING_KB.md` | Authority model / namespaces / hardening notes | **SPLIT** | Canonical-vs-derived, project namespace, provenance expectations → **ATLAS-OWNED** docs/ADR. Athena-specific operator runbook content → **DROP** from Atlas contracts. |
| `packages/core/src/vectors.ts` | Optional `pathPrefix` filter for vector search | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Generic engine capability. Atlas may consume via integration; must not hard-code Wiki path layout in Atlas contracts. |
| `packages/indexer/src/indexer.ts` | Pass-through path prefix | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Same as vectors. |
| `packages/indexer/src/server.ts` | HTTP search accepts path prefix | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Same as vectors. |
| `packages/mcp/src/indexer-client.ts` | Client forwards path prefix | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Same as vectors. |
| `packages/mcp/src/tools.ts` | `path_prefix` search arg + write-tool scope gating | **SPLIT** | Write-scope gating + optional path filter APIs → **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE**. Atlas retrieval contract uses project/namespace scope, not Wiki path as public identity. |
| `packages/mcp/src/tools.test.ts` | Regression for scoped search / tool registration | **ATHENA-FORK-OWNED** | Keep with generic fork patch; reuse ideas for Atlas MCP tests later. |
| `packages/mcp/src/auth/provider.ts` | `wiki.read` / `wiki.write` scopes | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Generic auth hardening. Atlas authz model remains Atlas-owned and must not require Wiki scope names publicly. |
| `packages/mcp/src/auth/provider.test.ts` | Scope defaults tests | **ATHENA-FORK-OWNED** | Keep with generic fork patch. |
| `packages/mcp/src/auth/routes.ts` | Remove unused pending lookup after failed login | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Lint/correctness cleanup only. |
| `packages/mcp/src/server.ts` | Wire `currentScopes` into tool context | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Supports write gating. |
| `.github/workflows/ci.yml` | Pin Actions SHAs + `bun audit` | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Supply-chain hygiene; not Atlas product behavior. |
| `package.json` | Dependency overrides (`fast-uri`, `hono`, `qs`) | **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** | Security overrides accompany generic hardening. |
| `bun.lock` | Lockfile for overrides/deps | **ATHENA-FORK-OWNED** | Travel with the generic fork patch only. |

## Extracted generic Athena fork branch

Generic (non-Atlas-product) changes are retained on:

- Repository: `datarelay-labs/athena`
- Branch: `fix/generic-mcp-search-hardening`
- Based on: `atlas-upstream-20260921` / `6720b948744d42f1332f86f9a8157ff588e40d6e`
- Commit: `5b90969a7e0fecadae587144a0bd7f444352799f`
- PR: https://github.com/datarelay-labs/athena/pull/1
- Includes only the **ATHENA-FORK-OWNED** / **UPSTREAM-CANDIDATE** files above
- Excludes Atlas-owned sync config/scripts and PoC governance doc

Fork `main` remains upstream-tracking and is **not** fast-forwarded to the PoC branch.
The Athena PR is intentionally review/upstream-candidate continuity, not a requirement to merge before Atlas Phase 0 closure.

## Atlas extraction targets (contracts before code)

1. source/provider configuration schema → [`docs/contracts/source-provider-provenance.md`](../../docs/contracts/source-provider-provenance.md)
2. project registry identity and namespace
3. canonical GitHub source fetch + immutable revision metadata
4. derived projection contract
5. retrieval result provenance contract
6. MCP-facing Atlas context contract

Actual runtime framework/language choices remain a design decision and must not be inferred from the PoC merely because Athena currently uses Bun/TypeScript.

## Migration rules

1. Migrate **contracts before code**.
2. Never make Wiki.js/Athena page identity a public Atlas contract unless explicitly accepted.
3. Preserve provenance fields: project, repository, ref, source path, source revision/blob identity.
4. Reuse PoC tests as regression evidence when moving behavior, adapted to the Atlas-owned boundary.
5. Keep generic Athena fixes separately reviewable so upstream updates remain manageable.
