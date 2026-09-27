"""Source path validation and derived-projection rendering (ADR-0003/0004)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
GITHUB_PROVIDER = "github"
LOCAL_MARKDOWN_PROVIDER = "local-markdown"
ENGINEERING_SOURCE_CLASS = "engineering"
PERSONAL_SOURCE_CLASS = "personal"
LOCAL_MARKDOWN_REPOSITORY = "local/markdown"
LOCAL_MARKDOWN_REF = "snapshot"


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
    source_class: str = ENGINEERING_SOURCE_CLASS


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
    source_class: str = ENGINEERING_SOURCE_CLASS
    engineering_authority: bool = True


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
    if not source.source_id.strip():
        raise ValidationError("source_id is required")
    validate_source_path(source.source_path)
    if source.provider == GITHUB_PROVIDER:
        if source.source_class != ENGINEERING_SOURCE_CLASS:
            raise ValidationError("github sources are engineering authority")
        if not REPO_RE.match(source.repository):
            raise ValidationError(f"invalid repository: {source.repository}")
        if not source.ref.strip():
            raise ValidationError("ref is required")
        return
    if source.provider == LOCAL_MARKDOWN_PROVIDER:
        if source.source_class != PERSONAL_SOURCE_CLASS:
            raise ValidationError("local markdown snapshots are personal/reference only")
        if source.repository != LOCAL_MARKDOWN_REPOSITORY:
            raise ValidationError("local markdown snapshots are not engineering repositories")
        if source.ref != LOCAL_MARKDOWN_REF:
            raise ValidationError("local markdown snapshots use the snapshot ref")
        if source.media_type != "text/markdown" or not source.source_path.endswith(".md"):
            raise ValidationError("unsupported media")
        return
    raise ValidationError(f"unsupported provider: {source.provider}")


def render_derived_document(source: CanonicalSource, body: str, source_revision: str) -> str:
    """Render a derived projection that cannot be mistaken for canonical truth."""
    validate_source(source)
    title = source.title or source.source_path
    if source.source_class == PERSONAL_SOURCE_CLASS:
        banner = [
            "> **Derived personal knowledge. Do not treat this projection as canonical engineering truth.**",
            "> Local Markdown snapshot; this projection is replaced by sync.",
        ]
        classification = [
            f"- Source class: `{source.source_class}`",
            "- Engineering authority: `false`",
        ]
    else:
        banner = [
            "> **Derived knowledge. Do not treat this projection as canonical.**",
            "> GitHub/repository artifacts remain authoritative; this projection is replaced by sync.",
        ]
        classification = []
    return "\n".join(
        [
            "<!-- atlas-derived: true -->",
            f"<!-- atlas-project-id: {source.project_id} -->",
            "",
            *banner,
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
            *classification,
            f"- Canonical: `false`",
            f"- Derived: `true`",
            "",
            "---",
            "",
            body.strip(),
            "",
        ]
    )


_RENDERED_IDENTITY_LABELS = (
    ("Project", "project_id"),
    ("Provider", "provider"),
    ("Repository", "repository"),
    ("Ref", "ref"),
    ("Source path", "source_path"),
    ("Source revision", "source_revision"),
)
_RENDERED_CLASS_LABELS = (
    ("Source class", "source_class"),
    ("Engineering authority", "engineering_authority"),
)


def rendered_projection_identity(text: str) -> dict[str, str]:
    """Read source identity from the derived document `render_derived_document` writes."""
    lines = text.splitlines()
    if not lines or lines[0] != "<!-- atlas-derived: true -->":
        raise ValidationError("projection bytes identity mismatch")
    try:
        separator = lines.index("---")
    except ValueError as exc:
        raise ValidationError("projection bytes identity mismatch") from exc
    header = lines[:separator]
    found: dict[str, str] = {}
    optional: dict[str, str] = {}
    for line in header:
        for label, key in _RENDERED_IDENTITY_LABELS:
            prefix = f"- {label}: `"
            if line.startswith(prefix) and line.endswith("`") and len(line) > len(prefix) + 1:
                found[key] = line[len(prefix):-1]
        for label, key in _RENDERED_CLASS_LABELS:
            prefix = f"- {label}: `"
            if line.startswith(prefix) and line.endswith("`") and len(line) > len(prefix) + 1:
                optional[key] = line[len(prefix):-1]
    expected = {key for _, key in _RENDERED_IDENTITY_LABELS}
    if set(found) != expected or any(not value.strip() for value in found.values()):
        raise ValidationError("projection bytes identity mismatch")
    if any(not value.strip() for value in optional.values()):
        raise ValidationError("projection bytes identity mismatch")
    found.update(optional)
    if f"<!-- atlas-project-id: {found['project_id']} -->" not in header:
        raise ValidationError("projection bytes identity mismatch")
    if "- Canonical: `false`" not in header or "- Derived: `true`" not in header:
        raise ValidationError("projection bytes identity mismatch")
    return found


def provenance_from_source(source: CanonicalSource, source_revision: str) -> Provenance:
    validate_source(source)
    authority = source.source_class == ENGINEERING_SOURCE_CLASS and source.provider == GITHUB_PROVIDER
    return Provenance(
        project_id=source.project_id,
        provider=source.provider,
        repository=source.repository,
        ref=source.ref,
        source_path=source.source_path,
        source_revision=source_revision,
        source_class=source.source_class,
        engineering_authority=authority,
    )


def provenance_dict(prov: Provenance) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "project_id": prov.project_id,
        "provider": prov.provider,
        "repository": prov.repository,
        "ref": prov.ref,
        "source_path": prov.source_path,
        "source_revision": prov.source_revision,
        "derived": prov.derived,
        "canonical": prov.canonical,
    }
    if prov.provider != GITHUB_PROVIDER or prov.source_class != ENGINEERING_SOURCE_CLASS:
        payload["source_class"] = prov.source_class
        payload["engineering_authority"] = prov.engineering_authority
    return payload
