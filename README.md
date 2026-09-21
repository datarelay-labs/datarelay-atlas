<h1 align="center">DataRelay Atlas</h1>

<p align="center">
  <strong>Engineering Knowledge & Lifecycle Platform</strong>
</p>

<p align="center">
  Map your engineering. Connect your AI.
</p>

<p align="center">
  <a href="README.ko.md">한국어</a> ·
  <a href="https://github.com/datarelay-labs/engineering-system">Engineering System</a> ·
  <a href="https://github.com/datarelay-labs/datarelay-atlas-docs">Documentation</a>
</p>

---

## What is DataRelay Atlas?

DataRelay Atlas is an AI-assisted engineering platform that connects project knowledge, methodology, lifecycle state, and AI context.

Atlas is designed to give humans and AI agents one consistent view of:

- engineering methodology and standards
- registered products and repositories
- architecture, specifications, ADRs, tests, releases, and operational evidence
- current lifecycle and delivery state
- searchable cross-project engineering knowledge
- trusted context for Cursor, ChatGPT, and other MCP-capable agents

Atlas does **not** replace GitHub, CI/CD, issue trackers, or coding agents. It connects and explains the engineering state that already exists in those systems.

## Core model

```text
Canonical Engineering State
GitHub / OpenSpec / Code / Tests / ADR / CI
                    |
                    v
              DataRelay Atlas
       Methodology + Project State
       Knowledge + AI Context
                    |
          +---------+---------+
          |                   |
          v                   v
      Human UI           AI / MCP Clients
                         Cursor / ChatGPT
```

## Product principles

1. **GitHub remains normative.** Derived knowledge never overrides canonical repository state.
2. **Engineering System defines the methodology.** Atlas applies, observes, and explains it; Atlas does not create a competing engineering standard.
3. **Knowledge engines are replaceable.** Athena may provide indexing, Wiki, semantic retrieval, and MCP infrastructure, but Atlas is the product boundary.
4. **Provenance is mandatory.** Derived knowledge must retain repository, ref, path, and source revision where applicable.
5. **Start self-hosted and single-organization.** Multi-organization/SaaS behavior is future scope, not an MVP assumption.
6. **Do not rebuild tools that already work.** Git hosting, CI/CD, coding agents, and issue tracking stay external unless a clear product requirement changes that boundary.

## Current status

DataRelay Atlas is in initial product definition and architecture bootstrap.

The current phase establishes the product contract, Engineering System adoption, knowledge architecture, and migration path from the existing Athena proof of concept.

## Engineering

This repository follows the canonical [Data Relay Labs Engineering System](https://github.com/datarelay-labs/engineering-system).

Start with:

- `AGENTS.md`
- `.engineering/project.yaml`
- `docs/product/PRODUCT-CHARTER.md`
- `docs/architecture/ARCHITECTURE.md`
- `docs/roadmap/ROADMAP.md`

Human-facing documentation lives in [datarelay-atlas-docs](https://github.com/datarelay-labs/datarelay-atlas-docs).

## License

Source-available. The project license will follow the Data Relay Source Available License model used by Data Relay Labs products, with third-party components continuing under their own licenses.
