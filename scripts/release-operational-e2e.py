"""Release-contract wrapper for a repeatable prod operational E2E.

The qualification harness rejects an existing backup or restore destination.
Each invocation derives a new unused path under the configured parent and
leaves the operator-configured path untouched. Missing inputs still fail
closed before the harness starts.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

_DESTINATIONS = ("ATLAS_BACKUP_DEST", "ATLAS_RESTORE_PROOF_DEST")


def fresh_destinations(environ: dict[str, str]) -> tuple[dict[str, str], str | None]:
    updated = dict(environ)
    allocated: list[Path] = []
    for name in _DESTINATIONS:
        raw = (updated.get(name) or "").strip()
        if not raw:
            return updated, f"{name} is absent"
        configured = Path(raw)
        parent = configured.parent
        if str(parent) in {"", "."} or parent.is_symlink() or not parent.is_dir():
            return updated, "backup destination parent is missing"
        fresh = parent / f"{configured.name}-{secrets.token_hex(8)}"
        partial = Path(str(fresh) + ".partial")
        if (
            fresh in allocated
            or fresh.exists()
            or fresh.is_symlink()
            or partial.exists()
            or partial.is_symlink()
        ):
            return updated, "fresh destination already exists"
        allocated.append(fresh)
        updated[name] = str(fresh)
    return updated, None


def main() -> int:
    updated, reason = fresh_destinations(dict(os.environ))
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    os.environ.update(updated)
    script = Path(__file__).resolve().parent / "prod-qualification.py"
    os.execv(
        sys.executable,
        [sys.executable, "-P", str(script), "operational-e2e", "--mode", "prod"],
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
