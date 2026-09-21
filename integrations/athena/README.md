# Athena Migration Evidence

`integrations/athena/` retains **historical migration evidence** for DataRelay Atlas.
It is not a runtime integration and must not be required to build, test, or run Atlas
(see ADR-0004).

## Frozen source revisions

| Role | Repository / ref |
|---|---|
| Upstream snapshot | `jannismilz/athena` / `datarelay-labs/athena@atlas-upstream-20260921` → `6720b948744d42f1332f86f9a8157ff588e40d6e` |
| Preserved PoC | `datarelay-labs/athena@atlas-poc-20260921` → `f38e20ec4d4ea22c71f1457d9a5361da1e92773a` |
| Generic hardening evidence | `5b90969a7e0fecadae587144a0bd7f444352799f` / PR https://github.com/datarelay-labs/athena/pull/1 (closed unmerged, superseded) |

Machine-readable lock: [`source-lock.yaml`](source-lock.yaml)

## Why Athena was not vendored

Copying the Athena tree would blur upstream history, generic hardening, and Atlas
product ownership. ADR-0004 requires Atlas-native reimplementation of needed
behavior and explicit DROP/EXTERNAL/HISTORICAL dispositions instead.

## Absorbed vs historical

- Absorbed Atlas-native code: `atlas/`
- Capability inventory: `docs/migration/athena-capability-inventory.md`
- Retirement checklist: `docs/migration/athena-retirement-checklist.md`
- Prior PoC classification (Phase 0): [`POC-MIGRATION.md`](POC-MIGRATION.md)
- License audit evidence: [`DEPENDENCY-LICENSE-AUDIT.md`](DEPENDENCY-LICENSE-AUDIT.md)

## Independence

`scripts/validate-athena-independence.py` must PASS. Atlas must not submodule,
subtree, clone, or fetch `datarelay-labs/athena` for product operation.
