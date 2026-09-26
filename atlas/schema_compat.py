"""Non-destructive durable-schema upgrade and rollback checks.

Design gate: ADR-0011. These commands do not rewrite the data root and do
not change the Engineering System production profile.

``ops upgrade`` proves the code that is running. ``ops rollback`` proves the
staged rollback target by executing that tree's probe, not this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from atlas.data_lock import data_root_write_lock
from atlas.provenance import ValidationError
from atlas.registry import REGISTRY_SCHEMA_VERSION, ProjectRegistry
from atlas.work_controller import (
    CONTROLLER_SCHEMA_VERSION,
    CompletionEvent,
    WorkControllerStore,
)

_REGISTRY_NAME = "registry.json"
_CONTROLLER_NAME = "work-controller.json"
_EVENT_DIRS = ("completion-inbox", "completion-processed")
_TARGET_PROBE = """
import json
import sys
from pathlib import Path

from atlas.schema_compat import probe_durable_state

try:
    result = probe_durable_state(Path(sys.argv[1]))
except Exception as exc:
    sys.stderr.write(f"error: {exc}\\n")
    raise SystemExit(1)
sys.stdout.write(json.dumps(result))
raise SystemExit(0)
"""


def upgrade_data_root(data_root: Path) -> dict:
    """Succeed only when this running code can read the durable state."""
    return _check(data_root, action="upgrade")


def probe_durable_state(data_root: Path) -> dict:
    """Read-only probe executed by whichever Atlas tree is on ``sys.path``."""
    return _check(data_root, action="rollback")


def rollback_data_root(data_root: Path, target_code: Path) -> dict:
    """Succeed only when the staged target tree can read the data root.

    The allow decision comes from that tree's ``probe_durable_state``. This
    process does not treat its own readers as the rollback target.
    """
    root = Path(data_root)
    target = Path(target_code)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("rollback refused: data root is not a directory")
    marker = target / "atlas" / "schema_compat.py"
    if target.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValidationError("rollback refused: target code is not an Atlas tree")
    before = _durable_bytes(root)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target.resolve())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-P", "-c", _TARGET_PROBE, str(root.resolve())],
            cwd=str(target.resolve()),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("rollback refused: target code did not finish") from exc
    after = _durable_bytes(root)
    if before != after:
        raise ValidationError("rollback refused: target code changed durable state")
    if completed.returncode != 0:
        raise ValidationError(_probe_error(completed.stderr))
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("rollback refused: target code returned an unreadable result") from exc
    if (
        not isinstance(result, dict)
        or result.get("status") != "ok"
        or result.get("rewritten") is not False
    ):
        raise ValidationError("rollback refused: target code returned an unreadable result")
    return result


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
        _validate_completion_events(root, action)
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


def _validate_completion_events(root: Path, action: str) -> None:
    for dirname in _EVENT_DIRS:
        directory = root / dirname
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ValidationError(f"{action} refused: {dirname} is not a directory")
        _validate_event_tree(directory, action)


def _validate_event_tree(directory: Path, action: str) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise ValidationError(f"{action} refused: completion event is unsupported")
        if entry.is_dir():
            _validate_event_tree(entry, action)
            continue
        if entry.suffix != ".json":
            raise ValidationError(f"{action} refused: completion event is unsupported")
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValidationError("completion event is unsupported")
            CompletionEvent.from_dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"{action} refused: completion event is unsupported") from exc


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
    for dirname in _EVENT_DIRS:
        directory = root / dirname
        if directory.is_symlink() or not directory.is_dir():
            continue
        _snapshot_tree(directory, root, found)
    return found


def _snapshot_tree(directory: Path, root: Path, found: dict[str, bytes]) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            _snapshot_tree(entry, root, found)
        elif entry.is_file():
            found[str(entry.relative_to(root))] = entry.read_bytes()


def _probe_error(stderr: str) -> str:
    line = ""
    for item in stderr.splitlines():
        if item.strip():
            line = item.strip()
    if line.startswith("error: "):
        line = line[len("error: ") :]
    if line.startswith("rollback refused:"):
        return line
    if line:
        return f"rollback refused: {line}"
    return "rollback refused: target code cannot read the data root"
