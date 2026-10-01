<h1 align="center">DataRelay Atlas</h1>

<p align="center">
  <strong>Trusted Engineering Context & Lifecycle Control Plane</strong>
</p>

<p align="center">
  Map your engineering. Connect your AI.
</p>

<p align="center">
  <strong>English</strong> · <a href="README.ko.md">한국어</a> ·
  <a href="https://github.com/datarelay-labs/engineering-system">Engineering System</a> ·
  <a href="https://github.com/datarelay-labs/datarelay-atlas-docs">Documentation</a>
</p>

---

## What is DataRelay Atlas?

DataRelay Atlas is an AI-assisted Engineering Knowledge & Lifecycle Platform. Its Core product is the trusted engineering context and lifecycle control plane shared by humans and AI clients.

Atlas gives humans and AI clients one consistent view of engineering methodology, registered products and repositories, architecture/specifications/ADRs, validation and release evidence, current lifecycle state, searchable cross-project knowledge, and attributable context for MCP-capable clients.

Atlas does **not** replace GitHub, CI/CD, issue trackers, or coding agents. It connects, validates, indexes, and explains the engineering state that already exists in those systems. Optional automation can coordinate AI-assisted engineering work on top of that trusted state, but automation/provider optimization is not required for Atlas Core to be complete.

## Core model

```text
Canonical Engineering State
GitHub / Specs / Code / Tests / ADR / CI
                    |
                    v
             DataRelay Atlas Core
       Knowledge + Lifecycle + Trust
                    |
          +---------+---------+
          |                   |
          v                   v
      Human UI           AI / MCP Clients
                         ChatGPT / approved clients
                    |
                    +---- optional ----> Automation Extension
```

## Local Human UI

Atlas includes a read-only local Human UI for registered project knowledge and lifecycle navigation:

```bash
ATLAS_DATA_ROOT=/path/to/atlas-data python -m atlas web serve
```

The first slice binds to `127.0.0.1:8788` by default and is intentionally not a remotely exposed production UI. It provides project inventory, source/projection state, attributable project/cross-project search, and bounded lifecycle evidence without canonical writes.

## Product principles

1. **GitHub remains normative.** Derived knowledge never overrides canonical repository state.
2. **Engineering System defines the methodology.** Atlas applies, observes, validates, and explains it; Atlas does not create a competing engineering standard.
3. **Atlas owns knowledge behavior.** Historical Athena/Wiki.js work is migration evidence only; Atlas must not depend on the Athena repository at build/test/runtime (ADR-0004).
4. **Provenance is mandatory.** Derived knowledge keeps repository, ref, path, and source revision where applicable.
5. **Start self-hosted and single-organization.** Multi-organization/SaaS behavior is future scope, not an MVP assumption.
6. **Do not rebuild tools that already work.** Git hosting, CI/CD, coding agents, and issue tracking remain external unless an explicit product requirement changes that boundary.
7. **Core before automation.** Dependency scheduling, provider routing, decision models, and other automation consume Core state; they are not Core release prerequisites unless explicitly promoted.
8. **Search is not generic chat.** Atlas owns attributable retrieval/context packaging; answer synthesis is normally performed by the approved MCP client.

## Current status

Atlas owns its registry, canonical sync, projection/retrieval, authenticated HTTPS MCP, lifecycle/control-plane foundations, and the first read-only Human UI. Athena remains historical migration evidence only and is not a runtime dependency. The browser surface is local/loopback in the current slice; remote UI authentication/exposure and same-candidate human-equivalent release execution remain explicit Core hardening gates.

The roadmap separates **Atlas Core** from the optional **Automation Extension** and **Experimental/Optional** work. Core completion is judged by one end-to-end attributable knowledge + lifecycle + operations journey, not by completing every provider, automation, or research experiment.

Milestones:

- product contract + Engineering System adoption
- source/provider/provenance contracts (ADR-0003)
- Athena absorption inventory + Atlas-native sync/retrieval/MCP context library
- Athena independence gate + retirement checklist (owner delete is separate)
- Phase 1 project registry + canonical sync operator surface (`python -m atlas`)
- optional Automation Extension foundation: Autonomous Work Controller PoC (`python -m atlas work-controller`, ADR-0006)

## Phase 1 operator surface

Register one project, configure canonical source paths, sync/rebuild derived projections, and read Engineering System adoption metadata:

```bash
PYTHONPATH=. python3 -m atlas project register datarelay-atlas \
  --repository datarelay-labs/datarelay-atlas
PYTHONPATH=. python3 -m atlas source add datarelay-atlas charter \
  --path docs/product/PRODUCT-CHARTER.md
PYTHONPATH=. python3 -m atlas sync datarelay-atlas
PYTHONPATH=. python3 -m atlas rebuild datarelay-atlas
```

Durable local state defaults to `.atlas-data/` (gitignored). Credentials use `GITHUB_TOKEN` / runtime env only. See `docs/runbooks/phase1-project-registry-canonical-sync.md`.

Search one registered project's successful projections. The keyword index is rebuilt in memory and each hit keeps projection provenance:

```bash
PYTHONPATH=. python3 -m atlas search datarelay-atlas "product charter"
```

Optional semantic ranking uses a self-hosted embeddings endpoint compatible with Hugging Face Text Embeddings Inference `POST /v1/embeddings`. Omit the endpoint to keep keyword-only search. The endpoint and model are runtime flags; do not commit them.

```bash
PYTHONPATH=. python3 -m atlas search datarelay-atlas "product charter" \
  --embedding-endpoint http://127.0.0.1:8080 \
  --embedding-model bge-small-en-v1.5
```

No matches print `[]` and exit 0. Missing projection bytes, malformed provenance, or embedding transport/payload failures exit non-zero.

Search JSON includes `path` (the source path) and `identity` (`source_id@ref`). Use `identity` when two projections share a source path.

## Authenticated MCP

`python -m atlas mcp serve` exposes `search_project` and `get_provenance` on Streamable HTTP `/mcp` over TLS. Atlas checks bearer tokens as an OAuth resource server; it does not issue them. See ADR-0008 and `docs/runbooks/phase2-authenticated-https-mcp.md`.

## Automation Extension

Atlas includes Work Controller and audit foundations for durable AI-engineering coordination. This is an **optional extension** built on Atlas Core, not part of the Core product finish line.

The original ADR-0006 PoC modeled Cursor completion/resume transport. Current product direction is Chat-primary and provider-neutral: GitHub Work Packets remain durable coordination authority, independent verification remains explicit, and no provider-specific runtime may become hidden execution authority. Historical Cursor evidence may remain for compatibility/audit purposes while current execution contracts are retired or migrated.

See ADR-0006, the active Automation Extension roadmap, and docs/runbooks/autonomous-work-controller-poc.md for implementation history.

## Engineering

This repository follows the canonical [Data Relay Labs Engineering System](https://github.com/datarelay-labs/engineering-system), pinned by `.engineering/project.yaml`.

Start with:

- `AGENTS.md`
- `.engineering/project.yaml`
- `docs/product/PRODUCT-CHARTER.md`
- `docs/architecture/ARCHITECTURE.md`
- `docs/roadmap/ROADMAP.md`

Human-facing documentation lives in [datarelay-atlas-docs](https://github.com/datarelay-labs/datarelay-atlas-docs).

## License

The repository is currently all-rights-reserved pending the dependency/license compatibility audit. The intended Data Relay Labs model is source-available with internal commercial use permitted and productization/SaaS restrictions, while third-party components retain their own licenses.
