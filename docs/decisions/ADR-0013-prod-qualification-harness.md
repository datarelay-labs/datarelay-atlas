# ADR-0013: Prod-atlas qualification harness

Status: Accepted
Date: 2026-09-26

## Context

Issue #65 prepares public-smoke and operational-E2E machinery while Lane A
activates the prod-atlas runtime. Atlas is pinned to Engineering System
v1.6.5 baseline `14150e424c922ff3a930b45dcf31d3a3d3ba28b2`. At that baseline,
`production_oriented: true` requires `operational_e2e_required`,
`public_smoke_required`, and `full_e2e_passes>=1`, and a non-empty release
command is executed by the pinned release contract. Those flags stay false
until a real prod-atlas run exists. This change does not upgrade the
Engineering System pin.

## Minimal design gate

1. **Goal** — Provide a deterministic qualification harness that can smoke the
   real HTTPS health and MCP surface, exercise register → sync → attributable
   retrieval → MCP tool query → restart/recovery, and call the existing
   backup, restore-test, upgrade, and rollback hooks.
2. **Non-goals** — Flipping `production_oriented`, `public_smoke_required`,
   `operational_e2e_required`, or `full_e2e_passes`; editing systemd or
   service-runtime files; adding an authorization server; a Cursor or ChatGPT
   session inside the harness; claiming a production pass from a local run.
3. **Affected public contract** — `scripts/prod-qualification.py` with
   `public-smoke` and `operational-e2e`. Evidence is secret-free JSON.
   Prod mode proves project `datarelay-atlas` /
   `datarelay-labs/datarelay-atlas` and source
   `docs/product/PRODUCT-CHARTER.md` on the live data root. Only that source
   is synced. Cursor MCP evidence must carry the same endpoint, project,
   query, identity, exact `source_revision`, and deployed code HEAD.
   `.engineering/release.yaml` commands stay empty.
4. **State / migration impact** — No schema change. The local journey uses a
   temporary data root and synthetic content. Prod mode registers the Atlas
   project only when it is absent, and fails closed when an existing
   registration disagrees. It does not overwrite a conflicting project or
   source. Backup and restore-test run only after sync and bound retrieval
   succeed.
5. **Security / operations impact** — Missing prod inputs fail closed before
   any network call or data-root mutation. GitHub credentials are read from
   an operator file outside Git and are not written to evidence, restart
   output, or stored sync errors. Public smoke requires HTTPS, does not
   follow redirects, and records status codes rather than response bodies.
   The anonymous MCP check expects HTTP 401 and sends no bearer token.
   The harness reads `engineering_system.version` and `baseline` from the
   project profile directly. It does not run the Phase 1 adoption parser over
   the rest of that file. A mismatch or unreadable pin fails closed.
   Restart evidence requires a service identity marker that changes across
   the restart command. A zero exit status with an unchanged marker fails.
   Restart is followed by another health check and the same attributable
   retrieval.
6. **Architecture boundary** — `atlas.qualification` owns the harness.
   `atlas.service`, `atlas.mcp_context`, `atlas.data_protection`, and
   `atlas.schema_compat` stay the existing implementations. Lane A continues
   to own deployment and unit installation.
7. **Acceptance / regression criteria** — Local operational E2E passes with
   `production_claim` false. Public smoke fails closed without an HTTPS URL
   and does not claim production for any other host. Prod mode fails closed
   when required inputs are absent. Release and project production flags stay
   false.

## Decision

Ship the harness as an isolated script and module. Local mode is the
deterministic proof and stays synthetic. Prod mode is operator-gated. It
syncs only the canonical Atlas charter with a GitHub credential file and
does not rewrite other enabled sources. It accepts Cursor MCP evidence only
when that evidence names the same public endpoint, project, query, identity,
exact source revision, and deployed code HEAD. Restart must change a service
identity marker. A generic Cursor PASS file is not sufficient. Release
metadata stays unchanged until that live journey has passed.
