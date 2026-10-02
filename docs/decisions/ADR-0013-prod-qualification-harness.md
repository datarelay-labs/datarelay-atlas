# ADR-0013: Prod-atlas qualification harness

Status: Accepted
Date: 2026-09-26

## Context

Issue #65 prepares public-smoke and operational-E2E machinery while Lane A
activates the prod-atlas runtime. Atlas is pinned to Engineering System
v1.7.0 baseline `930e01f72403b28099a37fc9aaa63146cb832f6a`. Under the active
Engineering System contract, `production_oriented: true` requires the configured
release qualification gates;
those flags stay false until a real prod-atlas run exists. Managed baseline
adoption advances the approved exact pin together with this qualification gate;
a project profile that disagrees with the approved pin still fails closed.

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
   query, identity, exact `source_revision`, and the checkout commit from
   `git -C <repo_root> rev-parse --verify HEAD^{commit}`. An operator-supplied
   SHA is not an input.
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
   The deployed code HEAD is the full commit SHA of the qualification checkout.
   Cursor evidence that names another SHA fails closed. An environment variable
   cannot supply that SHA.
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

## Core-local integrated qualification

The qualification harness also provides `operational-e2e --mode core-local` as the deterministic integrated proof for the Atlas Core finish line. It composes existing Registry/Sync, keyword+semantic retrieval, explicit cross-project search, Human UI, MCP provenance, Lifecycle Intelligence, Derived Intelligence, restart/recovery, backup/restore, and upgrade/rollback behavior. The detailed contract is `docs/contracts/ATLAS_CORE_PRODUCT_E2E.md`.

`core-local` is deliberately non-production. Its deterministic embedding client is injected only through the existing semantic interface, and its PASS cannot set production release state or satisfy Surface Reconciliation / Full User E2E.

## Decision

Ship the harness as an isolated script and module. Local mode is the
deterministic proof and stays synthetic. Prod mode is operator-gated. It
syncs only the canonical Atlas charter with a GitHub credential file and
does not rewrite other enabled sources. It accepts Cursor MCP evidence only
when that evidence names the same public endpoint, project, query, identity,
exact source revision, and the checkout's full HEAD. Restart must change a service
identity marker. A generic Cursor PASS file is not sufficient. Release
metadata stays unchanged until that live journey has passed.

## Managed baseline update — 2026-10-02

The managed Engineering System adoption advanced to v1.7.0 baseline
`7d7c83aeb3cd7766bc224e8496a283f5af87edd5` at policy epoch 4. In accordance
with this ADR's original fail-closed decision, `atlas.qualification` advances
its separately approved exact baseline pin in the same change. A project profile
naming any other baseline still fails closed before qualification performs network
or data-root mutation.
