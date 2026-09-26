"""Deterministic filesystem projection store for derived knowledge."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from atlas.data_lock import (
    atomic_write_text,
    data_root_write_lock,
    projection_store_lock_root,
)
from atlas.github_sync import FetchedSource, FetchFn, fetch_github_file
from atlas.provenance import (
    CanonicalSource,
    provenance_dict,
    provenance_from_source,
    render_derived_document,
    validate_source,
)

PROJECTOR_ID = "atlas.projection/v1"


@dataclass(frozen=True)
class ProjectionRecord:
    source_id: str
    project_id: str
    projection_path: str
    content_digest: str
    source_revision: str
    fetched_at: str
    sync_state: str
    projector: str = PROJECTOR_ID


class ProjectionStore:
    """Project-scoped derived store. Rebuildable from canonical sources."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._meta = self.root / "projections.json"

    def _load(self) -> dict:
        if not self._meta.exists():
            return {"projections": {}}
        return json.loads(self._meta.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        with data_root_write_lock(projection_store_lock_root(self.root)):
            atomic_write_text(self._meta, text)

    def projection_key(self, source: CanonicalSource) -> str:
        return f"{source.project_id}/{source.source_id}.md"

    def meta_key(self, source: CanonicalSource) -> str:
        return f"{source.project_id}/{source.source_id}"

    def sync_one(
        self,
        source: CanonicalSource,
        *,
        token: str | None = None,
        fetch: FetchFn | None = None,
    ) -> ProjectionRecord:
        validate_source(source)
        key = self.meta_key(source)
        if not source.enabled:
            record = ProjectionRecord(
                source_id=source.source_id,
                project_id=source.project_id,
                projection_path="",
                content_digest="",
                source_revision="",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                sync_state="disabled",
            )
            with data_root_write_lock(projection_store_lock_root(self.root)):
                data = self._load()
                data["projections"][key] = asdict(record)
                self._save(data)
            return record

        fetch_fn = fetch or (lambda src, tok: fetch_github_file(src, tok))
        try:
            fetched: FetchedSource = fetch_fn(source, token)
        except Exception as exc:  # noqa: BLE001 - fail closed to sync_state
            # Preserve prior successful projection bytes; mark current sync as error.
            with data_root_write_lock(projection_store_lock_root(self.root)):
                data = self._load()
                prior = data["projections"].get(key) or {}
                record = ProjectionRecord(
                    source_id=source.source_id,
                    project_id=source.project_id,
                    projection_path=str(prior.get("projection_path") or ""),
                    content_digest=str(prior.get("content_digest") or ""),
                    source_revision=str(prior.get("source_revision") or ""),
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    sync_state="error",
                )
                entry = asdict(record)
                entry["error"] = str(exc)
                if prior.get("provenance"):
                    entry["prior_provenance"] = prior["provenance"]
                data["projections"][key] = entry
                self._save(data)
            return record

        body = render_derived_document(source, fetched.content, fetched.source_revision)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        rel = self.projection_key(source)
        with data_root_write_lock(projection_store_lock_root(self.root)):
            data = self._load()
            prior = data["projections"].get(key) or {}
            unchanged = (
                prior.get("sync_state") in {"success", "unchanged", "ok"}
                and prior.get("source_revision") == fetched.source_revision
                and prior.get("content_digest") == digest
                and prior.get("projection_path") == rel
            )
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, body)

            sync_state = "unchanged" if unchanged else "success"
            prov = provenance_from_source(source, fetched.source_revision)
            record = ProjectionRecord(
                source_id=source.source_id,
                project_id=source.project_id,
                projection_path=rel,
                content_digest=digest,
                source_revision=fetched.source_revision,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                sync_state=sync_state,
            )
            data["projections"][key] = {
                **asdict(record),
                "provenance": dict(provenance_dict(prov)),
                "rebuild_key": (
                    f"{source.project_id}:{source.source_id}:"
                    f"{fetched.source_revision}:{PROJECTOR_ID}"
                ),
            }
            self._save(data)
        return record

    def sync_all(
        self,
        sources: Iterable[CanonicalSource],
        *,
        token: str | None = None,
        fetch: FetchFn | None = None,
    ) -> list[ProjectionRecord]:
        return [self.sync_one(source, token=token, fetch=fetch) for source in sources]

    def clear_project(self, project_id: str) -> None:
        with data_root_write_lock(projection_store_lock_root(self.root)):
            data = self._load()
            remaining = {
                key: value
                for key, value in data.get("projections", {}).items()
                if value.get("project_id") != project_id
            }
            project_dir = self.root / project_id
            if project_dir.exists():
                shutil.rmtree(project_dir)
            data["projections"] = remaining
            self._save(data)

    def list_records(self, project_id: str | None = None) -> list[dict[str, Any]]:
        data = self._load()
        out: list[dict[str, Any]] = []
        for key, meta in sorted(data.get("projections", {}).items()):
            if project_id and meta.get("project_id") != project_id:
                continue
            out.append({"key": key, **meta})
        return out

    def list_documents(self, project_id: str | None = None) -> list[tuple[str, str, str]]:
        """Return (project_id, source_id, text) for keyword indexing."""
        data = self._load()
        out: list[tuple[str, str, str]] = []
        for _key, meta in data.get("projections", {}).items():
            if meta.get("sync_state") not in {"success", "unchanged", "ok"}:
                continue
            if project_id and meta.get("project_id") != project_id:
                continue
            rel = meta.get("projection_path")
            if not rel:
                continue
            text = (self.root / rel).read_text(encoding="utf-8")
            out.append((meta["project_id"], meta["source_id"], text))
        return out
