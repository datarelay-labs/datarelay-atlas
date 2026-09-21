# Athena Capability Inventory (ADR-0004)

Machine-readable record: [`athena-capability-inventory.json`](athena-capability-inventory.json)

## Source refs (frozen)

| Role | Commit |
|---|---|
| Upstream snapshot | `6720b948744d42f1332f86f9a8157ff588e40d6e` |
| PoC snapshot | `f38e20ec4d4ea22c71f1457d9a5361da1e92773a` |
| Generic hardening evidence | `5b90969a7e0fecadae587144a0bd7f444352799f` |
| Athena PR #1 | https://github.com/datarelay-labs/athena/pull/1 (CLOSED unmerged, superseded) |

## Disposition vocabulary

- `ATLAS-NATIVE` — required Atlas behavior reimplemented/owned in Atlas
- `THIRD-PARTY-EMBEDDED` — Athena-origin code copied/derived into Atlas (none in this absorption)
- `EXTERNAL-DEPENDENCY` — general package/image dependency not requiring the Athena repo
- `DROP` — not required by Atlas
- `HISTORICAL-EVIDENCE` — retained only via pinned refs / migration docs

## Capability audit (required areas)

| Capability | Disposition | Atlas target |
|---|---|---|
| MCP transport/auth/tool framework | ATLAS-NATIVE (tool semantics + scope gating); transport deferred | `atlas/mcp_context.py`, `atlas/security.py` |
| Wiki.js coupling | DROP | — |
| PostgreSQL/pgvector | EXTERNAL-DEPENDENCY (interface only now) | `atlas/retrieval.py` SemanticProvider |
| Embedding provider/client | EXTERNAL-DEPENDENCY | `atlas/retrieval.py` |
| Keyword + semantic fusion | ATLAS-NATIVE | `atlas/retrieval.py`, `tests/test_retrieval.py` |
| Indexing/chunking | ATLAS-NATIVE (deterministic projection index) | `atlas/projection.py`, `atlas/retrieval.py` |
| GitHub canonical sync + provenance | ATLAS-NATIVE | `atlas/github_sync.py`, `atlas/provenance.py`, `atlas/projection.py` |
| Project namespace/scoping | ATLAS-NATIVE | ADR-0003 + retrieval/MCP enforcement |
| Dashboard/metrics | DROP | — |
| Backup/restore | DROP (deferred to persistence/Phase 5) | — |
| Docker/Compose packaging | DROP (Athena packaging) | — |
| Auth/rate limiting/security hardening | ATLAS-NATIVE (path + scope regressions) | `atlas/security.py` |
| Tests/CI/supply-chain hardening | HISTORICAL-EVIDENCE + Atlas tests.yaml/CI | `.engineering/tests.yaml` |

## Copy policy

No Athena source files were copied into Atlas (`third_party_embedded_files: []`).
Required behaviors were reimplemented as Atlas-native Python modules. PoC tests informed regression coverage.

## PoC file counts

See JSON `counts.by_disposition` for the durable per-file classification of the PoC tree.
