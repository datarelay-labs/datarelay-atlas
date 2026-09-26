# ADR-0010: Quiesced data-root backup and restore verification

Status: Accepted
Date: 2026-09-26

## Context

ADR-0005 stores Atlas-owned durable state in `<data-root>/registry.json` and
rebuildable projections under `<data-root>/projections/`. Those writers use
direct file writes. ADR-0009 left backup, restore, upgrade, and rollback to
Issue #43 and did not treat a live directory copy as a consistent snapshot.

Issue #43's first slice is backup/restore consistency and validation. Upgrade,
rollback, and the production Engineering System profile stay later slices.

## Minimal design gate

1. **Goal** — Snapshot `registry.json` and `projections/` at a writer-quiesced
   boundary, and prove restore by rejecting corrupt, partial, secret, and
   unsupported backups.
2. **Non-goals** — Online consistency of a live `cp -a` while a non-cooperating
   process writes the data root; upgrade and rollback commands; flipping
   `production_oriented` or replacing `.engineering/project.yaml`
   `backup_command`; in-place restore over a live data root; production
   mutation; backing up service env files, TLS keys, or tokens.
3. **Affected public contract** — `python -m atlas ops backup --data-root
   --dest` and `python -m atlas ops restore-test --backup --dest`. Backup
   layout is a directory with `manifest.json` (`backup_schema_version` 1) plus
   the snapshotted files. Registry `schema_version` stays 1.
4. **State / migration impact** — No registry or projection schema bump.
   Cooperating writers take an exclusive lock on `<data-root>/.write.lock` and
   publish file bytes with rename. The lock file is operational and is not
   part of the backup. Unsupported registry schemas are rejected, not migrated.
5. **Security / operations impact** — Backup and restore-test copy only
   `registry.json` and regular files under `projections/`. Symlinks, unexpected
   top-level entries, leftover `.tmp` writes, and content classified as a live
   secret are refused. Commands print a count and destination, not file
   bodies. `restore-test` writes only to a new directory and does not modify
   the source data root.
6. **Architecture boundary** — `atlas.data_protection` owns the lock, snapshot,
   and restore verification. `ProjectRegistry` and `ProjectionStore` take the
   same lock around their writes. `atlas.ops` still owns service readiness and
   does not copy the data root. Canonical GitHub state stays outside the
   backup. Projections remain rebuildable; registry remains the durable
   configuration the restore proof loads.
7. **Acceptance / regression criteria** — A backup taken while a cooperating
   writer is blocked matches a complete pre-write tree. Restore-test loads
   that registry and projection bytes, and fails closed on digest mismatch,
   missing or extra files, corrupt JSON, unsupported schema, and secret-like
   content. The production operations profile stays non-production.

## Decision

1. **Quiesce cooperating writers.** `data_root_write_lock` holds an exclusive
   `flock` on `<data-root>/.write.lock` for the process-critical section.
   Registry mutations and projection document plus metadata mutations take
   that lock. When the projection store directory is named `projections`, its
   lock root is the parent data root so registry and projection writers share
   one lock.
2. **Snapshot under that lock.** `ops backup` reads the allowed tree only
   while the lock is held, then publishes a sibling directory by rename.
   Consistency applies to Atlas writers that use the lock. A process that
   writes the same files without the lock can still race; this ADR does not
   call that case an online-consistent backup.
3. **Validate before publish and again on restore-test.** The snapshot must
   contain a supported registry object when `registry.json` is present, a
   projections object when `projections/projections.json` is present, and
   matching projection document digests. `ops restore-test` checks the
   manifest digest set, rejects partial or unexpected files, restores into an
   empty directory, and loads the registry.
4. **Point the operations profile at this snapshot** once upgrade and rollback
   exist. `.engineering/project.yaml` `backup_command` is `ops backup` with a
   required `ATLAS_BACKUP_DEST`. It is not a live directory copy.

## Consequences

- Operators can prove a restorable snapshot without stopping on a torn JSON
  write from `ProjectRegistry` or `ProjectionStore`.
- Secrets that the shared classifier flags never enter the backup directory.
- The production-profile slice sets `production_oriented: true` and points
  backup, restore-test, upgrade, and rollback at these commands. Public smoke
  and operational E2E stay disabled until `prod-atlas` evidence exists.

## Amendment — full data-root contract

Audited HEAD `c5a8cef` rejected a normal root that also contained ADR-0006
controller state. This amendment is part of the same backup/restore slice.

1. **Included Atlas-owned state** is `registry.json`, `projections/`,
   `work-controller.json`, `completion-inbox/`, and `completion-processed/`.
   Controller and inbox bytes use manifest role `controller`. Registry remains
   durable configuration. Projections remain rebuildable. Completion events
   are included because drain idempotency and restart depend on them.
2. **The same inter-process lock** covers controller publication, inbox
   enqueue, the inbox directory listing, and the move into
   `completion-processed/`. Audit and Cursor dispatch stay outside the lock
   so a spawned session cannot deadlock on it. A snapshot can therefore show
   a controller record whose event file is still in the inbox; that is the
   crash-consistent state drain already restarts from, and a later drain
   treats an already processed event id as a replay.
3. **Derived Chat Audit cache is not backed up.** `chat-audit.json`,
   `chat-audit.lock`, `chat-audit.tmp`, and `chat-audit-handoffs/` may sit in
   the data root without failing the backup and without being copied.
   ADR-0007 keeps that cache non-canonical. Any other top-level entry still
   fails closed.
4. **Restore-test** loads both the registry and, when present, the controller
   workstream list. It still writes only to a new directory.
5. **Lock open does not follow symlinks.** `.write.lock` is opened with
   `O_NOFOLLOW` and accepted only when the descriptor is a regular file, so
   a symlink cannot be chmod'd before validation.
6. **Backup success is published durably.** File bytes, the completed
   directory tree, and the destination parent are fsynced before `status: ok`.

## Amendment — production operations profile

The profile slice points `.engineering/project.yaml` at `ops backup`,
`ops restore-test`, `ops upgrade`, and `ops rollback --target-code`.
Destination and rollback-target variables are required and have no default.
`incident_response_required` is true because the pinned adoption check
requires it whenever `production_oriented` is true. Public smoke and
operational E2E remain disabled until real `prod-atlas` evidence exists.
