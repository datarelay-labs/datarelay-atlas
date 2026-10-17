# Atlas protected-production release boundary (prepared, not activated)

**State:** Prepared qualification-only workflow. This change does **not** create
a runner, grant production permission, replace running code, or deploy the MCP
knowledge-write feature. Track deployment and acceptance in
[datarelay-atlas Issue #346](https://github.com/datarelay-labs/datarelay-atlas/issues/346).

## Scope and trust boundary

The approved alternative to the rejected Desktop Commander command is an
independently administered release execution boundary—not rewriting the
rejected shell command or removing a blocklist entry. Existing
`Remote_Desktop_Commander.start_process` refusals remain binding.
Do not route the refused prod-atlas operation through a new connector, terminal,
runner or helper solely to escape that refusal. Provisioning a new production
execution authority requires its own controls and evidence before use.

The checked-in workflow `.github/workflows/atlas-protected-release-boundary.yml`
is **qualification-only**:

- `workflow_dispatch` from the protected default branch; no PR trigger.
- Full expected candidate SHA, validated by the canonical reusable release
  workflow against protected main history; not an arbitrary branch.
- Reusable workflow pinned to the Engineering System 1.7.0 baseline SHA
  `e85d6a4de7e7df2dcbcf4f5dd43269bb308de195`.
- `contents: read`, no secrets passed, serialized workflow dispatches.
- Canonical runner group `engineering-release-production`, self-hosted runner
  and same-named label; runner marker `ENGINEERING_RELEASE_CONTEXT` must be
  `protected-production`.
- Our additional source-controlled gate checks event, owner/repo, protected
  ref, execution context, `CANDIDATE_ROOT`, exact checked-out HEAD and phase.
- Qualification emits `BOUNDARY_ONLY_NOT_RELEASE_AUTHORIZATION`: it does not
  stage or swap code/env, restart ingress or write Atlas data.
- The `post-release` mode is read-only HTTPS observation of both public SNI
  routes; it does not assert deployment identity or native MCP write acceptance.

This workflow cannot be dispatched usefully until an administrator separately
provisions the trusted runner group, runner and branch protection. A GitHub
App read of `branches/main/protection` returned HTTP 403
`Resource not accessible by integration` during preparation, so no branch
protection state is inferred.

## Privileged-environment prerequisites (verify before any dispatch)

1. GitHub organization admin: create/verify `engineering-release-production`
   runner group with **Selected repositories** containing only
   `datarelay-labs/datarelay-atlas` (or the explicitly approved minimal set),
   and **Selected workflows** containing only the SHA-pinned canonical
   `release-contract.yml` trusted workflow. Confirm org-level policy actually
   enforces both restrictions.
2. GitHub repository admin: protect the default `main` branch with review and
   appropriate required checks. Confirm GitHub sets `ref_protected=true`.
   Keep production release workflow changes in review until trust checks pass.
3. Runner operator: register a *dedicated*, isolated self-hosted runner in that
   group with labels `self-hosted`, `engineering-release-production`.
   Confirm its operator-managed `ENGINEERING_RELEASE_CONTEXT=protected-production`
   value and secure ownership/updates. No untrusted PR jobs, generic workspace,
   or ordinary developer processes are allowed on the release runner.
4. Keep the qualification runner **without production administrative secrets,
   privileged host access, deployment tokens, or generic passwordless elevation**.
   Network reachability for its read-only public smoke is enough at this stage.
5. Reconcile group allowlists, protected main, runner isolation, exact source
   provenance and current owner approval with the Engineering System security
   standard. Record evidence in Issue #346. Do not declare these PASS merely
   because a workflow exists.
6. The original application release `9177c976a591143b045ebdeabdbccd0d48b87c6c`
   is frozen. Merging this workflow creates a **new GitHub commit SHA**. Follow
   the exact-HEAD invalidation rules: qualify the new candidate rather than
   presenting old `9177c976` CI/user acceptance as qualification of the new
   merged HEAD.

## Qualification invocation (only after runner setup)

Use the GitHub Actions manual dispatch for
`Atlas Protected Release Boundary Qualification`, from protected `main`,
with `phase=qualify` and the **full current reviewed commit SHA**.
The canonical reusable workflow and local gate must both PASS at that same
candidate HEAD. A green result means the *runner boundary and this limited
qualification contract* passed, not a production cutover or release acceptance.

After a separately authorized and independently verified successful cutover,
`phase=post-release` performs read-only TLS observations of
`https://mcp.atlas.datarelay.run/healthz` and
`https://auth.atlas.datarelay.run/realms/atlas/.well-known/openid-configuration`.
Keycloak issuer and both OAuth endpoints must retain their expected HTTPS
origins. This alone never proves the production code tree matches GitHub.

## Subsequent production transaction: NOT implemented or enabled by this PR

Actual privilege-bearing installation must be a separately reviewed,
runner-administered *bounded operation*. A qualified runner alone does not grant
production filesystem or service control. Do not assume that existing scripts
that failed twice are now safe. The production operator must preauthorize a
new, auditable, narrowly scoped deploy/rollback helper and a single immutable
manifest-bound release candidate, with **no arbitrary shell execution surface**
and no GitHub credential copied to the runtime server.

Its required fail-closed transaction, after independently confirming current
production and tested rollback backups, is:

1. Check immutable complete 421-file tracked manifest; verify release root
   and its parents allow Ingress `DynamicUser=yes` traversal (0755 code root),
   actual sandboxed execution test, candidate flags environment
   `root:atlas 0640`, prod env preflight and read-only upgrade/rollback
   compatibility. Validate preserved backups for existing code, config and
   84-file/7-project data snapshot (restore-test). Refuse unsafe mutations.
2. Quiesce **both** `datarelay-atlas-ingress.socket` and
   `datarelay-atlas-ingress.service` before moving the live code tree.
   Then stop Atlas MCP and Web. Preserve Keycloak and durable data.
3. Switch the manifest-qualified code and guarded candidate env atomically,
   keeping old code/env available for immediate restoration; set release root
   0755 so ingress's transient DynamicUser can execute its interpreter.
4. Start MCP and Web first; prove loopback backend health. Only then
   `reset-failed` the ingress service/socket and start socket plus service.
   Verify all five units, boot enablement, public MCP and public Keycloak
   SNI HTTP200/issuer, and running 421-file manifest hashes.
5. On **any** failed mandatory step, restore old code and old env, restart
   Atlas MCP/Web, explicitly reset/start ingress socket and service, and
   verify both public SNI hostnames. If rollback health fails, remain in
   `INCIDENT/UNRECOVERED` and do not report rollback PASS.
6. After a successful switch, independently use real *connected ChatGPT*
   native MCP tools for bounded memory write/replay/readback/deny and canonical
   source registration/idempotency/provenance/scoped deny. Optional
   `atlas.sources.write` OAuth re-consent is distinct; private GitHub source
   access needs approved least-privilege read credentials. Keep audit
   argument/token-free.

The new privileged helper, host runner administration, native write tests and
production switch are **pending independent qualification**. The existing
operating service and backup directories remain untouched by this proposal.
