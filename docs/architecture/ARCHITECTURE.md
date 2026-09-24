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

The Operational Phase 2 operator slice (`python -m atlas search <project_id> <query>`) rebuilds a project-scoped keyword `Retriever` in memory from ProjectionStore documents whose sync state is `success`, `unchanged`, or `ok`. Hit provenance is copied from that projection metadata. Distinct projections that share a `source_path` stay independently searchable when their `source_id` and `ref` differ. No separate search index is stored. Failed, disabled, and other non-successful projections are excluded. Missing projection bytes, unsafe projection paths, a mismatch against the stored `content_digest`, disagreement between record and provenance identity, a rendered-projection identity that does not match that provenance, and malformed provenance fail closed with a deterministic diagnostic.

When `--embedding-endpoint` and `--embedding-model` are set, search also ranks those same integrity-checked texts through an external OpenAI-compatible `POST /v1/embeddings` endpoint (Hugging Face Text Embeddings Inference, or another self-hosted compatible server). Atlas ranks the returned vectors with cosine similarity, higher score first and projection identity as the tie break, then fuses them with keyword hits using the existing reciprocal rank fusion path. Query and document prefixes are optional operator configuration and default to empty; Atlas does not hardcode a model's prefix rules. Embeddings are not persisted. With no embedding endpoint, search stays keyword-only. Timeout, connection failure, a truncated response body, non-2xx responses, redirects, malformed JSON, missing or empty embeddings, non-numeric or non-finite values, zero-norm query or document vectors, oversized responses, and dimension mismatch fail closed. Each embeddings request contains at most 32 inputs, matching TEI's default `max-client-batch-size`, and Atlas concatenates those responses in input order. Batched `/v1/embeddings` results are bound by response cardinality and per-item `index` (unique, in range, and complete), not by raw array order. Dimension mismatch is rejected within a response and across responses. Semantic hit keys stay on the projection identity used by keyword fusion; the user-visible path still comes from provenance after fusion. Each hit also carries `identity` (`source_id@ref`) so a later provenance lookup can name one projection when several share a `source_path`. The endpoint is an http(s) URL without query, fragment, or userinfo. A root or `/v1` base resolves to `/v1/embeddings`; an explicit `/v1/embeddings` URL is unchanged. Cross-project search, pgvector, and a human UI remain outside this slice.

### Derived Synthesis
Future bounded layer for concepts, entities, cross-project links, contradiction detection, and knowledge-gap detection. Synthesized content is always marked derived and attributable.

### Lifecycle State
Normalizes observable GitHub/CI/test/release evidence into project status. It must distinguish observed fact from inferred/unknown state.

### AI Context / MCP
Exposes scoped retrieval to Cursor, ChatGPT, and other agents. Tool semantics stay Atlas-owned (`atlas/mcp_context.py`). `python -m atlas mcp serve` mounts the official MCP Python SDK Streamable HTTP endpoint at `/mcp` and terminates TLS in-process (ADR-0008).

Atlas is an OAuth 2.1 resource server, not an authorization server. Bearer tokens are checked with RFC 7662 introspection configured at runtime. The SDK publishes RFC 9728 protected-resource metadata and rejects missing, invalid, or wrong-resource tokens. Read tools require `atlas.read`. `search_project` reads current projections for the requested project only. `get_provenance` resolves the search hit `identity` (`source_id@ref`). A user-visible source path resolves only when one projection uses it; shared source paths fail closed as ambiguous. Canonical write tools are not mounted on this endpoint. Live ChatGPT/Cursor client certification is a later slice.

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

Autonomous Work Controller PoC state (ADR-0006) extends the same data root with
`work-controller.json` (one local workstream control loop). It does not replace
GitHub AI Work Packets as canonical coordination state.

Continuous Chat Audit Supervisor PoC state (ADR-0007) extends the same data root
with `chat-audit.json` (derived cache) and a GitHub Contents API Audit Control
Packet (blob-SHA CAS on `atlas/chat-audit-control`). Chat conversations remain
disposable execution instances; the checkpoint is durable. Optional browser
rollover adapters must not redefine checkpoint semantics.
Stagehand remains gated on Issue #19.

Postgres/pgvector/Bun/Wiki.js are not Phase 1 persistence choices.

## Related decisions

- ADR-0001 — canonical state and product boundary
- ADR-0002 — historical Athena fork/pin evidence (superseded long-term strategy)
- ADR-0003 — source/provider/provenance contracts
- ADR-0004 — Athena absorption and repository retirement
- ADR-0005 — Phase 1 persistence model (local filesystem JSON)
- ADR-0006 — Autonomous Work Controller PoC (completion → audit → rework/pass)
- ADR-0007 — Continuous Chat Audit & Session Supervisor PoC
- ADR-0008 — Authenticated HTTPS MCP resource server
