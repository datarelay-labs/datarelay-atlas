"""Atlas-owned project registry (ADR-0005)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from atlas.provenance import (
    REPO_RE,
    CanonicalSource,
    ValidationError,
    validate_project_id,
    validate_source_path,
)

REGISTRY_SCHEMA_VERSION = 1
DEFAULT_ENGINEERING_METADATA_PATH = ".engineering/project.yaml"
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")


@dataclass(frozen=True)
class RegisteredSource:
    source_id: str
    source_path: str
    ref: str | None = None
    enabled: bool = True
    media_type: str = "text/markdown"
    title: str | None = None
    provider: str = "github"


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    repository: str
    default_ref: str = "main"
    engineering_metadata_path: str = DEFAULT_ENGINEERING_METADATA_PATH
    enabled: bool = True
    sources: dict[str, RegisteredSource] = field(default_factory=dict)


class ProjectRegistry:
    """Deterministic project-scoped registry backed by local JSON (ADR-0005)."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.path = self.data_root / "registry.json"

    def _empty(self) -> dict:
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "projects": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        version = data.get("schema_version")
        if version != REGISTRY_SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported registry schema_version: {version}; "
                f"expected {REGISTRY_SCHEMA_VERSION}"
            )
        if "projects" not in data or not isinstance(data["projects"], dict):
            raise ValidationError("registry.json missing projects map")
        return data

    def _save(self, data: dict) -> None:
        data = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "projects": data.get("projects", {}),
        }
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _validate_ref(self, ref: str) -> None:
        if not ref or not ref.strip() or not REF_RE.match(ref):
            raise ValidationError(f"invalid ref: {ref}")

    def _validate_repository(self, repository: str) -> None:
        if not REPO_RE.match(repository):
            raise ValidationError(f"invalid repository: {repository}")

    def _validate_source_id(self, source_id: str) -> None:
        if not SOURCE_ID_RE.match(source_id):
            raise ValidationError(f"invalid source_id: {source_id}")

    def _project_from_dict(self, raw: dict) -> ProjectRecord:
        sources: dict[str, RegisteredSource] = {}
        for source_id, src in (raw.get("sources") or {}).items():
            sources[source_id] = RegisteredSource(
                source_id=source_id,
                source_path=src["source_path"],
                ref=src.get("ref"),
                enabled=bool(src.get("enabled", True)),
                media_type=src.get("media_type", "text/markdown"),
                title=src.get("title"),
                provider=src.get("provider", "github"),
            )
        return ProjectRecord(
            project_id=raw["project_id"],
            display_name=raw.get("display_name") or raw["project_id"],
            repository=raw["repository"],
            default_ref=raw.get("default_ref", "main"),
            engineering_metadata_path=raw.get(
                "engineering_metadata_path", DEFAULT_ENGINEERING_METADATA_PATH
            ),
            enabled=bool(raw.get("enabled", True)),
            sources=sources,
        )

    def _project_to_dict(self, project: ProjectRecord) -> dict:
        return {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "repository": project.repository,
            "default_ref": project.default_ref,
            "engineering_metadata_path": project.engineering_metadata_path,
            "enabled": project.enabled,
            "sources": {
                sid: {
                    "source_id": src.source_id,
                    "source_path": src.source_path,
                    "ref": src.ref,
                    "enabled": src.enabled,
                    "media_type": src.media_type,
                    "title": src.title,
                    "provider": src.provider,
                }
                for sid, src in sorted(project.sources.items())
            },
        }

    def register(
        self,
        *,
        project_id: str,
        repository: str,
        display_name: str | None = None,
        default_ref: str = "main",
        engineering_metadata_path: str = DEFAULT_ENGINEERING_METADATA_PATH,
        enabled: bool = True,
    ) -> ProjectRecord:
        validate_project_id(project_id)
        self._validate_repository(repository)
        self._validate_ref(default_ref)
        validate_source_path(engineering_metadata_path)

        data = self._load()
        if project_id in data["projects"]:
            raise ValidationError(f"duplicate project_id: {project_id}")

        project = ProjectRecord(
            project_id=project_id,
            display_name=display_name or project_id,
            repository=repository,
            default_ref=default_ref,
            engineering_metadata_path=engineering_metadata_path,
            enabled=enabled,
            sources={},
        )
        data["projects"][project_id] = self._project_to_dict(project)
        self._save(data)
        return project

    def get(self, project_id: str) -> ProjectRecord:
        validate_project_id(project_id)
        data = self._load()
        raw = data["projects"].get(project_id)
        if raw is None:
            raise ValidationError(f"unknown project_id: {project_id}")
        return self._project_from_dict(raw)

    def list_projects(self) -> list[ProjectRecord]:
        data = self._load()
        return [
            self._project_from_dict(raw)
            for _, raw in sorted(data["projects"].items())
        ]

    def add_source(
        self,
        project_id: str,
        *,
        source_id: str,
        source_path: str,
        ref: str | None = None,
        enabled: bool = True,
        media_type: str = "text/markdown",
        title: str | None = None,
        provider: str = "github",
    ) -> RegisteredSource:
        project = self.get(project_id)
        self._validate_source_id(source_id)
        validate_source_path(source_path)
        if provider != "github":
            raise ValidationError(f"unsupported provider: {provider}")
        if ref is not None:
            self._validate_ref(ref)
        if source_id in project.sources:
            raise ValidationError(
                f"duplicate source_id in project {project_id}: {source_id}"
            )

        source = RegisteredSource(
            source_id=source_id,
            source_path=source_path,
            ref=ref,
            enabled=enabled,
            media_type=media_type,
            title=title,
            provider=provider,
        )
        data = self._load()
        sources = data["projects"][project_id].setdefault("sources", {})
        sources[source_id] = asdict(source)
        self._save(data)
        return source

    def list_sources(self, project_id: str) -> list[RegisteredSource]:
        project = self.get(project_id)
        return [project.sources[sid] for sid in sorted(project.sources)]

    def canonical_sources(self, project_id: str) -> list[CanonicalSource]:
        project = self.get(project_id)
        out: list[CanonicalSource] = []
        for source in self.list_sources(project_id):
            out.append(
                CanonicalSource(
                    source_id=source.source_id,
                    project_id=project.project_id,
                    provider=source.provider,
                    repository=project.repository,
                    ref=source.ref or project.default_ref,
                    source_path=source.source_path,
                    enabled=project.enabled and source.enabled,
                    media_type=source.media_type,
                    title=source.title,
                )
            )
        return out

    def assert_repository_identity(
        self, project_id: str, expected_repository: str
    ) -> None:
        project = self.get(project_id)
        self._validate_repository(expected_repository)
        if project.repository != expected_repository:
            raise ValidationError(
                f"repository identity mismatch for {project_id}: "
                f"registered={project.repository} expected={expected_repository}"
            )
