"""Bounded directory import for personal/reference Markdown sources.

This module reuses Atlas local-markdown snapshots. It never grants engineering
authority and never persists rejected source bodies.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from atlas.data_lock import data_root_write_lock
from atlas.local_markdown import (
    content_sha256,
    fetch_local_markdown,
    publish_snapshot,
    read_allowlisted_markdown,
)
from atlas.projection import ProjectionStore
from atlas.provenance import (
    LOCAL_MARKDOWN_PROVIDER,
    PERSONAL_SOURCE_CLASS,
    ValidationError,
)
from atlas.registry import ProjectRegistry, SOURCE_ID_RE
from atlas.secrets import contains_unsafe_secret

_COLLECTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_MAX_FILES = 4096
_MAX_ENTRIES = 8192
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_RELATIVE_PATH = 512


def directory_source_id(collection_id: str, relative_path: str) -> str:
    """Return a stable source id for one collection-relative Markdown path."""
    _validate_collection_id(collection_id)
    try:
        encoded = relative_path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("personal directory path is not UTF-8") from exc
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    source_id = f"{collection_id}-{digest}"
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise ValidationError("personal directory source id is invalid")
    return source_id


def import_personal_markdown_directory(
    *,
    data_root: Path,
    registry: ProjectRegistry,
    projections: ProjectionStore,
    snapshot_root: Path,
    project_id: str,
    source_root: Path,
    collection_id: str,
) -> dict[str, object]:
    """Import one explicit Markdown directory under the Atlas data-root lock."""
    with data_root_write_lock(data_root):
        return _import_personal_markdown_directory_locked(
            data_root=data_root,
            registry=registry,
            projections=projections,
            snapshot_root=snapshot_root,
            project_id=project_id,
            source_root=source_root,
            collection_id=collection_id,
        )


def _import_personal_markdown_directory_locked(
    *,
    data_root: Path,
    registry: ProjectRegistry,
    projections: ProjectionStore,
    snapshot_root: Path,
    project_id: str,
    source_root: Path,
    collection_id: str,
) -> dict[str, object]:
    _validate_collection_id(collection_id)
    root = _validated_source_root(source_root, data_root)
    project = registry.get(project_id)
    discovered, rejected = _scan_directory(root)

    existing_sources = registry.list_sources(project_id)
    existing_by_id = {source.source_id: source for source in existing_sources}
    existing_by_path = {source.source_path: source for source in existing_sources}
    canonical_by_id = {
        source.source_id: source for source in registry.canonical_sources(project_id)
    }

    prefix = f"{collection_id}/"
    planned_ids: dict[str, str] = {}
    for relative_path, _size_bytes in discovered:
        source_id = directory_source_id(collection_id, relative_path)
        prior_path = planned_ids.get(source_id)
        if prior_path is not None and prior_path != relative_path:
            raise ValidationError("personal directory source id collision")
        planned_ids[source_id] = relative_path

    current_paths: set[str] = set()
    items: list[dict[str, object]] = list(rejected)
    actual_total_bytes = 0
    counts = {
        "imported": 0,
        "updated": 0,
        "unchanged": 0,
        "quarantined": 0,
        "rejected": len(rejected),
        "missing": 0,
        "conflicts": 0,
    }

    def observe_bytes(count: int) -> None:
        nonlocal actual_total_bytes
        actual_total_bytes += count
        if actual_total_bytes > _MAX_TOTAL_BYTES:
            raise ValidationError(
                "personal directory Markdown bytes exceed bounded size"
            )

    prepared: list[dict[str, object]] = []
    for relative_path, _scanned_size in discovered:
        virtual_path = f"{collection_id}/{relative_path}"
        current_paths.add(virtual_path)
        source_id = directory_source_id(collection_id, relative_path)
        title = Path(relative_path).stem[:256] or source_id

        existing = existing_by_id.get(source_id)
        same_path = existing_by_path.get(virtual_path)
        if (
            (existing is not None and not _compatible_existing(existing, virtual_path))
            or (same_path is not None and same_path.source_id != source_id)
        ):
            counts["conflicts"] += 1
            items.append(
                _item(relative_path, source_id, "CONFLICT", "SOURCE_ID_OR_PATH_CONFLICT")
            )
            continue

        try:
            text = read_allowlisted_markdown(
                root,
                relative_path,
                max_bytes=_MAX_FILE_BYTES,
                byte_observer=observe_bytes,
            )
        except ValidationError as exc:
            if str(exc) == "personal directory Markdown bytes exceed bounded size":
                raise
            if existing is not None:
                raise ValidationError(
                    "existing personal directory source is unreadable or rejected"
                ) from exc
            reason = (
                "SECRET_GUARD"
                if str(exc) == "snapshot content looks secret"
                else "FILE_TOO_LARGE"
                if str(exc) == "snapshot exceeds bounded size"
                else "UNREADABLE_MARKDOWN"
            )
            state = "QUARANTINED" if reason == "SECRET_GUARD" else "REJECTED"
            counts["quarantined" if state == "QUARANTINED" else "rejected"] += 1
            items.append(_item(relative_path, source_id, state, reason))
            continue

        encoded = text.encode("utf-8")
        actual_size = len(encoded)
        incoming_digest = content_sha256(encoded)

        if existing is None:
            state = "IMPORTED"
        else:
            canonical = canonical_by_id[source_id]
            try:
                prior = fetch_local_markdown(snapshot_root, canonical)
                unchanged = prior.source_revision == incoming_digest
            except ValidationError as exc:
                if str(exc) != "snapshot is missing":
                    raise
                unchanged = False
            state = "UNCHANGED" if unchanged else "UPDATED"

        prepared.append(
            {
                "relative_path": relative_path,
                "virtual_path": virtual_path,
                "source_id": source_id,
                "title": title,
                "text": text,
                "size_bytes": actual_size,
                "state": state,
            }
        )

    for operation in prepared:
        relative_path = str(operation["relative_path"])
        virtual_path = str(operation["virtual_path"])
        source_id = str(operation["source_id"])
        title = str(operation["title"])
        text = str(operation["text"])
        actual_size = int(operation["size_bytes"])
        state = str(operation["state"])

        if state == "IMPORTED":
            published = publish_snapshot(snapshot_root, project_id, source_id, text)
            try:
                registered = registry.add_personal_snapshot(
                    project_id,
                    source_id=source_id,
                    source_path=virtual_path,
                    title=title,
                )
            except Exception:
                published.unlink(missing_ok=True)
                raise
            canonical = next(
                source
                for source in registry.canonical_sources(project_id)
                if source.source_id == registered.source_id
            )
        else:
            canonical = canonical_by_id[source_id]
            if state == "UPDATED":
                publish_snapshot(snapshot_root, project_id, source_id, text)

        record = projections.sync_one(canonical)
        if record.sync_state == "error":
            raise ValidationError("personal directory projection sync failed")
        counts[state.lower()] += 1
        items.append(
            _item(relative_path, source_id, state, None, size_bytes=actual_size)
        )

    for source in existing_sources:
        if (
            source.provider == LOCAL_MARKDOWN_PROVIDER
            and source.source_class == PERSONAL_SOURCE_CLASS
            and source.source_path.startswith(prefix)
            and source.source_path not in current_paths
        ):
            counts["missing"] += 1
            relative = source.source_path[len(prefix):]
            try:
                relative.encode("utf-8")
                unsafe_path = contains_unsafe_secret(relative)
            except UnicodeEncodeError:
                unsafe_path = True
            items.append(
                _item(
                    "[REDACTED_PATH]" if unsafe_path else relative,
                    source.source_id,
                    "MISSING",
                    (
                        "SOURCE_FILE_ABSENT_REDACTED_PATH"
                        if unsafe_path
                        else "SOURCE_FILE_ABSENT"
                    ),
                )
            )

    items.sort(key=lambda item: (str(item["relative_path"]), str(item["state"])))
    return {
        "authority": "PERSONAL_REFERENCE_ONLY",
        "engineering_authority": False,
        "project_id": project.project_id,
        "collection_id": collection_id,
        "counts": counts,
        "items": items,
    }


def _validate_collection_id(collection_id: str) -> None:
    if not isinstance(collection_id, str) or not _COLLECTION_ID_RE.fullmatch(collection_id):
        raise ValidationError("personal directory collection_id is invalid")


def _validated_source_root(source_root: Path, data_root: Path) -> Path:
    raw = Path(source_root)
    if raw.is_symlink() or not raw.is_dir():
        raise ValidationError("personal directory source root must be a real directory")
    try:
        root = raw.resolve(strict=True)
        durable = Path(data_root).resolve(strict=True)
    except OSError as exc:
        raise ValidationError("personal directory source root is unreadable") from exc
    if root == durable or root.is_relative_to(durable) or durable.is_relative_to(root):
        raise ValidationError("personal directory source root overlaps Atlas data root")
    return root


def _scan_directory(root: Path) -> tuple[list[tuple[str, int]], list[dict[str, object]]]:
    markdown: list[tuple[str, int]] = []
    rejected: list[dict[str, object]] = []
    total_bytes = 0
    entry_count = 0

    def walk(directory: Path) -> None:
        nonlocal total_bytes, entry_count
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ValidationError("personal directory source root is unreadable") from exc
        for entry in entries:
            entry_count += 1
            if entry_count > _MAX_ENTRIES:
                raise ValidationError("personal directory contains too many entries")
            path = Path(entry.path)
            if entry.is_symlink():
                raise ValidationError("personal directory contains a symlink")
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise ValidationError("personal directory entry is unreadable") from exc
            if stat.S_ISDIR(mode):
                walk(path)
                continue
            relative = path.relative_to(root).as_posix()
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError:
                rejected.append(
                    _item(
                        "[NON_UTF8_PATH]",
                        None,
                        "REJECTED",
                        "NON_UTF8_PATH",
                    )
                )
                continue
            if contains_unsafe_secret(relative):
                raise ValidationError("personal directory path metadata looks secret")
            if len(relative) > _MAX_RELATIVE_PATH:
                rejected.append(_item(relative[:_MAX_RELATIVE_PATH], None, "REJECTED", "PATH_TOO_LONG"))
                continue
            if not stat.S_ISREG(mode) or path.suffix != ".md":
                rejected.append(_item(relative, None, "REJECTED", "UNSUPPORTED_FILE"))
                continue
            size = entry.stat(follow_symlinks=False).st_size
            if size > _MAX_FILE_BYTES:
                rejected.append(_item(relative, None, "REJECTED", "FILE_TOO_LARGE"))
                continue
            markdown.append((relative, size))
            total_bytes += size
            if len(markdown) > _MAX_FILES:
                raise ValidationError("personal directory contains too many Markdown files")
            if total_bytes > _MAX_TOTAL_BYTES:
                raise ValidationError("personal directory Markdown bytes exceed bounded size")

    walk(root)
    markdown.sort(key=lambda value: value[0])
    return markdown, rejected


def _compatible_existing(source, virtual_path: str) -> bool:
    return (
        source.provider == LOCAL_MARKDOWN_PROVIDER
        and source.source_class == PERSONAL_SOURCE_CLASS
        and source.source_path == virtual_path
    )


def _item(
    relative_path: str,
    source_id: str | None,
    state: str,
    reason_code: str | None,
    *,
    size_bytes: int | None = None,
) -> dict[str, object]:
    return {
        "relative_path": relative_path,
        "source_id": source_id,
        "state": state,
        "reason_code": reason_code,
        "size_bytes": size_bytes,
    }
