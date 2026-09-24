"""Build a project-scoped keyword Retriever from ProjectionStore documents.

The index is rebuilt in memory from successful projection bytes and metadata.
No separate search index is persisted.
"""

from __future__ import annotations

import hashlib
import json

from atlas.projection import ProjectionStore
from atlas.provenance import (
    PROJECT_ID_RE,
    REPO_RE,
    Provenance,
    ValidationError,
    validate_source_path,
)
from atlas.retrieval import IndexedDocument, KeywordIndex, Retriever, normalize_path
from atlas.security import require_project_scope

INDEXABLE_SYNC_STATES = frozenset({"success", "unchanged", "ok"})
_REQUIRED_PROVENANCE = (
    "project_id",
    "provider",
    "repository",
    "ref",
    "source_path",
    "source_revision",
)


def build_keyword_retriever(store: ProjectionStore, project_id: str) -> Retriever:
    """Index one project's successful projections with metadata provenance."""
    require_project_scope(project_id)
    records = _load_records(store, project_id)
    index = KeywordIndex()
    provenance_by_path: dict[tuple[str, str], Provenance] = {}
    seen_paths: set[str] = set()

    for meta in records:
        if meta.get("sync_state") not in INDEXABLE_SYNC_STATES:
            continue
        label = _record_label(meta, project_id)
        provenance = _provenance_from_record(meta, project_id=project_id, label=label)
        path = normalize_path(provenance.source_path)
        if path in seen_paths:
            raise ValidationError(f"duplicate projection source_path: {path}")
        seen_paths.add(path)
        text = _read_projection_text(
            store,
            meta.get("projection_path"),
            label,
            meta.get("content_digest"),
        )
        index.add(
            IndexedDocument(
                project_id=project_id,
                path=path,
                title=path,
                text=text,
                provenance=provenance,
            )
        )
        provenance_by_path[(project_id, path)] = provenance

    return Retriever(index, provenance_by_path=provenance_by_path)


def _load_records(store: ProjectionStore, project_id: str) -> list[dict]:
    try:
        return store.list_records(project_id=project_id)
    except json.JSONDecodeError as exc:
        raise ValidationError("corrupt projection metadata") from exc


def _record_label(meta: dict, project_id: str) -> str:
    source_id = meta.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        return f"{project_id}/{source_id}"
    key = meta.get("key")
    if isinstance(key, str) and key.strip():
        return key
    return project_id


def _provenance_from_record(meta: dict, *, project_id: str, label: str) -> Provenance:
    raw = meta.get("provenance")
    if not isinstance(raw, dict):
        raise ValidationError(f"malformed projection provenance: {label}")
    if meta.get("project_id") != project_id:
        raise ValidationError(f"malformed projection provenance: {label}")
    values: dict[str, str] = {}
    for field in _REQUIRED_PROVENANCE:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"malformed projection provenance: {label}")
        values[field] = value
    if values["project_id"] != project_id:
        raise ValidationError(f"malformed projection provenance: {label}")
    if raw.get("canonical") is not False or raw.get("derived") is not True:
        raise ValidationError(f"malformed projection provenance: {label}")
    if values["provider"] != "github" or not REPO_RE.match(values["repository"]):
        raise ValidationError(f"malformed projection provenance: {label}")
    if not PROJECT_ID_RE.match(values["project_id"]):
        raise ValidationError(f"malformed projection provenance: {label}")
    try:
        validate_source_path(values["source_path"])
    except ValidationError as exc:
        raise ValidationError(f"malformed projection provenance: {label}") from exc
    return Provenance(
        project_id=values["project_id"],
        provider=values["provider"],
        repository=values["repository"],
        ref=values["ref"],
        source_path=values["source_path"],
        source_revision=values["source_revision"],
        derived=True,
        canonical=False,
    )


def _read_projection_text(
    store: ProjectionStore,
    rel: object,
    label: str,
    content_digest: object,
) -> str:
    if not isinstance(rel, str) or not rel.strip():
        raise ValidationError(f"projection bytes missing: {label}")
    if rel.startswith("/") or "\\" in rel or any(part in {"", ".", ".."} for part in rel.split("/")):
        raise ValidationError(f"projection bytes missing: {label}")
    root = store.root.resolve()
    try:
        candidate = (store.root / rel).resolve()
    except OSError as exc:
        raise ValidationError(f"projection bytes unreadable: {label}") from exc
    if not candidate.is_relative_to(root):
        raise ValidationError(f"projection bytes missing: {label}")
    if not candidate.is_file():
        raise ValidationError(f"projection bytes missing: {label}")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(f"projection bytes unreadable: {label}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if not isinstance(content_digest, str) or digest != content_digest:
        raise ValidationError(f"projection bytes digest mismatch: {label}")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError(f"projection bytes unreadable: {label}") from exc
