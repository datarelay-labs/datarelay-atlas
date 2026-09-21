<h1 align="center">DataRelay Atlas</h1>

<p align="center">
  <strong>Engineering Knowledge & Lifecycle Platform</strong>
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

DataRelay Atlas is an AI-assisted engineering platform that connects project knowledge, methodology, lifecycle state, and AI context.

Atlas gives humans and AI agents one consistent view of engineering methodology, registered products and repositories, architecture/specifications/ADRs, validation and release evidence, current lifecycle state, searchable cross-project knowledge, and trusted context for MCP-capable AI clients.

Atlas does **not** replace GitHub, CI/CD, issue trackers, or coding agents. It connects, validates, indexes, and explains the engineering state that already exists in those systems.

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
2. **Engineering System defines the methodology.** Atlas applies, observes, validates, and explains it; Atlas does not create a competing engineering standard.
3. **Atlas owns knowledge behavior.** Historical Athena/Wiki.js work is migration evidence only; Atlas must not depend on the Athena repository at build/test/runtime (ADR-0004).
4. **Provenance is mandatory.** Derived knowledge keeps repository, ref, path, and source revision where applicable.
5. **Start self-hosted and single-organization.** Multi-organization/SaaS behavior is future scope, not an MVP assumption.
6. **Do not rebuild tools that already work.** Git hosting, CI/CD, coding agents, and issue tracking remain external unless an explicit product requirement changes that boundary.

## Current status

DataRelay Atlas is absorbing required Engineering Knowledge PoC capabilities into Atlas-owned code while retiring Athena as a product dependency (ADR-0004).

Milestones:

- product contract + Engineering System adoption
- source/provider/provenance contracts (ADR-0003)
- Athena absorption inventory + Atlas-native sync/retrieval/MCP context library
- Athena independence gate + retirement checklist (owner delete is separate)

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
