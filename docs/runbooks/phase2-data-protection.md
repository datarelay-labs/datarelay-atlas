# Data-root backup and restore verification

Snapshot Atlas durable registry state and rebuildable projections without
copying a live directory over in-flight writes. This runbook does not upgrade,
roll back, deploy `prod-atlas`, or replace the data root in place.

The commands below are the consistent backup path from ADR-0010. The
`backup_command` directory copy in `.engineering/project.yaml` is not that
path.

## Safety

- Stop before running either command against a data root you cannot identify.
- `--dest` must be a new path outside the source tree. An existing destination
  is refused.
- `restore-test` does not modify the source data root. It restores into
  `--dest` and checks that the copy loads.
- Service env files, TLS keys, and tokens are not backup inputs. Content
  classified as a live secret fails the backup.
- Unexpected files at the data-root top level fail the backup. Remove or
  relocate them before retrying. Do not delete `registry.json` to force a
  passing run.

## Backup

```bash
export PYTHONPATH=.
python3 -m atlas ops backup \
  --data-root /var/lib/datarelay-atlas \
  --dest /var/backups/datarelay-atlas/snapshot
```

Expected result: exit 0 and JSON `status` of `ok`, with `file_count` and
`project_count`. The destination contains `manifest.json` and whichever of
these exist in the data root:

- `registry.json` — durable project configuration
- `projections/` — rebuildable derived documents
- `work-controller.json`, `completion-inbox/`, and `completion-processed/` —
  Atlas-owned controller state

`chat-audit.json`, `chat-audit.lock`, and `chat-audit-handoffs/` are derived
cache. They are left in place and omitted from the snapshot. Any other
top-level entry fails the backup.

A cooperating registry or projection write waits until the snapshot reads
finish. A process that writes those files without the Atlas lock is outside
this guarantee.

## Restore verification

```bash
export PYTHONPATH=.
python3 -m atlas ops restore-test \
  --backup /var/backups/datarelay-atlas/snapshot \
  --dest /tmp/datarelay-atlas-restore-proof
```

Expected result: exit 0 and JSON `status` of `ok`. The proof directory loads
as a data root. Corrupt JSON, a digest mismatch, a missing or extra file, an
unsupported registry schema, and secret-like content exit non-zero and leave
no successful destination.

After a real registry restore, run `rebuild` or `sync` before treating
projections as current. This runbook does not perform that restore over the
live data root.

## Upgrade and rollback compatibility

These commands do not rewrite the data root and do not switch the installed
build. They only prove whether this code can read `registry.json` and
`work-controller.json`.

```bash
export PYTHONPATH=.
python3 -m atlas ops upgrade --data-root /var/lib/datarelay-atlas
python3 -m atlas ops rollback --data-root /var/lib/datarelay-atlas
```

Expected result: exit 0 and JSON `"rewritten": false` when both files are
absent or already at the schema this code reads. A newer or unsupported
schema exits non-zero and leaves the files unchanged. Do not start the older
build after a refused rollback.
