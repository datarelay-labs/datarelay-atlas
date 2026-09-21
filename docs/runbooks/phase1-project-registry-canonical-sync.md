# Phase 1 operator runbook — register, sync, rebuild

This runbook exercises the bounded Phase 1 exit condition: one project can be
registered and its selected canonical knowledge is reproducibly projected.

## Prerequisites

- Python 3.11+ (stdlib + optional `PyYAML`; Atlas includes a constrained YAML
  parser for `.engineering/project.yaml` when PyYAML is absent)
- GitHub read credentials via `GITHUB_TOKEN` or `gh` auth (never commit tokens)
- Working directory: Atlas repository checkout

## Data root

- Default: `./.atlas-data/` (gitignored)
- Override: `--data-root /path` or `ATLAS_DATA_ROOT`
- Contents:
  - `registry.json` — Atlas-owned durable project/source configuration (ADR-0005)
  - `projections/` — rebuildable derived knowledge + metadata

Backup implication: copy the data root directory. Restoring registry restores
configuration; run `rebuild`/`sync` to refresh derived projections.

## E2E flow

```bash
export ATLAS_DATA_ROOT="$(pwd)/.atlas-data-e2e"
rm -rf "$ATLAS_DATA_ROOT"
export PYTHONPATH=.

# 1. Register this repository as a project
python3 -m atlas project register datarelay-atlas \
  --repository datarelay-labs/datarelay-atlas \
  --display-name "DataRelay Atlas" \
  --ref main

# 2. Read Engineering System adoption metadata
python3 -m atlas adoption datarelay-atlas

# 3. Configure at least two canonical sources
python3 -m atlas source add datarelay-atlas charter \
  --path docs/product/PRODUCT-CHARTER.md \
  --title "Product Charter"
python3 -m atlas source add datarelay-atlas architecture \
  --path docs/architecture/ARCHITECTURE.md \
  --title "Architecture"

# 4. Authenticated sync
python3 -m atlas sync datarelay-atlas

# 5. Inspect immutable revisions / provenance
python3 -m atlas projections datarelay-atlas

# 6. Repeat sync — expect sync_state=unchanged when remote revision is stable
python3 -m atlas sync datarelay-atlas

# 7. Rebuild derived projection from registered config
python3 -m atlas rebuild datarelay-atlas

# 8. Show / list helpers
python3 -m atlas project show datarelay-atlas
python3 -m atlas project list
python3 -m atlas source list datarelay-atlas
```

## Expected failure behavior

- Invalid/duplicate `project_id` or `source_id` → CLI exits non-zero
- Path traversal in source paths → rejected
- GitHub fetch failure → projection `sync_state=error`; content is not presented
  as newly current via `list_documents`
- Credentials are never written into registry or projection metadata

## Non-goals

Human UI, semantic retrieval, MCP HTTPS transport, multi-tenant storage, and
production backup automation are out of Phase 1 scope.
