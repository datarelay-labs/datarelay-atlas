# DataRelay Atlas — Initial Architecture

## Architecture boundary

DataRelay Atlas is the product. The Engineering System is the canonical methodology.
Atlas owns its runtime, contracts, and tests. `datarelay-labs/athena` is migration
input only and is not a build/test/deploy/runtime dependency (ADR-0004).

```text
                 DataRelay Atlas
                       |
      +----------------+----------------+
      |                |                |
 Methodology      Project State      Knowledge
 Engineering      GitHub / Specs     Projection
 System            PR / CI / Test     Search / Vector
      |                |                |
      +----------------+----------------+
                       |
                 AI Context Layer
              Cursor / ChatGPT / MCP
```

## Canonical vs derived

```text
GitHub / OpenSpec / Code / Tests / ADR / CI
                  CANONICAL
                      |
                authenticated sync
                      v
              Atlas source projection
          repo / ref / path / revision
                      |
             index + optional synthesis
                      v
         searchable DERIVED knowledge
                      |
              UI + HTTPS MCP clients
```

Every derived record must retain enough provenance to return to the canonical source. If canonical and derived content disagree, canonical content wins.

## Major logical components

### Project Registry
Owns project identity, repository mappings, Engineering System baseline/adoption state, and configured knowledge sources. Phase 1 implementation: `atlas/registry.py` + `python -m atlas` operator commands (`project` / `source` / `sync` / `rebuild` / `adoption`).

### Methodology Integration
Reads the canonical Engineering System contract and project-local `.engineering/*` metadata. It does not redefine the standard.

### Source Synchronization
Fetches approved canonical sources through authenticated provider integrations. GitHub is the first provider. The durable field contract is defined by ADR-0003 and `docs/contracts/source-provider-provenance.md`. Atlas-owned implementation lives under `atlas/`.

### Knowledge Projection
Creates rebuildable project-scoped representations of canonical content with repository/ref/path/revision provenance. Projection identity is an Atlas rebuild key, never a Wiki.js/Athena page id.

### Retrieval
Provides exact/keyword retrieval and hybrid fusion with a pluggable semantic provider. Semantic backends may be EXTERNAL-DEPENDENCY services (for example pgvector/TEI) consumed directly by Atlas—not via the Athena repository.

### Derived Synthesis
Future bounded layer for concepts, entities, cross-project links, contradiction detection, and knowledge-gap detection. Synthesized content is always marked derived and attributable.

### Lifecycle State
Normalizes observable GitHub/CI/test/release evidence into project status. It must distinguish observed fact from inferred/unknown state.

### AI Context / MCP
Exposes scoped retrieval and project state to Cursor, ChatGPT, and other agents. Tool semantics are Atlas-owned (`atlas/mcp_context.py`); HTTPS/OAuth transport remains a later Atlas packaging concern.

### Human UI
Shows project inventory, lifecycle state, source provenance, knowledge coverage, gaps, and relationships.

## Athena relationship (historical)

Pinned Athena revisions remain **historical migration evidence** only:

- upstream `6720b948744d42f1332f86f9a8157ff588e40d6e`
- PoC `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`
- generic-hardening evidence `5b90969a7e0fecadae587144a0bd7f444352799f`

Inventory and retirement checklist:

- `docs/migration/athena-capability-inventory.md`
- `docs/migration/athena-retirement-checklist.md`

Wiki.js is not an Atlas public product concept. Required PoC behaviors were reimplemented as Atlas-native modules rather than vendoring the Athena tree.

## Security baseline

- authenticated provider access
- least-privilege source tokens
- HTTPS for remote MCP/UI (when exposed)
- secrets outside Git
- project/source authorization boundaries before multi-user expansion
- no AI-generated provenance claims; provenance derives from authenticated source metadata
- path-traversal rejection and scope gating for retrieval/context tools

## Persistence

Source/provider/provenance **semantics** are frozen by ADR-0003. Phase 1 durable state is defined by ADR-0005:

- Atlas-owned registry/configuration: local `registry.json` under a configurable data root (default `.atlas-data/`)
- Derived projections: rebuildable files under `<data-root>/projections/`
- Secrets remain outside Git and outside registry/projection payloads
- Backup implication: copy the data root; rebuild/sync refreshes derived content

Postgres/pgvector/Bun/Wiki.js are not Phase 1 persistence choices.

## Related decisions

- ADR-0001 — canonical state and product boundary
- ADR-0002 — historical Athena fork/pin evidence (superseded long-term strategy)
- ADR-0003 — source/provider/provenance contracts
- ADR-0004 — Athena absorption and repository retirement
- ADR-0005 — Phase 1 persistence model (local filesystem JSON)
