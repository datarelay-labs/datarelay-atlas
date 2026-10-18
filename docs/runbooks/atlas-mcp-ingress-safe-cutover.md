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

## Frozen application candidate versus operator implementation

- **Frozen application bytes:** commit
  `d561eed138d56b6d3878048f66d08c7c5b4723cb`, **431 tracked files**.
  This includes the earlier PR #353 integrated application snapshot
  `01206a200b941abe7773d161f2e342b0c33224ce` (local 1259/1259
  tests, CI #39085945899 SUCCESS), plus a source-only immutable manifest
  generator and its two focused tests. The frozen application HEAD
  `d561eed` separately passed CI #39098463663; these evidence sets must
  not be conflated with tests on later operator HEADs.
- **Operator source:** the later, independently reviewed
  `scripts/atlas_ingress_cutover_transaction.py` in this QA branch. Its
  Git HEAD deliberately differs from the frozen application commit; running
  `--expected-head` must name the *application* commit, not operator HEAD.
  Updating the operator does not silently re-freeze or deploy its source.
- **Immutable tracked-file manifest:** the exact frozen commit's blob
  hashes, **431** records, SHA256
  `ebc19d743236f7f27f6dd8484950bc14d1000949d36febc8e1b6a1282b06f49c`.
  The operator requires the separately delivered artifact at
  `/tmp/atlas-immutable-integrated-d561eed-20261018.sha256`.
- **New unoccupied candidate input:** `/opt/datarelay-atlas-integrated-candidate-d561eed-20261018`,
  assembled from that immutable commit with a compatible, independently
  qualified `.venv/bin/python` and executable dependencies. Do **not** use
  the pre-existing failed retry path as the input. The new input is **not**
  installed or certified on production as part of this source-only work.
- **Separate non-live stage:** `/opt/datarelay-atlas-stage-safe-d561eed-20261018`.
  Separate previous/failed release and environment paths also use this
  new suffix; existing paths are never overwritten.
- **Old live code:** `/opt/datarelay-atlas` (0755), new write module absent.
- **Original immutable rollback code:** `/var/backups/datarelay-atlas/active-code-before-pr347-20261017T0249Z`.
- **Original data backup:** `/var/backups/datarelay-atlas/atlas-mcp-write-9177c976-precutover-20261017`.
- **Original service environment backup:** `/var/backups/datarelay-atlas/atlas-mcp-write-9177c976-code-env-20261017/service.env.before`.
  These three historic identities remain bound to the original running
  service, **not** to the new candidate suffix.
- Keep `/var/lib/datarelay-atlas`, Keycloak, certificates, all prior
  backups and unrelated worktrees intact.

### Source-only immutable artifact preparation

On an authorized **development** checkout, the following performs no
production operation; it creates a fresh manifest from Git *blob objects*
rather than mutable worktree bytes:

```sh
python3 scripts/atlas_release_manifest.py \
  --repo /home/aella/datarelay-atlas-worktrees/atlas-standards-cutover-integration-20261018 \
  --commit d561eed138d56b6d3878048f66d08c7c5b4723cb \
  --output /tmp/atlas-immutable-integrated-d561eed-20261018.sha256
```

The exact output must have 431 entries and the pinned manifest digest.
The independent development-server artifacts were generated at
`/home/aella/datarelay-atlas-release-artifacts/atlas-integrated-d561eed.tar`
(SHA256 `4cd01be9f5d3f97ca2739087855a70a680fc15654344217d34c5b3c2f104069b`)
and `/home/aella/datarelay-atlas-release-artifacts/atlas-integrated-d561eed.sha256`.
The archive contains only the frozen Git-tracked source files: it does
**not** contain a deployable Python environment, operator credentials, or
any service configuration.

**Separate operator artifact:** the reviewed operator code at commit
`f260320c7fda17a1f94ce7187bef21402b7a4af0` was exported to
`/home/aella/datarelay-atlas-release-artifacts/atlas-ingress-operator-f260320.py`
with SHA256
`604728305e8e24cad2547f053ce7120bb923d9133b5a9b3afbc24237a8a69789`.
Its `--help` mode was verified on dev-atlas. The operator script inside the
**frozen application archive is an earlier historical version** and must
not be mistaken for this new reviewed operator. Separate delivery and
execution are subject to the original trusted operations policy.

The existence of the artifacts on dev-atlas is **not** evidence they were
delivered to or validated on prod-atlas.
A distinct, independently authorized production-artifact delivery and
operator context must provide the same immutable manifest, a world-traversable
0755 candidate root with exactly those tracked bytes (excluding `.venv`),
and a validated runtime interpreter at the **new** source path. The
`prepare_stage` operation refuses a missing/mismatched manifest, an
occupied stage, untracked candidate content and incorrect modes.
**Do not** deliver these artifacts or invoke the operator through an
alternate account, host, command encoding, or transport to get around the
previous production execution-policy refusal.

## New bounded transaction

`scripts/atlas_ingress_cutover_transaction.py` has three explicitly
selected operations with an exact-head argument. **It is not executed by
CI, a GitHub Action, or this handoff.** Actual privileged preparation and
cutover require a separately permitted trusted operator execution context.

1. **Prepare:** copy the fixed candidate to a new isolated stage. Verify all
   431 hashes and code root 0755. Create an isolated root:atlas 0640
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
3. **Fresh data preservation:** the `--execute` transaction, after passing
   preflight but **before stopping any service**, captures a separate
   non-overwriting snapshot of **current** durable Atlas state. Require
   seven projects, a nonempty manifest, identical restored file/project
   counts, and upgrade + old-code rollback compatibility on only the new
   disposable restore. An existing fresh-backup path or any failing proof
   halts before service shutdown. `--preflight-only` does not claim this
   live-fresh snapshot gate is complete.
4. **Cutover:** only after preflight and fresh snapshot pass, stop the Ingress
   socket **and** service before moving anything, then stop Web/MCP;
   save active code and environment by rename, move the qualified 0755 code
   and feature-flagged environment into place, start MCP/Web, reset/start
   ingress socket/service, verify all five units and startup persistence,
   the entire live 431-file manifest, Web, public MCP and Keycloak.
   **Do not restart or modify Keycloak itself.**

A failing mutating step (including normal SIGINT/SIGTERM handling) triggers
rollback: stop ingress socket/service before Atlas; restore the old code/env,
restart MCP and Web, reset/start ingress socket/service, and independently
verify the old code/environment identity and BOTH public hostnames. If any
rollback step or mandatory old-state check fails, the outcome is
`INCIDENT_UNRECOVERED`, never PASS. The failed new code is retained in a
separate non-live directory for diagnosis; old backups are never deleted.

The fresh protected snapshot path is
`/var/backups/datarelay-atlas/atlas-mcp-write-d561eed-20261018-fresh-operator`.
Existing historical backups are retained; the new snapshot is never silently
overwritten or removed. A second execution against the same fresh path must
be independently reconciled rather than replacing earlier evidence.

**Limits:** a kernel crash, SIGKILL, lost root process or host failure can
still interrupt in-process rollback; a separately authorized watchdog/
out-of-band recovery mechanism is needed before claiming crash-safe rollback.
No on-host 431-file, DynamicUser, backup, public OAuth or native MCP write
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
systemd dependency mismatch and a disposable-data restore proof. Additional
**actual local filesystem** tests cover both code/env renames, partial code
move, partial env move, already-occupied failed-release slots and corrupted
old-code identity. These tests use disposable directories and mock only
protected ownership/system-service boundaries; they never operate on prod. They run
without root and without any prod-atlas SSH, systemd or data mutation.
The real Atlas `AGENTS.md`, `.engineering/project.yaml`, user acceptance,
exact-HEAD CI, backup and release gates remain mandatory; a new merge SHA
requires independent requalification. The earlier protected Runner Draft
PR #350 is historical/unmerged and is not a dependency of this approach.
