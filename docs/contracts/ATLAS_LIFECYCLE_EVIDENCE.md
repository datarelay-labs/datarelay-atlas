# Atlas Lifecycle Evidence Contract

Atlas may display CI, test, release, and human-equivalent browser-gate evidence from a local bounded projection. This file is display evidence only; it never becomes CI, release, browser, or GitHub authority.

## File

Runtime data root: `lifecycle-evidence.json`. The published schema is `docs/contracts/atlas-lifecycle-evidence.schema.json`; `docs/contracts/fixtures/atlas-lifecycle-evidence.example.json` is non-authoritative example evidence.

Schema version 2 is an exact-schema, content-free object with:

- `schema_version`: integer `2`
- `kind`: `atlas_lifecycle_evidence`
- `observed_at`: UTC timestamp ending in `Z`
- `repository`: exact registered repository identity
- `candidate_head`: full 40-character lowercase Git commit
- `channels`: zero or more of `ci`, `tests`, `release`
- `human_equivalent_user_tests`: zero or more of `surface_reconciliation`, `full_user_e2e`

Each present machine channel contains exactly `outcome`, `detail`, and `evidence_ref`. `outcome` is `PASS`, `FAIL`, or `BLOCKED`; `detail` is a non-empty content-free summary of at most 256 characters; `evidence_ref` is a bounded opaque reference or HTTPS GitHub URL that the UI can expose for evidence navigation.
Each present human-equivalent gate contains exactly:

- `required`: boolean canonical-configuration observation
- `configured`: boolean contract/configuration observation
- `outcome`: `PASS`, `FAIL`, `BLOCKED`, or `null`
- `detail`: bounded execution summary when `outcome` is present, otherwise `null`
- `evidence_ref`: bounded execution evidence reference when `outcome` is present, otherwise `null`

Execution evidence is invalid when `configured=false`. The whole artifact is capped at 32 KiB and passes the existing Atlas content-free/secret guard.

## Truth rules

1. CI, tests, release, Surface Reconciliation, and Full User E2E are independent.
2. Missing machine-channel evidence is UNKNOWN.
3. Missing human-equivalent gate configuration/execution evidence is UNKNOWN; Atlas must not assume a browser gate is configured merely because a contract file exists in Atlas itself.
4. Malformed, secret-bearing, wrong-repository, unsupported, or internally contradictory evidence is UNAVAILABLE.
5. Positive current-candidate evidence requires binding to at least one canonical Work Packet HEAD from the validated `github-lifecycle.json` snapshot.
6. If no canonical Work Packet HEAD is available, present candidate evidence remains UNKNOWN rather than becoming current by inference.
7. If `candidate_head` differs from every canonical Work Packet HEAD, machine evidence is STALE and human-equivalent gate evidence is STALE_DIFFERENT_HEAD.
8. A required/configured human-equivalent gate without execution evidence is CONTRACT_CONFIGURED, not PASS.
9. Human-equivalent execution is represented explicitly as EXECUTION_PASS, EXECUTION_FAIL, or EXECUTION_BLOCKED.
10. Surface Reconciliation and Full User E2E never collapse into one generic browser PASS.
11. GitHub Work/PR state comes from the separately validated `github-lifecycle.json`; this artifact cannot override it.
12. Release PASS is never inferred from configuration, PR merge, CI, or browser contract presence.
13. Browser PASS is displayable only from owner-authorized actual-browser execution evidence under the Surface Reconciliation / Full User E2E contracts.
14. When multiple canonical Work Packets exist for one repository, Atlas displays all of them and accepts exact-candidate evidence only when its candidate HEAD matches one of their canonical HEADs.
