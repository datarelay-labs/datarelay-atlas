# Athena Integration

Athena is currently the preferred **knowledge-engine implementation input** for DataRelay Atlas. It is not the DataRelay Atlas product identity and it is not a canonical source of engineering truth.

## Source ownership

| Role | Repository / ref |
|---|---|
| Upstream | `jannismilz/athena@6720b948744d42f1332f86f9a8157ff588e40d6e` |
| Data Relay upstream-tracking fork | `datarelay-labs/athena@main` |
| Upstream snapshot tag | `datarelay-labs/athena@atlas-upstream-20260921` |
| Preserved Engineering Knowledge PoC | `datarelay-labs/athena@poc/engineering-kb-20260921` |
| PoC immutable snapshot | `datarelay-labs/athena@atlas-poc-20260921` |
| PoC HEAD | `f38e20ec4d4ea22c71f1457d9a5361da1e92773a` |

The machine-readable record is [`source-lock.yaml`](source-lock.yaml).

## Why the Athena tree is not copied into this repository

Copying the whole upstream source into `datarelay-atlas` would blur three boundaries:

1. upstream Athena history and upgrades
2. generic Athena hardening
3. DataRelay Atlas product behavior

Instead, Atlas records immutable source revisions and keeps Athena in a separate fork. Atlas-owned functionality is extracted into this repository as product implementation appears.

## Fork policy

- `datarelay-labs/athena/main` should remain an upstream-tracking branch.
- Do not put Atlas-only product contracts on fork `main`.
- Generic fixes that improve Athena independently may live on bounded fork branches and should be upstreamed when practical.
- Atlas-specific project registry, Engineering System integration, canonical-source contracts, provenance rules, lifecycle state, and derived engineering intelligence belong in `datarelay-atlas`.
- Deployment must pin an exact Athena source revision rather than consume a floating `main`.

## Preserved PoC

The validated PoC branch was copied from the legacy `xdr-labs/athena@feature/rick-kb-hardening` branch without rewriting history.

It is preserved because it already contains working evidence for:

- project-scoped MCP/search hardening
- GitHub canonical-source synchronization
- repository/ref/path/Git-blob provenance
- dependency/CI hardening
- Engineering Knowledge governance

The preserved branch is an implementation input, **not** the final Atlas architecture.

## Migration status

Per-file ownership for the four PoC commits is recorded in [`POC-MIGRATION.md`](POC-MIGRATION.md).

- Atlas-owned contracts: ADR-0003 + `docs/contracts/source-provider-provenance.md`
- Generic Athena hardening: retained on `datarelay-labs/athena` branch `fix/generic-mcp-search-hardening` (not merged into fork `main`)
- Dependency/license evidence: [`DEPENDENCY-LICENSE-AUDIT.md`](DEPENDENCY-LICENSE-AUDIT.md)

## Next migration boundary

Implement Phase 1 registry/sync against the accepted contracts. Reuse PoC sync/tests as migration input only after removing Wiki.js public identity.
