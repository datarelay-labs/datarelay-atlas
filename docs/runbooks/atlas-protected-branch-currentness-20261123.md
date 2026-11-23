# Protected-branch Work Packet currentness — 2026-11-23

Scope: Atlas Core task/lifecycle derived evidence and live GitHub
reconciliation. Read-only; no Work Packet/GitHub/production/OAuth writes.

## Reproduced issue

ACTIVE Issue #346 uses BRANCH=main and an older owner-qualified
LAST_VERIFIED_HEAD=9fe563252b184a65381922eccc3e5787dffdeef3.
Current GitHub main is f74f9a15ae84b75dbeda8af9817939820cba4ddc.
Previous code correlated unrelated historic PR #349 solely by source
branch name main, yielding PR_HEAD_MISMATCH. Accepting the old verified
HEAD as the *current* main would be equally misleading.

## Currentness rules

- For trusted OPEN ACTIVE, PAUSED or BLOCKED main/master packets, query
  the actual GitHub git/ref/heads/<branch> once per protected branch.
- Exact branch SHA match is required for canonical currentness.
  Otherwise record BRANCH_HEAD_MISMATCH.
- Missing, malformed, failed or denied GitHub ref lookup yields
  BRANCH_HEAD_UNVERIFIED, never guessed execution or release authority.
- Do not associate old PRs by protected branch name alone when the
  packet's HEAD differs; exact-head PR links retain state checks.
- Feature branch PR head mismatches and closed COMPLETE history retain
  their earlier validation semantics.
- The existing lifecycle/MCP/Web UI warning reports noncanonical open
  work without replacing it with old COMPLETE work or importing its HEAD
  as currently authoritative.
- GitHub data is read-only. No user/customer payload, credential, key,
  private repository content or production state is written.

## Verification and remaining gates

New regressions cover the incorrect historical PR association,
fresh protected SHA, stale SHA, unavailable/denied SHA, and strict
feature branch PR validation. Run existing task context and Web UI tests.

Issue #346 must stay ACTIVE until complete same-head actual MCP user
Surface Reconciliation, Full User E2E, backup/restore/rollback and
authorized native ChatGPT MCP writes. Updating the source alone cannot
replace the currently installed refresh operator or production Atlas.
Never overwrite the previously qualified issue HEAD merely to make the
derived status green.
