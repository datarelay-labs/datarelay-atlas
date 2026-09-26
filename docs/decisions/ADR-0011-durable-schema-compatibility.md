# ADR-0011: Durable schema upgrade and rollback boundary

Status: Accepted
Date: 2026-09-26

## Context

ADR-0005 and ADR-0006 store Atlas-owned state at schema version 1 in
`registry.json` and `work-controller.json`. Readers already reject any other
`schema_version`. Issue #43 now needs that boundary as executable upgrade and
rollback commands. The production Engineering System profile stays unchanged
until those commands have passed an independent audit.

## Minimal design gate

1. **Goal** — Prove this code can read the data root's durable schemas, and
   refuse upgrade or rollback when it cannot, without rewriting the data root.
2. **Non-goals** — Migrating an older schema forward; downgrading a newer
   schema; flipping `production_oriented` or replacing `upgrade_command` /
   `rollback_command` in `.engineering/project.yaml`; treating projections or
   chat-audit cache as durable schema; production mutation.
3. **Affected public contract** — `python -m atlas ops upgrade --data-root`
   and `python -m atlas ops rollback --data-root`. Both print a secret-free
   JSON result and exit non-zero on refusal. Registry and controller schema
   versions stay 1.
4. **State / migration impact** — No schema bump and no rewrite. Success means
   the on-disk schema is exactly the schema this code reads. A newer or
   otherwise unsupported schema is left untouched.
5. **Security / operations impact** — Commands read under the existing
   data-root lock, do not follow a symlink data root, and do not print file
   bodies. Rollback does not restore an older code version; it only allows
   the compatibility claim when this code can read the current schema.
6. **Architecture boundary** — `atlas.schema_compat` owns the check.
   `ProjectRegistry` and `WorkControllerStore` remain the readers. Backup and
   restore-test stay as ADR-0010 defined them.
7. **Acceptance / regression criteria** — Schema 1 upgrade and rollback
   succeed, leave the files unchanged, and the registry still loads. A newer
   schema fails both commands with the files unchanged.

## Decision

1. **Supported durable schemas** are registry `1` and work-controller `1`.
   A missing file is compatible. Projections and chat-audit cache are not
   part of this decision.
2. **`ops upgrade`** succeeds only when this running code can read every
   present durable file. That includes `registry.json`, `work-controller.json`,
   and completion events in `completion-inbox/` and `completion-processed/`
   through `CompletionEvent`. A missing file or directory is compatible.
   Unsupported, corrupt, or newer schemas fail closed with no rewrite.
3. **`ops rollback`** does not use the running binary as the compatibility
   proof. The operator passes `--target-code` pointing at the staged rollback
   checkout. The command runs that tree's `probe_durable_state` in a separate
   interpreter and allows the rollback only when that target exits successfully.
   A newer schema fails closed inside the target. Neither process rewrites the
   data root.

## Consequences

- Operators can refuse a schema mismatch before restarting on another build.
- No migration exists yet. A future schema bump needs an explicit upgrade
  path before `ops upgrade` can accept the older version.
- `.engineering/project.yaml` upgrade and rollback commands stay empty.
