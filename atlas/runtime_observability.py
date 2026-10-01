"""Bounded read-only Atlas runtime observability snapshot."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import LOCK_NAME
from atlas.data_protection import (
    _CONTROLLER_DIRS,
    _CONTROLLER_NAME,
    _DERIVED_CACHE_DIRS,
    _DERIVED_CACHE_FILES,
    _DURABLE_NAME,
    _PROJECTIONS_DIR,
    _tree_files,
)
from atlas.local_markdown import IMPORT_DIRNAME, SNAPSHOT_DIRNAME
from atlas.ops import data_root_runtime_ready
from atlas.projection import ProjectionStore
from atlas.provenance import ValidationError
from atlas.registry import ProjectRegistry
from atlas.sbom import _source_facts
from atlas.work_controller import (
    ALLOWED_STATES,
    COMPLETION_INBOX_DIRNAME,
    COMPLETION_PROCESSED_DIRNAME,
    WorkControllerStore,
    load_completion_event,
)

SCHEMA_VERSION = 1
KIND = "atlas_runtime_observability"
AUTHORITY = "OBSERVABILITY_ONLY"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_digest(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "runtime observability facts are not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _observed_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("runtime observability observed_at must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            "runtime observability observed_at must be UTC"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("runtime observability observed_at must be UTC")
    return value


def _completion_count(root: Path, dirname: str) -> int:
    directory = root / dirname
    if not directory.exists():
        return 0
    if directory.is_symlink() or not directory.is_dir():
        _reject("runtime observability completion queue is unsafe")
    count = 0
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if (
            entry.is_symlink()
            or not entry.is_file()
            or entry.suffix != ".json"
        ):
            _reject("runtime observability completion queue is unsafe")
        load_completion_event(entry)
        count += 1
    return count


def _derived_cache_stats(root: Path) -> dict[str, int]:
    root_count = 0
    file_count = 0
    for name in sorted(_DERIVED_CACHE_FILES):
        path = root / name
        if path.is_symlink():
            _reject("runtime observability derived cache is unsafe")
        if not path.exists():
            continue
        if not path.is_file():
            _reject("runtime observability derived cache is unsafe")
        root_count += 1
        file_count += 1

    for name in sorted(_DERIVED_CACHE_DIRS):
        path = root / name
        if path.is_symlink():
            _reject("runtime observability derived cache is unsafe")
        if not path.exists():
            continue
        if not path.is_dir():
            _reject("runtime observability derived cache is unsafe")
        root_count += 1
        file_count += len(_tree_files(path))

    return {
        "present_root_count": root_count,
        "file_count": file_count,
    }


def _regular_size(path: Path, *, error: str) -> int:
    if path.is_symlink() or not path.is_file():
        _reject(error)
    try:
        return path.stat().st_size
    except OSError as exc:
        raise ValidationError(error) from exc


def _tree_stats(path: Path, *, error: str) -> tuple[int, int]:
    if path.is_symlink() or not path.is_dir():
        _reject(error)
    files = _tree_files(path)
    return len(files), sum(_regular_size(item, error=error) for item in files)


def _storage_stats(root: Path) -> dict[str, int]:
    durable_files = 0
    durable_bytes = 0
    rebuildable_files = 0
    rebuildable_bytes = 0
    unexpected = False

    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        name = entry.name
        if name == LOCK_NAME:
            if entry.is_symlink():
                _reject("runtime observability data root is unsafe")
            continue
        if name in _DERIVED_CACHE_FILES or name in _DERIVED_CACHE_DIRS:
            continue
        if name.endswith(".tmp"):
            _reject("runtime observability data root is unsafe")
        if name in {_DURABLE_NAME, _CONTROLLER_NAME}:
            durable_files += 1
            durable_bytes += _regular_size(
                entry,
                error="runtime observability durable state is unsafe",
            )
            continue
        if name in _CONTROLLER_DIRS or name == SNAPSHOT_DIRNAME:
            count, size = _tree_stats(
                entry,
                error="runtime observability durable state is unsafe",
            )
            durable_files += count
            durable_bytes += size
            continue
        if name == _PROJECTIONS_DIR:
            count, size = _tree_stats(
                entry,
                error="runtime observability projection state is unsafe",
            )
            rebuildable_files += count
            rebuildable_bytes += size
            continue
        if name == IMPORT_DIRNAME:
            if entry.is_symlink() or not entry.is_dir():
                _reject("runtime observability personal import state is unsafe")
            continue
        unexpected = True

    if unexpected:
        _reject("runtime observability data root contains unexpected entries")
    return {
        "durable_file_count": durable_files,
        "durable_byte_count": durable_bytes,
        "rebuildable_file_count": rebuildable_files,
        "rebuildable_byte_count": rebuildable_bytes,
    }


def _canonical_counts(root: Path) -> dict[str, object]:
    registry = ProjectRegistry(root)
    controller = WorkControllerStore(root)

    projects = registry.list_projects()
    project_count = len(projects)
    enabled_project_count = sum(project.enabled for project in projects)
    sources_by_project = {
        project.project_id: registry.list_sources(project.project_id)
        for project in projects
    }
    source_count = sum(len(items) for items in sources_by_project.values())
    enabled_source_count = sum(
        project.enabled and source.enabled
        for project in projects
        for source in sources_by_project[project.project_id]
    )

    projection_root = root / "projections"
    if projection_root.exists():
        if projection_root.is_symlink() or not projection_root.is_dir():
            _reject("runtime observability projection store is unsafe")
        projections = ProjectionStore(
            projection_root,
            snapshot_root=root / SNAPSHOT_DIRNAME,
        )
        records = projections.list_records()
        documents = projections.list_documents()
    else:
        records = []
        documents = []
    projection_projects = {
        str(record["project_id"])
        for record in records
        if isinstance(record.get("project_id"), str)
    }
    workstreams = controller.list_workstreams()
    state_counts = {
        state: sum(record.state == state for record in workstreams)
        for state in sorted(ALLOWED_STATES)
    }
    return {
        "registry": {
            "project_count": project_count,
            "enabled_project_count": enabled_project_count,
            "source_count": source_count,
            "enabled_source_count": enabled_source_count,
        },
        "projections": {
            "project_count": len(projection_projects),
            "record_count": len(records),
            "document_count": len(documents),
        },
        "controller": {
            "workstream_count": len(workstreams),
            "state_counts": state_counts,
        },
    }


def _python_facts() -> dict[str, str]:
    return {
        "implementation": sys.implementation.name,
        "version": platform.python_version(),
    }


def _source_payload(
    repository_root: Path,
    supplied: dict[str, object] | None,
) -> dict[str, object]:
    if supplied is None:
        try:
            source = _source_facts(repository_root, require_clean=False)
        except ValidationError:
            return {
                "state": "UNKNOWN",
                "repository": "UNKNOWN",
                "head": "UNKNOWN",
                "clean": "UNKNOWN",
            }
    else:
        source = dict(supplied)

    repository = source.get("repository")
    head = source.get("source_revision")
    clean = source.get("clean")
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(head, str)
        or _HEAD.fullmatch(head) is None
        or not isinstance(clean, bool)
    ):
        _reject("runtime observability source facts are invalid")
    return {
        "state": "OBSERVED",
        "repository": repository,
        "head": head,
        "clean": clean,
    }


def _validate_snapshot(payload: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "observed_at",
        "snapshot_digest",
        "source",
        "python",
        "data_root",
        "registry",
        "projections",
        "controller",
        "completion",
        "storage",
        "derived_cache",
        "release_authority",
        "pass_authority",
        "deploy_authority",
        "merge_authority",
        "repair_authority",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("runtime observability snapshot schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("release_authority") != "NONE"
        or payload.get("pass_authority") != "NONE"
        or payload.get("deploy_authority") != "NONE"
        or payload.get("merge_authority") != "NONE"
        or payload.get("repair_authority") != "NONE"
    ):
        _reject("runtime observability snapshot authority is invalid")
    _observed_at(payload.get("observed_at"))
    digest = payload.get("snapshot_digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        _reject("runtime observability snapshot digest is invalid")
    basis = {
        key: value
        for key, value in payload.items()
        if key not in {"observed_at", "snapshot_digest"}
    }
    if _canonical_digest(basis) != digest:
        _reject("runtime observability snapshot digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def runtime_observability_snapshot(
    data_root: Path,
    *,
    repo_root: Path | None = None,
    observed_at: str | None = None,
    source_facts: dict[str, object] | None = None,
    python_facts: dict[str, str] | None = None,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("runtime observability data root is unavailable")
    repository_root = (
        Path(repo_root)
        if repo_root is not None
        else Path(__file__).resolve().parents[1]
    )

    try:
        source_payload = _source_payload(repository_root, source_facts)
        python_state = (
            dict(python_facts)
            if python_facts is not None
            else _python_facts()
        )
        counts = _canonical_counts(root)
        storage = _storage_stats(root)
        derived = _derived_cache_stats(root)
        inbox_count = _completion_count(root, COMPLETION_INBOX_DIRNAME)
        processed_count = _completion_count(
            root,
            COMPLETION_PROCESSED_DIRNAME,
        )
    except ValidationError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise ValidationError(
            "runtime observability canonical state is invalid"
        ) from exc

    python_payload = {
        "implementation": python_state.get("implementation"),
        "version": python_state.get("version"),
    }
    if not all(
        isinstance(value, str) and bool(value)
        for value in python_payload.values()
    ):
        _reject("runtime observability Python facts are invalid")

    basis: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "source": source_payload,
        "python": python_payload,
        "data_root": {
            "state": (
                "READY"
                if data_root_runtime_ready(root)
                else "NOT_READY"
            )
        },
        **counts,
        "completion": {
            "inbox_count": inbox_count,
            "processed_count": processed_count,
        },
        "storage": storage,
        "derived_cache": derived,
        "release_authority": "NONE",
        "pass_authority": "NONE",
        "deploy_authority": "NONE",
        "merge_authority": "NONE",
        "repair_authority": "NONE",
    }
    snapshot = {
        **basis,
        "observed_at": _observed_at(observed_at),
        "snapshot_digest": _canonical_digest(basis),
    }
    return _validate_snapshot(snapshot)
