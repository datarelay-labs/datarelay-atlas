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

DataRelay Atlas is absorbing required Engineering Knowledge PoC capabilities into Atlas-owned code while retiring Athena as a product dependency (ADR-0004). Phase 1 adds a local project registry and authenticated canonical sync (ADR-0005).

Milestones:

- product contract + Engineering System adoption
- source/provider/provenance contracts (ADR-0003)
- Athena absorption inventory + Atlas-native sync/retrieval/MCP context library
- Athena independence gate + retirement checklist (owner delete is separate)
- Phase 1 project registry + canonical sync operator surface (`python -m atlas`)
- Autonomous Work Controller PoC (`python -m atlas work-controller`, ADR-0006)

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

No matches print `[]` and exit 0. Missing projection bytes or malformed provenance exit non-zero.

## Autonomous Work Controller PoC

Persist one local workstream, accept an idempotent Cursor completion event, run a
replaceable audit adapter, and stop or dispatch a fresh `/work-resume` on rework.
See ADR-0006 and `docs/runbooks/autonomous-work-controller-poc.md`.

```bash
PYTHONPATH=. python3 -m atlas work-controller register <workstream> \
  --repository datarelay-labs/datarelay-atlas \
  --issue-number <n> \
  --branch <branch> \
  --worktree "$(pwd)" \
  --expected-head "$(git rev-parse HEAD)"
PYTHONPATH=. python3 -m atlas work-controller completion /path/to/event.json \
  --audit-adapter fixed \
  --audit-verdict PASS
```

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
