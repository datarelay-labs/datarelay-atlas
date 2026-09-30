# Atlas Lifecycle Evidence Contract

Atlas may display CI, test, release, and browser-execution evidence from a local bounded projection. This file is display evidence only; it never becomes CI, release, or GitHub authority.

## File

Runtime data root: `lifecycle-evidence.json`.

The JSON object is exact-schema and content-free:

- `schema_version`: integer `1`
- `kind`: `atlas_lifecycle_evidence`
- `observed_at`: non-empty observation timestamp string
- `repository`: exact registered repository identity
- `candidate_head`: full 40-character lowercase Git commit
- `channels`: zero or more of `ci`, `tests`, `release`, `browser`

Each present channel contains exactly:

- `outcome`: `PASS`, `FAIL`, or `BLOCKED`
- `detail`: bounded content-free summary, maximum 256 characters

The whole artifact is capped at 32 KiB and passes the existing Atlas content-free/secret guard.

## Truth rules

1. Channels are independent. CI PASS does not imply tests, release, or browser PASS.
2. Missing channel evidence is UNKNOWN.
3. Malformed, secret-bearing, wrong-repository, or unsupported evidence is UNAVAILABLE.
4. If canonical Work Packet evidence provides an exact HEAD and `candidate_head` differs, every present channel is STALE even when its recorded outcome says PASS.
5. GitHub Work/PR state comes from the separately validated `github-lifecycle.json`; this artifact cannot override it.
6. Browser PASS is displayable only when an authorized executor has produced evidence under the repository's Surface Reconciliation / Full User E2E contracts. The UI does not create that evidence.
7. Release PASS is never inferred from configuration, PR merge, CI, or browser contract presence.
