# Atlas MCP ingress-aware cutover: existing dev-atlas → prod-atlas

**Status: code prepared for independent review only. Production code is not
switched.** See [Issue #346](https://github.com/datarelay-labs/datarelay-atlas/issues/346).

This is an alternative to provisioning a new VM or GitHub self-hosted Runner.
It is **not** an alternative route around a rejected Desktop Commander command:
the prior `Remote_Desktop_Commander.start_process` permission refusal for a
privileged `dev-atlas → prod-atlas` preflight remains binding. Do not call this
candidate on production through a different connector, terminal, host, account,
interpreter, or owner-executed command as a workaround. Use it only after the
same trusted operations boundary separately authorizes actual execution.

## Frozen inputs and protected state

- Source candidate: GitHub main
  `9177c976a591143b045ebdeabdbccd0d48b87c6c`, 421 tracked files.
- Immutable tracked-file SHA256 manifest:
  `/tmp/atlas-immutable-main-9177c976-20261017.sha256`, expected digest
  `f49f8327236b94b48912e6b5df8ca4a72469ab50911775d1bfd2ae750d18235e`.
- Fixed 0755 release candidate (leave untouched):
  `/opt/datarelay-atlas-failed-retry-9177c976-20261017`.
- Old live code: `/opt/datarelay-atlas` (0755), new write module absent.
- Old code backup:
  `/var/backups/datarelay-atlas/active-code-before-pr347-20261017T0249Z`.
- Data backup and independently tested restore from earlier Issue evidence:
  `/var/backups/datarelay-atlas/atlas-mcp-write-9177c976-precutover-20261017`.
- Old protected service environment:
  `/var/backups/datarelay-atlas/atlas-mcp-write-9177c976-code-env-20261017/service.env.before`.
- Keep `/var/lib/datarelay-atlas`, Keycloak, certificates, existing backups
  and unrelated worktrees intact.

## New bounded transaction

`scripts/atlas_ingress_cutover_transaction.py` has three explicitly
selected operations with an exact-head argument. **It is not executed by
CI, a GitHub Action, or this handoff.** Actual privileged preparation and
cutover require a separately permitted trusted operator execution context.

1. **Prepare:** copy the fixed candidate to a new isolated stage. Verify all
   421 hashes and code root 0755. Create an isolated root:atlas 0640
   environment with only the memory/source write opt-in flags changed.
   Existing live code, live environment and data are untouched.
2. **Preflight:** verify stage, manifest, existing original code/env/data
   backups and source code identity; validate root:atlas permissions and
   exact flags. Run restore-test plus upgrade/rollback compatibility probes
   only against a *disposable restored data-root*, not production state.
   Validate current systemd `DynamicUser` and `Requires/After` relationships,
   directly test the staged ingress interpreter in a matching hardened
   isolated `systemd-run` unit. Require all five live units and startup
   settings, public HTTPS MCP, public Keycloak issuer/OAuth endpoints and
   loopback Web HTTP200.
3. **Cutover:** only after all preflight checks pass, stop the Ingress
   socket **and** service before moving anything, then stop Web/MCP;
   save active code and environment by rename, move the qualified 0755 code
   and feature-flagged environment into place, start MCP/Web, reset/start
   ingress socket/service, verify all five units and startup persistence,
   the entire live 421-file manifest, Web, public MCP and Keycloak.
   **Do not restart or modify Keycloak itself.**

A failing mutating step (including normal SIGINT/SIGTERM handling) triggers
rollback: stop ingress socket/service before Atlas; restore the old code/env,
restart MCP and Web, reset/start ingress socket/service, and independently
verify the old code/environment identity and BOTH public hostnames. If any
rollback step or mandatory old-state check fails, the outcome is
`INCIDENT_UNRECOVERED`, never PASS. The failed new code is retained in a
separate non-live directory for diagnosis; old backups are never deleted.

**Limits:** a kernel crash, SIGKILL, lost root process or host failure can
still interrupt in-process rollback; a separately authorized watchdog/
out-of-band recovery mechanism is needed before claiming crash-safe rollback.
No on-host 421-file, DynamicUser, backup, public OAuth or native MCP write
success is asserted by unit tests. An existing platform/tool denial remains
an absolute stop for applying the change through that boundary.

## Post-cutover actual user acceptance (pending)

Only after a permitted live cutover, directly discover the native connected
ChatGPT Atlas MCP tools `ingest_memory_candidates` (`atlas.write`) and
`register_github_source` (`atlas.sources.write`), complete OAuth optional
scope consent where required, perform real bounded write, replay/idempotency,
readback, project-scoped denial, GitHub Markdown provenance and audit checks.
Private GitHub source retrieval needs independently approved least-privilege
read access; an unavailable private source is not a sync PASS. A successful
candidate code switch alone does not close Issue #346.

## Evidence and release controls

The isolated dev-atlas worktree tests exercise success, every forward
operation fault, preflight refusal, rollback failure, stage/flag integrity,
systemd dependency mismatch and a disposable-data restore proof. They run
without root and without any prod-atlas SSH, systemd or data mutation.
The real Atlas `AGENTS.md`, `.engineering/project.yaml`, user acceptance,
exact-HEAD CI, backup and release gates remain mandatory; a new merge SHA
requires independent requalification. The earlier protected Runner Draft
PR #350 is historical/unmerged and is not a dependency of this approach.
