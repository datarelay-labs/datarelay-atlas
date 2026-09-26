"""Non-destructive durable-schema upgrade and rollback checks.

Design gate: ADR-0011. These commands do not rewrite the data root and do
not change the Engineering System production profile.
"""

from __future__ import annotations

import json
from pathlib import Path

from atlas.data_lock import data_root_write_lock
from atlas.provenance import ValidationError
from atlas.registry import REGISTRY_SCHEMA_VERSION, ProjectRegistry
from atlas.work_controller import CONTROLLER_SCHEMA_VERSION, WorkControllerStore

_REGISTRY_NAME = "registry.json"
_CONTROLLER_NAME = "work-controller.json"


def upgrade_data_root(data_root: Path) -> dict:
    """Succeed only when this code can read the current durable schemas."""
    return _check(data_root, action="upgrade")


def rollback_data_root(data_root: Path) -> dict:
    """Succeed only when this code can still read the current durable schemas.

    A newer schema fails closed so an older binary is not treated as compatible.
    """
    return _check(data_root, action="rollback")


def _check(data_root: Path, *, action: str) -> dict:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"{action} refused: data root is not a directory")
    with data_root_write_lock(root):
        before = _durable_bytes(root)
        registry_schema = _schema_of(root / _REGISTRY_NAME, action, "registry")
        controller_schema = _schema_of(root / _CONTROLLER_NAME, action, "work-controller")
        _require_supported(
            registry_schema,
            REGISTRY_SCHEMA_VERSION,
            action,
            "registry",
        )
        _require_supported(
            controller_schema,
            CONTROLLER_SCHEMA_VERSION,
            action,
            "work-controller",
        )
        _prove_readable(root, action, registry_schema is not None, controller_schema is not None)
        after = _durable_bytes(root)
    if before != after:
        raise ValidationError(f"{action} refused: durable state changed during the check")
    return {
        "controller_schema": controller_schema,
        "registry_schema": registry_schema,
        "rewritten": False,
        "status": "ok",
    }


def _schema_of(path: Path, action: str, name: str) -> int | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{action} refused: {name} is not a regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{action} refused: {name} is corrupt") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{action} refused: {name} is corrupt")
    version = data.get("schema_version")
    if type(version) is not int:
        raise ValidationError(f"{action} refused: {name} schema is unsupported")
    return version


def _require_supported(version: int | None, supported: int, action: str, name: str) -> None:
    if version is None or version == supported:
        return
    if action == "rollback" and version > supported:
        raise ValidationError(
            f"rollback refused: {name} schema is newer than this code can read"
        )
    raise ValidationError(f"{action} refused: {name} schema is unsupported")


def _prove_readable(
    root: Path,
    action: str,
    registry_present: bool,
    controller_present: bool,
) -> None:
    if registry_present:
        try:
            ProjectRegistry(root).list_projects()
        except (KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"{action} refused: registry is unsupported") from exc
    if controller_present:
        try:
            WorkControllerStore(root).list_workstreams()
        except (KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"{action} refused: work-controller is unsupported") from exc


def _durable_bytes(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for name in (_REGISTRY_NAME, _CONTROLLER_NAME):
        path = root / name
        if path.is_file() and not path.is_symlink():
            found[name] = path.read_bytes()
    return found
