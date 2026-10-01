"""Read-only Personal Knowledge Plane dashboard and scoped retrieval.

Personal/reference sources are derived context only. This module never promotes
personal content to engineering authority and never exposes rejected source
bodies from import/quarantine metadata.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from atlas.local_markdown import SNAPSHOT_DIRNAME
from atlas.projection import ProjectionStore
from atlas.projection_retrieval import build_keyword_retriever, iter_validated_projections
from atlas.provenance import PERSONAL_SOURCE_CLASS, ValidationError
from atlas.registry import ProjectRegistry

MANIFEST_FILENAME = f"{SNAPSHOT_DIRNAME}/import-manifest.json"
_MANIFEST_KIND = "personal_knowledge_import_manifest"
_MANIFEST_STATES = frozenset({"IMPORTED", "SANITIZED", "QUARANTINED", "REJECTED"})
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_ITEMS = 4096


def _snapshot_fact(snapshot_root: Path, project_id: str, source_id: str) -> dict[str, object]:
    if snapshot_root.is_symlink():
        raise ValidationError("personal snapshot root is unsafe")
    if not snapshot_root.exists():
        return {"state": "MISSING", "size_bytes": None, "sha256": None}
    if not snapshot_root.is_dir():
        raise ValidationError("personal snapshot root is unsafe")
    project_root = snapshot_root / project_id
    if project_root.is_symlink():
        raise ValidationError("personal snapshot project directory is unsafe")
    if not project_root.exists():
        return {"state": "MISSING", "size_bytes": None, "sha256": None}
    if not project_root.is_dir():
        raise ValidationError("personal snapshot project directory is unsafe")
    path = project_root / f"{source_id}.md"
    if path.is_symlink() or not path.is_file():
        return {"state": "MISSING", "size_bytes": None, "sha256": None}
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationError("personal snapshot inventory is unreadable") from exc
    return {
        "state": "PRESENT",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _manifest(data_root: Path) -> dict[str, object]:
    root = Path(data_root)
    snapshot_root = root / SNAPSHOT_DIRNAME
    if snapshot_root.is_symlink():
        raise ValidationError("personal import manifest root is unsafe")
    if snapshot_root.exists() and not snapshot_root.is_dir():
        raise ValidationError("personal import manifest root is unsafe")
    path = root / MANIFEST_FILENAME
    if not path.exists():
        return {
            "state": "UNKNOWN",
            "detail": "no bounded personal import manifest is present",
            "source_system": None,
            "observed_at": None,
            "counts": {"total": 0, "imported": 0, "sanitized": 0, "quarantined": 0, "rejected": 0},
            "items": [],
        }
    if path.is_symlink() or not path.is_file():
        raise ValidationError("personal import manifest is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("personal import manifest is unreadable") from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValidationError("personal import manifest exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("personal import manifest is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("personal import manifest is invalid")
    if payload.get("schema_version") != 1 or payload.get("kind") != _MANIFEST_KIND:
        raise ValidationError("personal import manifest is unsupported")
    source_system = payload.get("source_system")
    observed_at = payload.get("observed_at")
    items = payload.get("items")
    if not isinstance(source_system, str) or not source_system or len(source_system) > 64:
        raise ValidationError("personal import manifest source_system is invalid")
    if not isinstance(observed_at, str) or not observed_at or len(observed_at) > 64:
        raise ValidationError("personal import manifest observed_at is invalid")
    if not isinstance(items, list) or len(items) > _MAX_ITEMS:
        raise ValidationError("personal import manifest items are invalid")

    normalized: list[dict[str, object]] = []
    counts = {"total": 0, "imported": 0, "sanitized": 0, "quarantined": 0, "rejected": 0}
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("personal import manifest item is invalid")
        allowed = {"source_id", "external_id", "title", "state", "reason_code"}
        if set(item) - allowed:
            raise ValidationError("personal import manifest item contains unsupported fields")
        source_id = item.get("source_id")
        external_id = item.get("external_id")
        title = item.get("title")
        state = item.get("state")
        reason_code = item.get("reason_code")
        if source_id is not None and (not isinstance(source_id, str) or not source_id or len(source_id) > 128):
            raise ValidationError("personal import manifest source_id is invalid")
        if external_id is not None and (not isinstance(external_id, str) or not external_id or len(external_id) > 128):
            raise ValidationError("personal import manifest external_id is invalid")
        if title is not None and (not isinstance(title, str) or len(title) > 256):
            raise ValidationError("personal import manifest title is invalid")
        if state not in _MANIFEST_STATES:
            raise ValidationError("personal import manifest state is invalid")
        if reason_code is not None and (not isinstance(reason_code, str) or not reason_code or len(reason_code) > 128):
            raise ValidationError("personal import manifest reason_code is invalid")
        normalized.append(
            {
                "source_id": source_id,
                "external_id": external_id,
                "title": title,
                "state": state,
                "reason_code": reason_code,
            }
        )
        counts["total"] += 1
        counts[state.lower()] += 1
    return {
        "state": "OBSERVED",
        "detail": "bounded import/quarantine metadata only; rejected bodies are not retained here",
        "source_system": source_system,
        "observed_at": observed_at,
        "counts": counts,
        "items": normalized,
    }


def personal_knowledge_dashboard(
    data_root: Path,
    *,
    registry: ProjectRegistry | None = None,
    projections: ProjectionStore | None = None,
    snapshot_root: Path | None = None,
) -> dict[str, object]:
    root = Path(data_root)
    registry = registry or ProjectRegistry(root)
    snapshot_root = Path(snapshot_root) if snapshot_root is not None else root / SNAPSHOT_DIRNAME
    projections = projections or ProjectionStore(root / "projections", snapshot_root=snapshot_root)
    projects: list[dict[str, object]] = []
    totals = {
        "personal_sources": 0,
        "engineering_sources": 0,
        "successful_projections": 0,
        "projection_errors": 0,
        "projection_gaps": 0,
        "snapshots_present": 0,
        "snapshots_missing": 0,
    }
    projection_records = {
        str(record.get("key") or ""): record
        for record in projections.list_records()
    }

    for project in registry.list_projects():
        all_sources = registry.list_sources(project.project_id)
        sources = [
            source
            for source in all_sources
            if source.source_class == PERSONAL_SOURCE_CLASS
        ]
        engineering_source_count = len(all_sources) - len(sources)
        totals["engineering_sources"] += engineering_source_count
        if not sources:
            continue
        validated_personal = {
            item.identity.partition("@")[0]: item
            for item in iter_validated_projections(
                projections,
                project.project_id,
                source_classes=frozenset({PERSONAL_SOURCE_CLASS}),
            )
        }
        rows: list[dict[str, object]] = []
        for source in sources:
            record = projection_records.get(f"{project.project_id}/{source.source_id}", {})
            sync_state = str(record.get("sync_state") or "missing").lower()
            validated = validated_personal.get(source.source_id)
            snapshot = _snapshot_fact(snapshot_root, project.project_id, source.source_id)
            if sync_state in {"success", "unchanged", "ok"}:
                if validated is None:
                    raise ValidationError("personal projection validation is incomplete")
                totals["successful_projections"] += 1
            elif sync_state == "error":
                totals["projection_errors"] += 1
            else:
                totals["projection_gaps"] += 1
            if snapshot["state"] == "PRESENT":
                totals["snapshots_present"] += 1
            else:
                totals["snapshots_missing"] += 1
            totals["personal_sources"] += 1
            rows.append(
                {
                    "source_id": source.source_id,
                    "title": source.title,
                    "source_path": source.source_path,
                    "provider": source.provider,
                    "source_class": source.source_class,
                    "engineering_authority": False,
                    "enabled": source.enabled,
                    "sync_state": sync_state.upper(),
                    "source_revision": (
                        validated.provenance.source_revision if validated is not None else None
                    ),
                    "fetched_at": record.get("fetched_at"),
                    "snapshot": snapshot,
                }
            )
        projects.append(
            {
                "project_id": project.project_id,
                "display_name": project.display_name,
                "enabled": project.enabled,
                "personal_source_count": len(rows),
                "engineering_source_count": engineering_source_count,
                "sources": rows,
            }
        )
    manifest = _manifest(root)
    return {
        "state": "OBSERVED",
        "authority": "PERSONAL_REFERENCE_ONLY",
        "engineering_authority": False,
        "canonical": False,
        "tela_runtime_dependency": False,
        "totals": totals,
        "projects": projects,
        "import_manifest": {**manifest, "path": MANIFEST_FILENAME},
    }


def build_personal_retriever(store, project_id: str):
    return build_keyword_retriever(
        store,
        project_id,
        source_classes=frozenset({PERSONAL_SOURCE_CLASS}),
    )
