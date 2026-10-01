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

## Surfaces

Web: /personal

CLI: atlas personal show

CLI search: atlas personal search PROJECT QUERY

MCP: get_personal_knowledge

MCP search: search_personal_knowledge

## Cross-project source-class search

Cross-project search accepts one explicit project scope and one source-class filter: all, engineering, or personal. Filtering happens before retrieval. Personal-only search cannot return engineering projections, engineering-only search cannot return personal projections, and all-mode keeps the source relation visible on every result. MCP callers must provide project_ids explicitly.
