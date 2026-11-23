# Atlas README projection and production backup safety (2026-11-24)

## Incident evidence

Before the new Atlas main can be considered for protected production
rollout, the installed Atlas backup command failed closed with
"backup refused because content looks secret". It created no backup tree
and did not modify the live data root.

A **read-only**, local-to-prod examination using the existing
contains_unsafe_secret detector found precisely one matching projected
document among 57 projection files; the 3 durable metadata files and
30 personal snapshots were not matched. The source identity is
datarelay-atlas / readme with Git source revision
3ce2c51cea4053e0d570381e3792516855aeb843.
This is a public README *prose* false positive, not a leaked access token.
The original source revision is still the active public GitHub source
at the time of diagnosis.

## Fix and controls

Edit only the public README wording so the technical OAuth explanation
is accurate without resembling a credential-bearing HTTP Authorization
header. Do **not** weaken the shared secret detector, remove a live
projection or source mapping, bypass the normal sync, or exclude an
otherwise valid Atlas document from a full backup.

Regression: fetch/ingest the actual README through AtlasService,
preserving the noncanonical derived projection and revision metadata.
Run the standard quiesced backup_data_root, verify manifest integrity
and restore_test into separate temporary data root. Original registry
must be unchanged; secret classification must remain enabled.

## Production follow-through (distinct from deployment)

1. Merge the documentation source and verify GitHub source revision.
2. Refresh that registered source through the existing authorized
   operator synchronization path; source update must be atomic and
   owned by the existing atlas service account. Review operator
   runtime identity, timer behavior and source owner before invoking.
3. Independently verify the updated projection SHA/revision and that
   the unchanged original secret scanner no longer detects a
   credential-shaped phrase. Only then retry a *fresh* protected
   backup path, and restore_test at another fresh isolated path.
4. Verify service health and restore source/backup invariants. The
   backup must include every durable and rebuildable entry; no
   selective suppression, deletion, or direct JSON overwrite.
5. Keep any newly created backup/restored state private on the owner
   host. Do not paste source bodies, tokens, secrets, or registry
   contents into GitHub comments or test evidence.

This does **not** qualify exact-main real-user Surface/FUE, OAuth write
permissions or application/refresh-operator cutover. Those remain
separate blocking release gates in ACTIVE Issue #346.
