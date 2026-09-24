# ADR-0009: Production service runtime and health

Status: Accepted
Date: 2026-09-24

## Context

Atlas already serves project-scoped retrieval as an OAuth 2.1 resource server
(`python -m atlas mcp serve`, ADR-0008). Issue #42 asks for a staging-testable
Linux service layout around that process: installation, configuration outside
Git, a non-root lifecycle, and health that means more than "a process exists."

Issue #41 stays BLOCKED / HUMAN_REQUIRED. `prod-atlas` does not resolve from
this host, so this change must not mutate or claim a production deployment.

ChatGPT pre-implementation audit comment 5823936006 requires two production
invariants. Issue #42 was then reduced, and Issue #43 was queued for the
durable-state half:

- Phase 1 persistence writes `registry.json`, `projections/projections.json`,
  and projection documents with direct file writes and no global lock. A live
  directory copy can capture a partial snapshot. Consistent backup, restore
  verification, and schema-safe rollback are Issue #43. This ADR does not claim
  an online-consistent backup.
- `.engineering/project.yaml` stays non-production. `production_oriented`,
  `runbook_required`, and `incident_response_required` remain false.
  Operational E2E and public smoke stay disabled until real `prod-atlas`
  evidence exists. The existing `backup_command` / `restore_test_command` are
  left unchanged and are not a consistency proof. `upgrade_command` and
  `rollback_command` stay empty. The one operations-contract change in this ADR
  is a real `health_command`.

## Minimal design gate

1. **Goal** — Install and operate the existing MCP process as a dedicated
   non-root systemd service, with secrets outside Git, fail-closed
   configuration, and a readiness signal that the data root is usable.
2. **Non-goals** — Backup, restore, upgrade, and rollback implementation;
   flipping the Engineering System production profile; public DNS or
   certificate provisioning; a real `prod-atlas` deployment; ChatGPT OAuth or
   tool E2E; a web UI; Docker or Kubernetes; an embedded authorization server;
   claiming that the current directory-copy backup is consistent.
3. **Affected public contract** — systemd unit
   `deploy/systemd/datarelay-atlas.service`; environment file
   `/etc/datarelay-atlas/service.env` (example only in Git);
   `python -m atlas ops check` and `python -m atlas ops stage`; unauthenticated
   `GET /healthz`. Bind host and port may come from `ATLAS_MCP_BIND_HOST` and
   `ATLAS_MCP_BIND_PORT`. The introspection client secret may come from
   `ATLAS_MCP_INTROSPECTION_CLIENT_SECRET_FILE`. Search and provenance tool
   behavior is unchanged.
4. **State / migration impact** — No new durable schema and no migration.
   Readiness fails closed when `registry.json` or
   `projections/projections.json` is present but unreadable or structurally
   unsupported. Unsupported state is not rewritten.
5. **Security / operations impact** — The unit user and group are `atlas`, not
   root. `NoNewPrivileges=true` and `UMask=0077`. TLS private keys, inline
   secrets, and the env file must not be world-accessible. The certificate may
   be world-readable and must not be world-writable. `ops check` prints setting
   names and status codes only. `GET /healthz` returns `{"status":"ready"}` or
   `{"status":"not_ready"}` and no canonical data, credentials, token state, or
   internals. Atlas remains a resource server; `/authorize` and `/token` stay
   absent.
6. **Architecture boundary** — `atlas.mcp_http` still owns Streamable HTTP and
   resource-server auth. `atlas.ops` owns service-env assessment, unit staging,
   and the data-root readiness predicate. systemd owns boot persistence.
   Issue #43 will own quiesced backup and schema-compatible rollback.
7. **Acceptance / regression criteria** — Tests cover fail-closed config,
   secret redaction, world-readable secret rejection, non-root unit text,
   `/healthz` without project or credential data, and unsupported registry or
   projection metadata. They do not need root, a systemd daemon, network
   secrets, or a production host. Existing MCP auth tests stay green.

## Decision

1. Ship one system unit that starts `.venv/bin/python -m atlas mcp serve` as
   `atlas:atlas`, reads `/etc/datarelay-atlas/service.env`, and writes only
   `/var/lib/datarelay-atlas`.
2. Treat that env file as the service configuration. `ops check --env-file`
   does not consult the ambient process environment. Mandatory MCP and TLS
   settings must be present and valid before the report is `ready`.
   `GITHUB_TOKEN` is optional and is reported only as `present` or `absent`.
   Semantic settings are optional; a partial set is not ready. Unknown keys
   fail closed.
3. Publish `GET /healthz` on the existing TLS server without authentication.
   Ready means the data root is a real directory and any existing registry or
   projection metadata parses and matches the structure this code can read.
4. Set `operations.health_command` to `python -m atlas ops check --env-file`.
   Do not set `production_oriented: true` and do not invent upgrade, rollback,
   or replacement backup commands in this change.
5. TLS terminates in the MCP process with operator files. A proxy may forward
   TCP to that listener. This packet does not add a cleartext bind or an
   authorization server.

## Consequences

- An operator can stage the unit and validate a private env file on a dev host
  without installing it into the system instance or touching `prod-atlas`.
- A world-readable key or a corrupt registry prevents a ready result. It does
  not repair the file.
- Backup consistency, restore rejection of partial snapshots, and
  upgrade/rollback remain unimplemented until Issue #43.
- Live ChatGPT MCP E2E remains Issue #41.
