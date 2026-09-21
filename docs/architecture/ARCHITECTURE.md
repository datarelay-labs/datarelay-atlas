# DataRelay Atlas — Initial Architecture

## Architecture boundary

DataRelay Atlas is the product. The Engineering System is the canonical methodology. Athena is an optional/replaceable knowledge-engine integration.

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
Owns project identity, repository mappings, Engineering System baseline/adoption state, and configured knowledge sources.

### Methodology Integration
Reads the canonical Engineering System contract and project-local `.engineering/*` metadata. It does not redefine the standard.

### Source Synchronization
Fetches approved canonical sources through authenticated provider integrations. GitHub is the first provider. The durable field contract is defined by ADR-0003 and `docs/contracts/source-provider-provenance.md`.

### Knowledge Projection
Creates rebuildable project-scoped representations of canonical content with repository/ref/path/revision provenance. Projection identity is an Atlas rebuild key, not a Wiki.js/Athena page id.

### Retrieval
Provides exact/keyword and semantic retrieval. Retrieval implementation may initially be supplied by Athena/Wiki.js/PostgreSQL/pgvector.

### Derived Synthesis
Future bounded layer for concepts, entities, cross-project links, contradiction detection, and knowledge-gap detection. Synthesized content is always marked derived and attributable.

### Lifecycle State
Normalizes observable GitHub/CI/test/release evidence into project status. It must distinguish observed fact from inferred/unknown state.

### AI Context / MCP
Exposes scoped retrieval and project state to Cursor, ChatGPT, and other agents over authenticated MCP.

### Human UI
Shows project inventory, lifecycle state, source provenance, knowledge coverage, gaps, and relationships.

## Athena relationship

The existing `xdr-labs/athena` work is treated as PoC evidence and a candidate runtime integration.

Atlas should reuse Athena capabilities where they reduce work:
- Wiki.js human browsing
- PostgreSQL/pgvector
- hybrid retrieval/indexing
- HTTPS MCP infrastructure
- dashboard/backup patterns

Atlas-specific behavior should move to the Atlas product boundary:
- Engineering System integration
- project registry
- GitHub/OpenSpec source contracts
- canonical/derived separation
- provenance rules
- lifecycle state
- knowledge compiler/synthesis
- product deployment and upgrade contract

No public Atlas contract should require Athena-specific concepts unless explicitly accepted.

## Security baseline

- authenticated provider access
- least-privilege source tokens
- HTTPS for remote MCP/UI
- secrets outside Git
- project/source authorization boundaries before multi-user expansion
- no AI-generated provenance claims; provenance derives from authenticated source metadata

## Persistence

Source/provider/provenance **semantics** are frozen by ADR-0003 for Phase 1 design. Durable storage schemas, migrations, backup, restore, upgrade, and rollback behavior still require an implementation ADR before Atlas-owned persistent state is introduced.

## Related decisions

- ADR-0001 — canonical state and Athena boundary
- ADR-0002 — Athena source preservation
- ADR-0003 — source/provider/provenance contracts
