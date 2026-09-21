# ADR-0002: Athena Source Preservation and Fork Strategy

Status: Accepted
Date: 2026-09-21

## Context

DataRelay Atlas needs a stable implementation base while preserving the ability to evolve beyond Athena. A validated Engineering Knowledge PoC already exists on `xdr-labs/athena@feature/rick-kb-hardening`, but Atlas now has its own product repository and Data Relay Labs organization boundary.

Copying the Athena tree into the Atlas repository would make upstream synchronization and ownership classification unnecessarily difficult.

## Decision

1. Maintain `datarelay-labs/athena` as the Data Relay upstream-tracking fork of `jannismilz/athena`.
2. Preserve the current upstream snapshot at tag `atlas-upstream-20260921`, commit `6720b948744d42f1332f86f9a8157ff588e40d6e`.
3. Preserve the validated legacy PoC at branch `poc/engineering-kb-20260921` and tag `atlas-poc-20260921`, commit `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`.
4. Do not vendor/copy the entire Athena source tree into `datarelay-atlas`.
5. Keep fork `main` suitable for upstream tracking. Atlas-only behavior belongs in `datarelay-atlas`; generic Athena fixes may remain on explicit fork branches and be proposed upstream.
6. Pin exact Athena revisions in Atlas integration/deployment metadata.
7. Keep Apache-2.0 third-party licensing distinct from the future Atlas-owned source-available license.

## Consequences

- Upstream Athena can be synchronized without mixing product history into Atlas.
- The proven PoC cannot be lost while its useful behavior is extracted.
- Atlas can replace Athena later without changing product identity.
- Migration work must explicitly classify PoC changes instead of blindly merging the PoC branch to fork `main`.
- Production packaging must retain applicable third-party license obligations.
