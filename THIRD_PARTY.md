# Third-Party Components

This file records third-party components considered for DataRelay Atlas. It is not a complete SBOM or final legal determination.

## Athena (historical migration evidence; not an Atlas runtime dependency)

Per ADR-0004, Atlas does **not** vendor the Athena source tree and does **not**
require `datarelay-labs/athena` at build/test/deploy/runtime.

- Upstream: `jannismilz/athena`
- Data Relay fork (migration input / retirement candidate): `datarelay-labs/athena`
- Pinned upstream snapshot: `6720b948744d42f1332f86f9a8157ff588e40d6e`
- Preserved PoC snapshot: `f38e20ec4d4ea22c71f1457d9a5361da1e92773a` (`atlas-poc-20260921`)
- Generic hardening evidence: `5b90969a7e0fecadae587144a0bd7f444352799f` / `datarelay-labs/athena#1` (closed unmerged, superseded)
- License declared by upstream: **Apache License 2.0**
- Upstream `NOTICE` file at the recorded snapshot: not present
- Athena-origin files copied into Atlas in this absorption: **none** (`THIRD-PARTY-EMBEDDED` empty)
- Required behaviors were reimplemented as Atlas-native code under `atlas/`

Apache-2.0 obligations remain relevant for any future copy/derivation of Athena-origin
files and for redistribution of Athena binaries/images if that ever occurs. The future
Data Relay Source Available license for Atlas-owned code must not be presented as
replacing or restricting rights granted by third-party component licenses.

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

Top-level Athena license: reviewed (historical).
Transitive npm/Bun dependency license inventory for pinned PoC: **complete** (see audit evidence).
Runtime image / redistribution compatibility: **review-required** if those images are ever redistributed by Atlas (Wiki.js AGPL-3.0; Bun LGPL-linked components). Wiki.js is **not** an Atlas product dependency under ADR-0004.
Final Atlas distribution/package boundary: pending persistence/deploy ADR work; must not reintroduce an Athena-repo dependency.
