"""Quiesced backup and restore verification for the Phase 1 data root.

Design gate: ADR-0010. Upgrade, rollback, and the production operations
profile are later Issue #43 slices. This module does not copy service
environment files or mutate the source data root during restore-test.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from atlas.data_lock import LOCK_NAME, data_root_write_lock
from atlas.provenance import ValidationError
from atlas.registry import REGISTRY_SCHEMA_VERSION, ProjectRegistry
from atlas.secrets import contains_unsafe_secret
from atlas.work_controller import (
    COMPLETION_INBOX_DIRNAME,
    COMPLETION_PROCESSED_DIRNAME,
    CONTROLLER_SCHEMA_VERSION,
    CompletionEvent,
    WorkControllerStore,
)

BACKUP_SCHEMA_VERSION = 1
PARTIAL_SUFFIX = ".partial"
_DURABLE_NAME = "registry.json"
_PROJECTIONS_DIR = "projections"
_CONTROLLER_NAME = "work-controller.json"
_DERIVED_CACHE_FILES = frozenset({"chat-audit.json", "chat-audit.lock", "chat-audit.tmp"})
_DERIVED_CACHE_DIRS = frozenset({"chat-audit-handoffs"})
_CONTROLLER_DIRS = frozenset({COMPLETION_INBOX_DIRNAME, COMPLETION_PROCESSED_DIRNAME})

@dataclass(frozen=True)
class _SnapshotFile:
    path: str
    role: str
    data: bytes


def backup_data_root(data_root: Path, dest: Path) -> dict:
    """Snapshot registry and projections while cooperating writers are excluded."""
    root = Path(data_root)
    target = Path(dest)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("data root is not a directory")
    if not target.parent.is_dir():
        raise ValidationError("backup destination parent is missing")
    if target.exists() or target.is_symlink():
        raise ValidationError("backup destination already exists")
    partial = Path(str(target) + PARTIAL_SUFFIX)
    if partial.exists() or partial.is_symlink():
        raise ValidationError("backup partial already exists")
    if _overlaps(target, root) or _overlaps(partial, root):
        raise ValidationError("backup destination must be outside the data root")

    with data_root_write_lock(root):
        files = _collect_snapshot(root)
        manifest = _manifest_bytes(files)
        project_count = _project_count(files)

    partial.mkdir(mode=0o700)
    os.chmod(partial, 0o700)
    try:
        for item in files:
            _write_snapshot_file(partial, item)
        _write_bytes(partial / "manifest.json", manifest)
        _assert_backup_tree(partial)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    _publish_tree(partial, target)
    return {
        "dest": str(target),
        "file_count": len(files),
        "project_count": project_count,
        "status": "ok",
    }


def restore_test(backup: Path, dest: Path) -> dict:
    """Validate ``backup`` and restore it into a new directory.

    The source data root is not an argument and is not modified.
    """
    source = Path(backup)
    target = Path(dest)
    if source.is_symlink() or not source.is_dir():
        raise ValidationError("backup is not a directory")
    if not target.parent.is_dir():
        raise ValidationError("restore destination parent is missing")
    if target.exists() or target.is_symlink():
        raise ValidationError("restore destination already exists")
    partial = Path(str(target) + PARTIAL_SUFFIX)
    if partial.exists() or partial.is_symlink():
        raise ValidationError("restore partial already exists")
    if _overlaps(target, source) or _overlaps(partial, source):
        raise ValidationError("restore destination must be outside the backup")

    files = _assert_backup_tree(source)
    partial.mkdir(mode=0o700)
    os.chmod(partial, 0o700)
    try:
        for item in files:
            _write_snapshot_file(partial, item)
        _write_bytes(partial / "manifest.json", (source / "manifest.json").read_bytes())
        checked = _assert_backup_tree(partial)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    _publish_tree(partial, target)
    return {
        "dest": str(target),
        "file_count": len(checked),
        "project_count": _project_count(checked),
        "status": "ok",
    }


def _collect_snapshot(root: Path) -> list[_SnapshotFile]:
    unexpected: list[str] = []
    saw_registry = False
    saw_projections = False
    saw_controller = False
    controller_dirs: list[str] = []
    for entry in root.iterdir():
        name = entry.name
        if name == LOCK_NAME:
            if entry.is_symlink():
                raise ValidationError("backup entry is not a regular file")
            continue
        if name in _DERIVED_CACHE_FILES:
            if entry.is_symlink() or not entry.is_file():
                raise ValidationError("backup entry is not a regular file")
            continue
        if name in _DERIVED_CACHE_DIRS:
            if entry.is_symlink() or not entry.is_dir():
                raise ValidationError("backup entry is not a regular file")
            continue
        if name.endswith(".tmp"):
            raise ValidationError("incomplete write marker")
        if name == _DURABLE_NAME:
            saw_registry = True
            if entry.is_symlink() or not entry.is_file():
                raise ValidationError("backup entry is not a regular file")
            continue
        if name == _PROJECTIONS_DIR:
            saw_projections = True
            if entry.is_symlink() or not entry.is_dir():
                raise ValidationError("backup entry is not a regular file")
            continue
        if name == _CONTROLLER_NAME:
            saw_controller = True
            if entry.is_symlink() or not entry.is_file():
                raise ValidationError("backup entry is not a regular file")
            continue
        if name in _CONTROLLER_DIRS:
            if entry.is_symlink() or not entry.is_dir():
                raise ValidationError("backup entry is not a regular file")
            controller_dirs.append(name)
            continue
        unexpected.append(name)
    if unexpected:
        raise ValidationError("data root contains unexpected entries")

    files: list[_SnapshotFile] = []
    if saw_registry:
        files.append(
            _SnapshotFile(
                _DURABLE_NAME,
                "durable",
                _read_regular(root / _DURABLE_NAME),
            )
        )
    if saw_projections:
        for path in _tree_files(root / _PROJECTIONS_DIR):
            relative = path.relative_to(root).as_posix()
            files.append(_SnapshotFile(relative, "rebuildable", _read_regular(path)))
    if saw_controller:
        files.append(
            _SnapshotFile(
                _CONTROLLER_NAME,
                "controller",
                _read_regular(root / _CONTROLLER_NAME),
            )
        )
    for dirname in sorted(controller_dirs):
        for path in _tree_files(root / dirname):
            relative = path.relative_to(root).as_posix()
            files.append(_SnapshotFile(relative, "controller", _read_regular(path)))
    files.sort(key=lambda item: item.path)
    _validate_snapshot_files(files)
    return files


def _tree_files(directory: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise ValidationError("backup entry is not a regular file")
        for dirname in dirnames:
            child = current / dirname
            if child.is_symlink():
                raise ValidationError("backup entry is not a regular file")
        for name in filenames:
            path = current / name
            if name.endswith(".tmp") or name == LOCK_NAME:
                raise ValidationError("incomplete write marker")
            if path.is_symlink() or not path.is_file():
                raise ValidationError("backup entry is not a regular file")
            found.append(path)
    return found


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError("backup entry is not a regular file")
    if not path.is_file():
        raise ValidationError("backup file is missing")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValidationError("backup entry is unreadable") from exc


def _validate_snapshot_files(files: list[_SnapshotFile]) -> None:
    blobs = {item.path: item.data for item in files}
    if len(blobs) != len(files):
        raise ValidationError("backup manifest is unsupported")
    for item in files:
        if not _role_matches(item.path, item.role):
            raise ValidationError("backup manifest is unsupported")
        _reject_secret(item.data)
    _validate_registry_blob(blobs.get(_DURABLE_NAME))
    _validate_projection_blobs(blobs)
    _validate_controller_blobs(blobs)


def _validate_registry_blob(raw: bytes | None) -> None:
    if raw is None:
        return
    data = _json_object(raw, "registry.json is corrupt")
    version = data.get("schema_version")
    if type(version) is not int or version != REGISTRY_SCHEMA_VERSION:
        raise ValidationError("registry schema is unsupported")
    if not isinstance(data.get("projects"), dict):
        raise ValidationError("registry.json is unsupported")


def _validate_projection_blobs(blobs: dict[str, bytes]) -> None:
    raw = blobs.get(f"{_PROJECTIONS_DIR}/projections.json")
    if raw is None:
        if any(path.startswith(f"{_PROJECTIONS_DIR}/") for path in blobs):
            raise ValidationError("projections.json is unsupported")
        return
    data = _json_object(raw, "projections.json is corrupt")
    records = data.get("projections")
    if not isinstance(records, dict):
        raise ValidationError("projections.json is unsupported")
    for meta in records.values():
        if not isinstance(meta, dict):
            raise ValidationError("projections.json is unsupported")
        rel = meta.get("projection_path") or ""
        digest = meta.get("content_digest") or ""
        if not rel and not digest:
            continue
        stored = _projection_storage_path(rel)
        blob = blobs.get(stored)
        if not isinstance(digest, str) or not digest or blob is None:
            raise ValidationError("projection record is partial")
        if hashlib.sha256(blob).hexdigest() != digest:
            raise ValidationError("projection document digest mismatch")


def _validate_controller_blobs(blobs: dict[str, bytes]) -> None:
    raw = blobs.get(_CONTROLLER_NAME)
    if raw is not None:
        data = _json_object(raw, "work-controller.json is corrupt")
        version = data.get("schema_version")
        if type(version) is not int or version != CONTROLLER_SCHEMA_VERSION:
            raise ValidationError("work-controller schema is unsupported")
        if not isinstance(data.get("workstreams"), dict):
            raise ValidationError("work-controller.json is unsupported")
    for path, blob in blobs.items():
        if not (
            path.startswith(f"{COMPLETION_INBOX_DIRNAME}/")
            or path.startswith(f"{COMPLETION_PROCESSED_DIRNAME}/")
        ):
            continue
        if not path.endswith(".json"):
            raise ValidationError("completion event is unsupported")
        event = _json_object(blob, "completion event is corrupt")
        try:
            CompletionEvent.from_dict(event)
        except ValidationError as exc:
            raise ValidationError("completion event is unsupported") from exc


def _role_matches(path: str, role: str) -> bool:
    if role == "durable":
        return path == _DURABLE_NAME
    if role == "rebuildable":
        return path.startswith(f"{_PROJECTIONS_DIR}/")
    if role == "controller":
        return path == _CONTROLLER_NAME or path.startswith(
            f"{COMPLETION_INBOX_DIRNAME}/"
        ) or path.startswith(f"{COMPLETION_PROCESSED_DIRNAME}/")
    return False


def _projection_storage_path(rel: str) -> str:
    path = Path(rel)
    if not isinstance(rel, str) or not rel or path.is_absolute() or ".." in path.parts:
        raise ValidationError("projection record is partial")
    return f"{_PROJECTIONS_DIR}/{path.as_posix()}"


def _reject_secret(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("backup entry is corrupt") from exc
    if contains_unsafe_secret(text):
        raise ValidationError("backup refused because content looks secret")


def _json_object(raw: bytes, corrupt_message: str) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(corrupt_message) from exc
    if not isinstance(data, dict):
        raise ValidationError(corrupt_message)
    return data


def _manifest_bytes(files: list[_SnapshotFile]) -> bytes:
    payload = {
        "backup_schema_version": BACKUP_SCHEMA_VERSION,
        "files": [
            {
                "bytes": len(item.data),
                "path": item.path,
                "role": item.role,
                "sha256": hashlib.sha256(item.data).hexdigest(),
            }
            for item in files
        ],
        "registry_schema_version": REGISTRY_SCHEMA_VERSION,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _assert_backup_tree(backup: Path) -> list[_SnapshotFile]:
    manifest_path = backup / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValidationError("backup manifest is corrupt")
    manifest = _json_object(_read_regular(manifest_path), "backup manifest is corrupt")
    if (
        type(manifest.get("backup_schema_version")) is not int
        or manifest.get("backup_schema_version") != BACKUP_SCHEMA_VERSION
        or type(manifest.get("registry_schema_version")) is not int
        or manifest.get("registry_schema_version") != REGISTRY_SCHEMA_VERSION
    ):
        raise ValidationError("backup manifest is unsupported")
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise ValidationError("backup manifest is unsupported")

    files: list[_SnapshotFile] = []
    seen: set[str] = set()
    for entry in listed:
        if not isinstance(entry, dict):
            raise ValidationError("backup manifest is unsupported")
        rel = entry.get("path")
        role = entry.get("role")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if not isinstance(rel, str) or rel in seen or rel == "manifest.json":
            raise ValidationError("backup manifest is unsupported")
        if role not in {"durable", "rebuildable", "controller"} or not isinstance(digest, str):
            raise ValidationError("backup manifest is unsupported")
        if type(size) is not int or size < 0:
            raise ValidationError("backup manifest is unsupported")
        child = _safe_member(backup, rel)
        blob = _read_regular(child)
        if len(blob) != size or hashlib.sha256(blob).hexdigest() != digest:
            raise ValidationError("backup file digest mismatch")
        if not _role_matches(rel, role):
            raise ValidationError("backup manifest is unsupported")
        seen.add(rel)
        files.append(_SnapshotFile(rel, role, blob))
    files.sort(key=lambda item: item.path)
    on_disk = _members_on_disk(backup)
    if on_disk != seen:
        missing = seen - on_disk
        extra = on_disk - seen
        if missing:
            raise ValidationError("backup file is missing")
        if extra:
            raise ValidationError("backup contains unexpected files")
    _validate_snapshot_files(files)
    _load_projects(backup)
    _load_controller(backup)
    return files


def _members_on_disk(backup: Path) -> set[str]:
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(backup, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise ValidationError("backup entry is not a regular file")
        for dirname in dirnames:
            if (current / dirname).is_symlink():
                raise ValidationError("backup entry is not a regular file")
        for name in filenames:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ValidationError("backup entry is not a regular file")
            relative = path.relative_to(backup).as_posix()
            if relative == "manifest.json":
                continue
            found.add(relative)
    return found


def _safe_member(root: Path, rel: str) -> Path:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts or not rel:
        raise ValidationError("backup manifest is unsupported")
    child = root.joinpath(path)
    if child.is_symlink():
        raise ValidationError("backup entry is not a regular file")
    try:
        child.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError("backup manifest is unsupported") from exc
    return child


def _load_projects(root: Path) -> list:
    try:
        return ProjectRegistry(root).list_projects()
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("registry.json is unsupported") from exc


def _load_controller(root: Path) -> None:
    if not (root / _CONTROLLER_NAME).is_file():
        return
    try:
        WorkControllerStore(root).list_workstreams()
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("work-controller.json is unsupported") from exc


def _project_count(files: list[_SnapshotFile]) -> int:
    raw = next((item.data for item in files if item.path == _DURABLE_NAME), None)
    if raw is None:
        return 0
    data = _json_object(raw, "registry.json is corrupt")
    projects = data.get("projects")
    if not isinstance(projects, dict):
        raise ValidationError("registry.json is unsupported")
    return len(projects)


def _publish_tree(partial: Path, target: Path) -> None:
    """Publish a completed tree only after its directory entries are durable.

    File bytes are already fsynced. Directory fsyncs make those names durable,
    and the destination parent is fsynced after the rename so a crash cannot
    report success for a publication the filesystem can still drop.
    """
    os.chmod(partial, 0o700)
    _fsync_directories(partial)
    os.rename(partial, target)
    os.chmod(target, 0o700)
    _fsync_directory(target)
    _fsync_directory(target.parent)


def _fsync_directories(root: Path) -> None:
    directories = [root]
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in dirnames:
            directories.append(current / name)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_snapshot_file(root: Path, item: _SnapshotFile) -> None:
    path = _safe_member(root, item.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(path, item.data)


def _write_bytes(path: Path, data: bytes) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _overlaps(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
