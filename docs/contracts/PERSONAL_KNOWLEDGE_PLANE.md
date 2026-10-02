# Personal Knowledge Plane Product Contract

Personal/reference knowledge is a distinct non-authoritative source class. Atlas may index and retrieve it for context, but it never becomes canonical engineering evidence.

## Product surface

The read-only dashboard reports registered personal sources, engineering-source counts excluded from personal search, projection state, durable local snapshot presence and digest identity, and bounded import or quarantine metadata. It also reports engineering_authority=false and no Tela runtime dependency.

Dedicated personal search filters the index to source_class=personal before retrieval. Engineering projections are not eligible results.

## Import manifest

The optional durable manifest is stored at personal-snapshots/import-manifest.json.

It contains metadata only. Allowed item fields are source id, external id, title, state, and bounded reason code. Raw source bodies, rejected content, credentials, tokens, and detector-matched values are forbidden.

Allowed states are IMPORTED, SANITIZED, QUARANTINED, and REJECTED.

The manifest is below personal-snapshots, so the existing data-root backup and restore path preserves it without a new persistence subsystem.

## Authority and safety

Personal content is always derived reference context. Engineering authority remains false. Quarantined or rejected items expose only bounded metadata. The dashboard does not mutate external sources or quarantine state. Tela is not a production runtime dependency. Existing GitHub, spec, ADR, CI, test, and release authority remains unchanged.

## Markdown directory import

An operator may import one explicitly selected external Markdown directory with:

`atlas personal import-dir PROJECT --root PATH --collection-id ID`

The directory importer reuses the existing `local-markdown` personal snapshot contract; it does not add an Obsidian/Tela runtime dependency or a new authority/persistence layer. `collection-id` creates a stable namespace and each Markdown file receives a deterministic source id derived from its collection-relative path.

Safety and lifecycle rules:

- the selected root must be a real directory outside the Atlas data root;
- symlinks fail the whole import closed;
- only regular lowercase `.md` files are eligible; other files are reported as rejected metadata and are never ingested;
- file count, directory-entry count, per-file bytes, total Markdown bytes, and relative path length are bounded; byte limits are rechecked against the bytes actually opened/read so scan-time size races cannot bypass them;
- non-UTF-8/surrogateescaped filenames are rejected as bounded generic metadata without echoing undecodable path bytes;
- secret-like new files are quarantined without persisting their bodies;
- if a previously registered file becomes secret-like, re-import fails closed rather than silently serving a stale projection as current;
- unchanged imports are idempotent; changed content refreshes the existing snapshot/projection only when source id and logical path still match;
- absent files are reported as missing, but existing Atlas sources are not automatically deleted; legacy missing paths that match the secret detector are replaced with a generic redacted-path marker;
- output contains bounded metadata/counts only, never Markdown bodies.

The collection-relative logical source path is retained in Atlas provenance, so personal search remains attributable while engineering authority remains false.

## Surfaces

Web: /personal

CLI: atlas personal show

CLI search: atlas personal search PROJECT QUERY

CLI directory import: atlas personal import-dir PROJECT --root PATH --collection-id ID

MCP: get_personal_knowledge

MCP search: search_personal_knowledge

## Cross-project source-class search

Cross-project search accepts one explicit project scope and one source-class filter: all, engineering, or personal. Filtering happens before retrieval. Personal-only search cannot return engineering projections, engineering-only search cannot return personal projections, and all-mode keeps the source relation visible on every result. MCP callers must provide project_ids explicitly.
