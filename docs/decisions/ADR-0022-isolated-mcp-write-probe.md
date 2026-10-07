# ADR-0022: Isolated MCP write-capability probe

Status: Accepted
Date: 2026-10-07

## Context

Atlas production MCP has intentionally been read-only. ADR-0018 explicitly excluded new write scopes. We now need to determine, using the real ChatGPT-to-Atlas MCP path, whether the connected ChatGPT plan and plugin runtime can invoke a write-class MCP tool.

Using a canonical project mutation for this experiment would mix product state with a capability test and would make a failed cleanup consequential.

## Decision

Add exactly three bounded probe tools:

- `create_write_probe(value)` — requires `atlas.write`.
- `get_write_probe(probe_id)` — requires `atlas.read`.
- `delete_write_probe(probe_id)` — requires `atlas.write`.

Probe records are not Atlas knowledge. They are stored under an OS temporary directory keyed by MCP resource identity, outside the configured Atlas data root. They are excluded from registry, projection, retrieval, memory, backup/restore, and lifecycle truth.

Values are limited to 512 UTF-8 bytes. Probe IDs are random 128-bit hex identifiers. Records expire after 15 minutes and stale/corrupt probe files are purged opportunistically.

The write tools advertise MCP write annotations and an OAuth `atlas.write` security scheme. This ADR narrows ADR-0018's "no new write scopes" decision only for this isolated capability probe; it does not authorize general Atlas, GitHub, project, policy, note, or derived-knowledge mutation.

## Consequences

A read-only token can discover the probe tools and read a known live probe but cannot create or delete one. A token carrying both `atlas.read` and `atlas.write` can complete create → read → delete.

Production OAuth configuration must not be changed merely because the code supports the scope. Enabling `atlas.write` for a production client is a permission-boundary/production change and requires the normal trusted approval path.

A successful probe demonstrates only that this MCP client/runtime can invoke this bounded write tool. It does not authorize broader Atlas write functionality.
