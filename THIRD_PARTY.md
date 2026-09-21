# Third-Party Components

This file records third-party components considered for DataRelay Atlas. It is not a complete SBOM or final legal determination.

## Athena

- Upstream: `jannismilz/athena`
- Data Relay fork: `datarelay-labs/athena`
- Pinned upstream snapshot: `6720b948744d42f1332f86f9a8157ff588e40d6e`
- Preserved PoC snapshot: `f38e20ec4d4ea22c71f1457d9a5361da1e92773a` (`atlas-poc-20260921`)
- License declared by upstream: **Apache License 2.0**
- Upstream `NOTICE` file at the recorded snapshot: not present
- Full Apache license text remains in the Athena source repository/fork.

Apache-2.0 permits modification and redistribution subject to its conditions. Before Atlas distributes Athena source/binaries as part of a release, the release process must verify required license/attribution handling and the licenses of Athena's transitive dependencies and runtime images.

The future Data Relay Source Available license for Atlas-owned code must not be presented as replacing or restricting rights granted by third-party component licenses.

### Dependency / license audit (pinned PoC)

Durable evidence: [`integrations/athena/DEPENDENCY-LICENSE-AUDIT.md`](integrations/athena/DEPENDENCY-LICENSE-AUDIT.md) and [`integrations/athena/evidence/athena-poc-npm-licenses.tsv`](integrations/athena/evidence/athena-poc-npm-licenses.tsv).

Summary of factual findings at PoC HEAD:

| Layer | Result |
|---|---|
| Athena top-level | Apache-2.0; NOTICE absent |
| npm/Bun lockfile (116 packages) | Declared licenses are MIT / Apache-2.0 / ISC / BSD / Unlicense only; 0 lookup failures; no GPL-family npm declarations |
| Wiki.js runtime image | Declared **AGPL-3.0** → **review-required** before redistribution |
| Bun runtime image | Bun MIT + LGPL-linked JSC/WebKit and mixed linked libs → **review-required** if Bun binaries are redistributed |
| pgvector/PostgreSQL image | PostgreSQL-style license text present |
| Hugging Face TEI image | Declared Apache-2.0 |

## Status

Top-level Athena license: reviewed.
Transitive npm/Bun dependency license inventory for pinned PoC: **complete** (see audit evidence).
Runtime image / redistribution compatibility: **review-required** (Wiki.js AGPL-3.0; Bun LGPL-linked components).
Final Atlas distribution/package boundary: pending (intentionally not invented in Phase 0).
