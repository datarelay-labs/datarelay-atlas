"""Phase 1 project lifecycle orchestration over registry + projection store."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from atlas.adoption import (
    AdoptionMetadata,
    assert_adoption_project_consistency,
    parse_adoption_yaml,
)
from atlas.github_sync import FetchFn, fetch_github_file
from atlas.derived_intelligence import derived_intelligence_payload
from atlas.data_lock import data_root_write_lock
from atlas.local_markdown import (
    IMPORT_DIRNAME,
    SNAPSHOT_DIRNAME,
    publish_snapshot,
    read_allowlisted_markdown,
)
from atlas.projection import PROJECTOR_ID, ProjectionRecord, ProjectionStore
from atlas.projection_retrieval import build_keyword_retriever, iter_validated_projections
from atlas.provenance import CanonicalSource, ValidationError, provenance_dict
from atlas.registry import ProjectRecord, ProjectRegistry, RegisteredSource
from atlas.retrieval import RetrievalHit, Retriever
from atlas.semantic_retrieval import EmbeddingClient, EmbeddingConfig


_MAX_SOURCE_DETAIL_CHARS = 128 * 1024


class AtlasService:
    """Bounded Phase 1 operator workflow: register → source → sync → rebuild."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.registry = ProjectRegistry(self.data_root)
        self.snapshot_root = self.data_root / SNAPSHOT_DIRNAME
        self.import_root = self.data_root / IMPORT_DIRNAME
        self.projections = ProjectionStore(
            self.data_root / "projections",
            snapshot_root=self.snapshot_root,
        )

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

    def import_personal_markdown(
        self,
        project_id: str,
        *,
        source_id: str,
        source_path: str,
        title: str | None = None,
        enabled: bool = True,
    ) -> RegisteredSource:
        """Copy one relative Markdown path from the fixed Atlas import root."""
        with data_root_write_lock(self.data_root):
            project = self.registry.get(project_id)
            if source_id in project.sources:
                raise ValidationError(
                    f"duplicate source_id in project {project_id}: {source_id}"
                )
            text = read_allowlisted_markdown(self.import_root, source_path)
            published = publish_snapshot(self.snapshot_root, project_id, source_id, text)
            try:
                return self.registry.add_personal_snapshot(
                    project_id,
                    source_id=source_id,
                    source_path=source_path,
                    enabled=enabled,
                    title=title,
                )
            except Exception:
                published.unlink(missing_ok=True)
                raise

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

    def source_detail(self, project_id: str, source_id: str) -> dict[str, Any]:
        """Return one registered source plus bounded validated projection detail."""
        project = self.registry.get(project_id)
        source = project.sources.get(source_id)
        if source is None:
            raise ValidationError(f"unknown source_id: {source_id}")

        record = next(
            (
                item
                for item in self.projection_records(project_id)
                if item.get("source_id") == source_id
            ),
            None,
        )
        projection_state = str((record or {}).get("sync_state") or "missing").upper()
        result: dict[str, Any] = {
            "project_id": project_id,
            "repository": project.repository,
            "source": {
                "source_id": source.source_id,
                "source_path": source.source_path,
                "ref": source.ref or project.default_ref,
                "enabled": source.enabled,
                "media_type": source.media_type,
                "title": source.title,
                "provider": source.provider,
                "source_class": source.source_class,
            },
            "projection": {
                "state": projection_state,
                "content_digest": (record or {}).get("content_digest"),
                "source_revision": (record or {}).get("source_revision"),
                "fetched_at": (record or {}).get("fetched_at"),
                "identity": None,
                "provenance": None,
                "body": None,
                "body_truncated": False,
            },
        }
        if record is None or record.get("sync_state") not in {"success", "unchanged", "ok"}:
            return result

        matches = [
            projection
            for projection in iter_validated_projections(self.projections, project_id)
            if projection.identity.partition("@")[0] == source_id
        ]
        if len(matches) != 1:
            raise ValidationError("source projection identity is ambiguous")

        projection = matches[0]
        _header, separator, body = projection.text.partition("\n---\n")
        if not separator:
            raise ValidationError("source projection body is unavailable")
        body = body.strip()
        truncated = len(body) > _MAX_SOURCE_DETAIL_CHARS
        if truncated:
            body = body[:_MAX_SOURCE_DETAIL_CHARS]
        result["projection"].update(
            {
                "identity": projection.identity,
                "provenance": dict(provenance_dict(projection.provenance)),
                "body": body,
                "body_truncated": truncated,
            }
        )
        return result

    def engineering_system_observation(self, project_id: str) -> dict[str, Any]:
        """Read Engineering System adoption metadata only from validated local projections."""
        project = self.registry.get(project_id)
        matches = [
            projection
            for projection in iter_validated_projections(self.projections, project_id)
            if projection.provenance.engineering_authority
            and projection.provenance.repository == project.repository
            and projection.provenance.source_path == project.engineering_metadata_path
        ]
        if not matches:
            return {
                "state": "UNKNOWN",
                "detail": "no validated Engineering System metadata projection",
                "source_identity": None,
                "source_revision": None,
                "version": None,
                "baseline": None,
                "mode": None,
                "ci_mode": None,
            }
        if len(matches) != 1:
            return {
                "state": "UNAVAILABLE",
                "detail": "Engineering System metadata projection is ambiguous",
                "source_identity": None,
                "source_revision": None,
                "version": None,
                "baseline": None,
                "mode": None,
                "ci_mode": None,
            }
        projection = matches[0]
        _header, separator, body = projection.text.partition("\n---\n")
        if not separator:
            raise ValidationError("Engineering System metadata projection body is unavailable")
        adoption = parse_adoption_yaml(body.strip(), source_path=project.engineering_metadata_path)
        assert_adoption_project_consistency(adoption, project_id=project_id)
        return {
            "state": "OBSERVED",
            "detail": "validated local Engineering System metadata projection",
            "source_identity": projection.identity,
            "source_revision": projection.provenance.source_revision,
            "version": adoption.engineering_system_version,
            "baseline": adoption.engineering_system_baseline,
            "mode": adoption.engineering_system_mode,
            "ci_mode": adoption.engineering_system_ci_mode,
        }

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

    def decision_detail(
        self,
        decision_id: str,
        project_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return deterministic ADR target/backlink navigation within an explicit scope."""
        normalized = str(decision_id).upper()
        if re.fullmatch(r"ADR-[0-9]{4,}", normalized) is None:
            raise ValidationError("invalid decision_id")
        overview = self.intelligence_overview(project_ids)
        targets = overview["decision_targets"].get(normalized, [])
        backlinks = [
            item
            for item in overview["items"]
            if item["kind"] == "decision_backlink" and item["value"] == normalized
        ]
        if not targets and not backlinks:
            raise ValidationError(f"unknown decision_id: {normalized}")
        return {
            "decision_id": normalized,
            "derived": True,
            "canonical": False,
            "targets": targets,
            "backlinks": backlinks,
        }

    def intelligence_overview(self, project_ids: list[str] | None = None) -> dict[str, Any]:
        """Build a cross-project deterministic intelligence overview.

        UI callers may omit project_ids to use all enabled registered projects.
        AI/MCP callers should pass an explicit scope list.
        """
        if project_ids is None:
            projects = [item for item in self.list_projects() if item.enabled]
        else:
            if (
                not isinstance(project_ids, list)
                or not project_ids
                or len(project_ids) > 32
                or any(not isinstance(item, str) or not item.strip() for item in project_ids)
            ):
                raise ValidationError("project_ids must be a non-empty list of at most 32 project IDs")
            normalized = sorted(set(project_ids))
            projects = []
            for project_id in normalized:
                project = self.registry.get(project_id)
                if not project.enabled:
                    raise ValidationError(f"project is disabled: {project_id}")
                projects.append(project)
        payload = derived_intelligence_payload(
            self.projections,
            [item.project_id for item in projects],
        )
        counts: dict[str, dict[str, int]] = {
            item.project_id: {
                "concept_heading": 0,
                "cross_project_link": 0,
                "decision_backlink": 0,
                "unanswered_question": 0,
                "contradiction_evidence": 0,
                "knowledge_gap": 0,
            }
            for item in projects
        }
        for item in payload["items"]:
            project_counts = counts.get(item["source_project_id"])
            if project_counts is not None and item["kind"] in project_counts:
                project_counts[item["kind"]] += 1
        knowledge_gaps: list[dict[str, str]] = []
        for project in projects:
            records = {
                str(record.get("source_id") or ""): record
                for record in self.projection_records(project.project_id)
            }
            project_gaps = []
            for source_id, source in sorted(project.sources.items()):
                if not source.enabled:
                    continue
                sync_state = str((records.get(source_id) or {}).get("sync_state") or "missing")
                if sync_state in {"success", "unchanged", "ok"}:
                    continue
                project_gaps.append(
                    {
                        "project_id": project.project_id,
                        "source_id": source_id,
                        "source_path": source.source_path,
                        "source_class": source.source_class,
                        "sync_state": sync_state,
                    }
                )
            counts[project.project_id]["knowledge_gap"] = len(project_gaps)
            knowledge_gaps.extend(project_gaps)
        totals = {
            key: sum(project_counts[key] for project_counts in counts.values())
            for key in (
                "concept_heading",
                "cross_project_link",
                "decision_backlink",
                "unanswered_question",
                "contradiction_evidence",
                "knowledge_gap",
            )
        }
        summary = {
            "state": "OBSERVED",
            "detail": (
                f"{len(projects)} enabled project(s) · "
                f"{totals['concept_heading']} concept anchor(s) · "
                f"{totals['cross_project_link']} cross-project link(s) · "
                f"{totals['decision_backlink']} ADR backlink(s) · "
                f"{totals['unanswered_question']} unanswered question(s) · "
                f"{totals['knowledge_gap']} configured-source gap(s) · "
                f"{totals['contradiction_evidence']} explicit contradiction evidence item(s)"
            ),
            "totals": totals,
        }
        return {
            **payload,
            "summary": summary,
            "knowledge_gaps": knowledge_gaps,
            "projects": [
                {
                    "project_id": project.project_id,
                    "display_name": project.display_name,
                    "repository": project.repository,
                    "counts": counts[project.project_id],
                }
                for project in projects
            ],
        }

    def project_intelligence(self, project_id: str) -> dict[str, Any]:
        """Build deterministic Phase 4 intelligence for one registered project."""
        project = self.registry.get(project_id)
        projects = [item for item in self.list_projects() if item.enabled]
        payload = derived_intelligence_payload(
            self.projections,
            [item.project_id for item in projects],
        )
        items = [
            item
            for item in payload["items"]
            if item["source_project_id"] == project_id
        ]
        records = {
            str(record.get("source_id") or ""): record
            for record in self.projection_records(project_id)
        }
        gaps = []
        for source_id, source in sorted(project.sources.items()):
            if not source.enabled:
                continue
            record = records.get(source_id)
            sync_state = str((record or {}).get("sync_state") or "missing")
            if sync_state in {"success", "unchanged", "ok"}:
                continue
            gaps.append(
                {
                    "source_id": source_id,
                    "source_path": source.source_path,
                    "source_class": source.source_class,
                    "sync_state": sync_state,
                }
            )
        backlinks: dict[str, list[dict[str, str]]] = {}
        concepts: dict[str, list[dict[str, str]]] = {}
        for item in items:
            row = {
                "source_project_id": item["source_project_id"],
                "source_identity": item["source_identity"],
            }
            if item["kind"] == "decision_backlink":
                backlinks.setdefault(item["value"], []).append(dict(row))
            elif item["kind"] == "concept_heading":
                concepts.setdefault(item["value"], []).append(dict(row))
        referenced_adrs = set(backlinks)
        repository_entities = {
            repository: rows
            for repository, rows in payload["entities"]["repositories"].items()
            if any(row["project_id"] == project_id for row in rows)
        }
        source_entities = {
            source_path: [
                row for row in rows if row["project_id"] == project_id
            ]
            for source_path, rows in payload["entities"]["source_paths"].items()
            if any(row["project_id"] == project_id for row in rows)
        }
        item_counts = {
            kind: sum(1 for item in items if item["kind"] == kind)
            for kind in (
                "concept_heading",
                "cross_project_link",
                "decision_backlink",
                "unanswered_question",
                "contradiction_evidence",
            )
        }
        summary = {
            "state": "OBSERVED",
            "detail": (
                f"{item_counts['concept_heading']} concept anchor(s) · "
                f"{item_counts['cross_project_link']} cross-project link(s) · "
                f"{item_counts['decision_backlink']} ADR backlink(s) · "
                f"{item_counts['unanswered_question']} unanswered question(s) · "
                f"{len(gaps)} configured-source gap(s) · "
                f"{item_counts['contradiction_evidence']} explicit contradiction evidence item(s)"
            ),
            "counts": {
                **item_counts,
                "knowledge_gap": len(gaps),
            },
        }
        return {
            "project_id": project_id,
            "repository": project.repository,
            "derived": True,
            "canonical": False,
            "summary": summary,
            "contradictions": payload["contradictions"],
            "knowledge_gaps": gaps,
            "concept_index": dict(sorted(concepts.items())),
            "entities": {
                "repositories": dict(sorted(repository_entities.items())),
                "source_paths": dict(sorted(source_entities.items())),
                "decisions": {
                    adr: payload["decision_targets"].get(adr, [])
                    for adr in sorted(referenced_adrs)
                },
            },
            "decision_backlinks": dict(sorted(backlinks.items())),
            "decision_targets": {
                adr: payload["decision_targets"].get(adr, [])
                for adr in sorted(referenced_adrs)
            },
            "unanswered_questions": payload["unanswered_questions"].get(project_id, []),
            "items": items,
        }
