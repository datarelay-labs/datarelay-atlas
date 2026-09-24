"""Phase 1 project lifecycle orchestration over registry + projection store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas.adoption import (
    AdoptionMetadata,
    assert_adoption_project_consistency,
    parse_adoption_yaml,
)
from atlas.github_sync import FetchFn, fetch_github_file
from atlas.projection import PROJECTOR_ID, ProjectionRecord, ProjectionStore
from atlas.projection_retrieval import build_keyword_retriever
from atlas.provenance import CanonicalSource, ValidationError
from atlas.registry import ProjectRecord, ProjectRegistry, RegisteredSource
from atlas.retrieval import RetrievalHit, Retriever
from atlas.semantic_retrieval import EmbeddingClient, EmbeddingConfig


class AtlasService:
    """Bounded Phase 1 operator workflow: register → source → sync → rebuild."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.registry = ProjectRegistry(self.data_root)
        self.projections = ProjectionStore(self.data_root / "projections")

    def register_project(
        self,
        *,
        project_id: str,
        repository: str,
        display_name: str | None = None,
        default_ref: str = "main",
        engineering_metadata_path: str = ".engineering/project.yaml",
        enabled: bool = True,
    ) -> ProjectRecord:
        return self.registry.register(
            project_id=project_id,
            repository=repository,
            display_name=display_name,
            default_ref=default_ref,
            engineering_metadata_path=engineering_metadata_path,
            enabled=enabled,
        )

    def show_project(self, project_id: str) -> dict[str, Any]:
        project = self.registry.get(project_id)
        return {
            "project": self.registry._project_to_dict(project),
            "projector": PROJECTOR_ID,
        }

    def list_projects(self) -> list[ProjectRecord]:
        return self.registry.list_projects()

    def add_source(
        self,
        project_id: str,
        *,
        source_id: str,
        source_path: str,
        ref: str | None = None,
        enabled: bool = True,
        title: str | None = None,
    ) -> RegisteredSource:
        return self.registry.add_source(
            project_id,
            source_id=source_id,
            source_path=source_path,
            ref=ref,
            enabled=enabled,
            title=title,
        )

    def list_sources(self, project_id: str) -> list[RegisteredSource]:
        return self.registry.list_sources(project_id)

    def sync_project(
        self,
        project_id: str,
        *,
        token: str | None = None,
        fetch: FetchFn | None = None,
    ) -> list[ProjectionRecord]:
        sources = self.registry.canonical_sources(project_id)
        if not sources:
            raise ValidationError(f"no sources configured for project {project_id}")
        return self.projections.sync_all(sources, token=token, fetch=fetch)

    def rebuild_project(
        self,
        project_id: str,
        *,
        token: str | None = None,
        fetch: FetchFn | None = None,
    ) -> list[ProjectionRecord]:
        self.projections.clear_project(project_id)
        return self.sync_project(project_id, token=token, fetch=fetch)

    def read_adoption(
        self,
        project_id: str,
        *,
        token: str | None = None,
        fetch: FetchFn | None = None,
    ) -> AdoptionMetadata:
        project = self.registry.get(project_id)
        meta_source = CanonicalSource(
            source_id="__engineering_metadata__",
            project_id=project.project_id,
            provider="github",
            repository=project.repository,
            ref=project.default_ref,
            source_path=project.engineering_metadata_path,
            enabled=True,
            title="Engineering System adoption",
        )
        fetch_fn = fetch or (lambda src, tok: fetch_github_file(src, tok))
        fetched = fetch_fn(meta_source, token)
        adoption = parse_adoption_yaml(
            fetched.content,
            source_path=project.engineering_metadata_path,
        )
        assert_adoption_project_consistency(adoption, project_id=project_id)
        return adoption

    def projection_records(self, project_id: str) -> list[dict[str, Any]]:
        return self.projections.list_records(project_id=project_id)

    def search(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 8,
        embedding: EmbeddingConfig | None = None,
        embedder: EmbeddingClient | None = None,
    ) -> list[RetrievalHit]:
        """Search successful projections for one registered project.

        Without ``embedding``, results stay keyword-only.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        if not isinstance(query, str):
            raise ValidationError("query is required")
        self.registry.get(project_id)
        retriever = build_keyword_retriever(
            self.projections,
            project_id,
            embedding=embedding,
            embedder=embedder,
        )
        return retriever.search(project_id, query, limit=limit)

    def project_retriever(self, project_id: str) -> Retriever:
        """Build a project-scoped retriever from current projections.

        Callers must not cache the result across requests. A later sync or
        rebuild has to be visible on the next call.
        """
        self.registry.get(project_id)
        return build_keyword_retriever(self.projections, project_id)
