"""Source path validation and derived-projection rendering (ADR-0003/0004)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class CanonicalSource:
    """Atlas public source configuration (no Wiki identity)."""

    source_id: str
    project_id: str
    provider: str
    repository: str
    ref: str
    source_path: str
    enabled: bool = True
    media_type: str = "text/markdown"
    title: str | None = None


@dataclass(frozen=True)
class Provenance:
    project_id: str
    provider: str
    repository: str
    ref: str
    source_path: str
    source_revision: str
    derived: bool = True
    canonical: bool = False


class ValidationError(ValueError):
    """Invalid source or path configuration."""


def validate_project_id(project_id: str) -> None:
    if not PROJECT_ID_RE.match(project_id):
        raise ValidationError(f"invalid project_id: {project_id}")


def validate_source_path(source_path: str) -> None:
    if not source_path or not source_path.strip():
        raise ValidationError("source_path is required")
    if source_path.startswith("/") or "\\" in source_path:
        raise ValidationError(f"invalid source_path: {source_path}")
    parts = source_path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValidationError(f"invalid source_path: {source_path}")


def validate_source(source: CanonicalSource) -> None:
    validate_project_id(source.project_id)
    if source.provider != "github":
        raise ValidationError(f"unsupported provider: {source.provider}")
    if not REPO_RE.match(source.repository):
        raise ValidationError(f"invalid repository: {source.repository}")
    if not source.ref.strip():
        raise ValidationError("ref is required")
    validate_source_path(source.source_path)
    if not source.source_id.strip():
        raise ValidationError("source_id is required")


def render_derived_document(source: CanonicalSource, body: str, source_revision: str) -> str:
    """Render a derived projection that cannot be mistaken for canonical truth."""
    validate_source(source)
    title = source.title or source.source_path
    return "\n".join(
        [
            "<!-- atlas-derived: true -->",
            f"<!-- atlas-project-id: {source.project_id} -->",
            "",
            "> **Derived knowledge. Do not treat this projection as canonical.**",
            "> GitHub/repository artifacts remain authoritative; this projection is replaced by sync.",
            "",
            f"# {title}",
            "",
            "## Provenance",
            "",
            f"- Project: `{source.project_id}`",
            f"- Provider: `{source.provider}`",
            f"- Repository: `{source.repository}`",
            f"- Ref: `{source.ref}`",
            f"- Source path: `{source.source_path}`",
            f"- Source revision: `{source_revision}`",
            f"- Canonical: `false`",
            f"- Derived: `true`",
            "",
            "---",
            "",
            body.strip(),
            "",
        ]
    )


def provenance_from_source(source: CanonicalSource, source_revision: str) -> Provenance:
    validate_source(source)
    return Provenance(
        project_id=source.project_id,
        provider=source.provider,
        repository=source.repository,
        ref=source.ref,
        source_path=source.source_path,
        source_revision=source_revision,
    )


def provenance_dict(prov: Provenance) -> Mapping[str, object]:
    return {
        "project_id": prov.project_id,
        "provider": prov.provider,
        "repository": prov.repository,
        "ref": prov.ref,
        "source_path": prov.source_path,
        "source_revision": prov.source_revision,
        "derived": prov.derived,
        "canonical": prov.canonical,
    }
