"""Deterministic filesystem projection store for derived knowledge."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from atlas.github_sync import FetchedSource, FetchFn, fetch_github_file
from atlas.provenance import CanonicalSource, provenance_dict, provenance_from_source, render_derived_document, validate_source


@dataclass(frozen=True)
class ProjectionRecord:
    source_id: str
    project_id: str
    projection_path: str
    content_digest: str
    source_revision: str
    fetched_at: str
    sync_state: str


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
        self._meta.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def projection_key(self, source: CanonicalSource) -> str:
        return f"{source.project_id}/{source.source_id}.md"

    def sync_one(
        self,
        source: CanonicalSource,
        *,
        token: str | None = None,
        fetch: FetchFn | None = None,
    ) -> ProjectionRecord:
        validate_source(source)
        if not source.enabled:
            return ProjectionRecord(
                source_id=source.source_id,
                project_id=source.project_id,
                projection_path="",
                content_digest="",
                source_revision="",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                sync_state="disabled",
            )

        fetch_fn = fetch or (lambda src, tok: fetch_github_file(src, tok))
        try:
            fetched: FetchedSource = fetch_fn(source, token)
        except Exception as exc:  # noqa: BLE001 - fail closed to sync_state
            record = ProjectionRecord(
                source_id=source.source_id,
                project_id=source.project_id,
                projection_path="",
                content_digest="",
                source_revision="",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                sync_state="error",
            )
            data = self._load()
            entry = asdict(record)
            entry["error"] = str(exc)
            data["projections"][source.source_id] = entry
            self._save(data)
            return record

        body = render_derived_document(source, fetched.content, fetched.source_revision)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        rel = self.projection_key(source)
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

        prov = provenance_from_source(source, fetched.source_revision)
        record = ProjectionRecord(
            source_id=source.source_id,
            project_id=source.project_id,
            projection_path=rel,
            content_digest=digest,
            source_revision=fetched.source_revision,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            sync_state="ok",
        )
        data = self._load()
        data["projections"][source.source_id] = {
            **asdict(record),
            "provenance": dict(provenance_dict(prov)),
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

    def list_documents(self, project_id: str | None = None) -> list[tuple[str, str, str]]:
        """Return (project_id, source_id, text) for keyword indexing."""
        data = self._load()
        out: list[tuple[str, str, str]] = []
        for source_id, meta in data.get("projections", {}).items():
            if meta.get("sync_state") != "ok":
                continue
            if project_id and meta.get("project_id") != project_id:
                continue
            rel = meta.get("projection_path")
            if not rel:
                continue
            text = (self.root / rel).read_text(encoding="utf-8")
            out.append((meta["project_id"], source_id, text))
        return out
