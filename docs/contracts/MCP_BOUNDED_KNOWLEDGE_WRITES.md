# Bounded MCP Knowledge Write Contract

## Scope

**Development implementation only; no production activation by default.** Canonical engineering truth stays in GitHub/OpenSpec/code/tests/ADR/CI. Atlas candidate memory is noncanonical; registered GitHub source projections are derived. Do not use Atlas as a GitHub editor or execution authority.

Existing normal MCP tools require `atlas.read`. Two optional tool families are discoverable **only** with operator-controlled feature flags; both are `0`/unset by default.

| Flag | MCP tool | Tool-specific OAuth scope |
| --- | --- | --- |
| `ATLAS_MCP_MEMORY_WRITE_ENABLED=1` | `ingest_memory_candidates` | `atlas.write` |
| `ATLAS_MCP_SOURCE_WRITE_ENABLED=1` | `register_github_source` | `atlas.sources.write` |

The MCP client token needs `atlas.read` plus the appropriate tool-specific scope. Enabling any flag/issuing a new scope on production is a governed permission+production change, **not** part of this code PR. The production `ops check --prod` and runtime `require_ready_to_bind` accept these two optional flags with **exactly `0` or `1`**; unknown or mistyped values fail closed rather than leaving the operator believing the tool is enabled. A valid feature flag does **not** grant OAuth scope.

## Submit bounded memory

Use `ingest_memory_candidates` with:

```json
{
  "project_id": "datarelay-atlas",
  "input_kind": "RUN_SUMMARY",
  "observed_at": "2026-10-17T00:00:00Z",
  "workstream": "shared-e2e",
  "candidates": [
    {
      "candidate_class": "LESSON_LEARNED",
      "content": "Use the approved noVNC runbook for shared-browser E2E; verify the session before connecting."
    }
  ]
}
```

Only interaction/run summaries are accepted through this MCP entry. The observed timestamp must include a timezone and cannot be materially in the future. The existing store validates candidate class/content/secrets/provenance, limits each batch to eight, and uses deterministic candidate IDs for exact replay. Results include `accepted`, `replayed`, `candidate_ids`, `canonical:false`. Read back using existing `get_memory_candidates(project_id=...)`. Use repository/operator controls for correction, pinning, or forget. A candidate is never accepted as an Engineering System instruction/approval.

## Register and sync a canonical GitHub source

First create/review/merge the document into the project's already configured default GitHub branch. Only then call `register_github_source`:

```json
{
  "project_id": "datarelay-atlas",
  "source_id": "shared-chromium-e2e",
  "source_path": "docs/runbooks/shared-headed-chromium-e2e-access.md",
  "title": "Shared Chromium E2E via Mac SSH noVNC"
}
```

No caller-supplied URL, GitHub credentials, repository or ref is accepted. The server resolves the registered project's repository/ref; validates the relative docs Markdown path and source identity; fetches the canonical file and GitHub blob SHA; then adds or reuses only the matching source and refreshes one derived projection. A source-id mismatch is a validation error. The normal `source add` duplicate error is avoided on exact retries.

A successful response contains `state:SYNCED|UNCHANGED`, `sync_state:success|unchanged`, project/source/repository/path/ref/blob revision, `canonical:false`, `derived:true`. Confirm via `get_source_detail` and `search_project`. A failed fetch does not create a source entry; an unexpected sync failure is not reported as PASS.

## Audit and effectiveness

Only `ATLAS_MCP_TOOL_EXECUTION tool=<name> outcome=SUCCESS|BLOCK|ERROR` is recorded for completed authenticated dispatch. Never store raw chat, passwords, bearer tokens, OTPs, or credentials. `get_memory_effectiveness` remains read-only and reports `NO_DATA` or `INSUFFICIENT` until enough actual observations exist; an HTTPS fixture PASS is **not** a production effectiveness measurement.

## Rollback and recovery

With flags unset, newly introduced tool names are absent; existing read-only retrieval and write probes remain unchanged. Do not delete existing registry/projections to recover from source conflicts. Roll back code/flags through the usual approved deployment process and preserve registered state. Use normal Atlas backup/restore procedures when a genuine data incident requires it.
