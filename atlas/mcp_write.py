"""Approved, bounded MCP writes. No general GitHub or canonical content editing.

Neither path modifies an arbitrary repository. The MCP deployment must
separately opt in to each tool family; enabling production OAuth scopes and
the feature flags is a trusted production/permission-boundary operation.
"""

from __future__ import annotations

import re
from typing import Any

from atlas.github_sync import FetchFn, fetch_github_file
from atlas.projection import _contains_unsafe_github_projection_secret
from atlas.provenance import CanonicalSource, ValidationError, validate_source_path
from atlas.secrets import contains_unsafe_secret
from atlas.service import AtlasService

SOURCE_WRITE_SCOPE = "atlas.sources.write"

_SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_TITLE_LIMIT = 128
# Narrow deliberate engineering source allowlist. Do not accept source roots,
# arbitrary git refs, symlinks, or user-selected GitHub/HTTP repositories.
_ALLOWED_MARKDOWN_PREFIXES = ("docs/",)


def ingest_candidate_memory(
    service: AtlasService,
    *,
    project_id: str,
    input_kind: str,
    observed_at: str,
    candidates: list[dict[str, object]],
    workstream: str = "",
) -> dict[str, object]:
    """Use the existing versioned, scoped, non-authoritative candidate store."""
    if not isinstance(project_id, str):
        raise ValidationError("project_id is invalid")
    if input_kind not in {"INTERACTION_SUMMARY", "RUN_SUMMARY"}:
        raise ValidationError("only owner interaction/run summaries may be submitted")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise ValidationError("candidate batch must contain between 1 and 8 items")
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ValidationError("candidate item is invalid")
        if item.get("candidate_class") not in {
            "OWNER_PREFERENCE", "VALIDATED_FINDING", "LESSON_LEARNED",
            "RUN_SUMMARY", "FUTURE_IDEA", "REFERENCE_FACT",
        }:
            raise ValidationError("candidate class is invalid")
        # Canonical event evidence is *not* inferred from an AI statement.
        # Existing candidate normalization also enforces bounds and secrets.
    from atlas.memory_candidates import _parse_time
    from datetime import datetime, timedelta, timezone
    if not isinstance(observed_at, str):
        raise ValidationError("observed_at is invalid")
    when = _parse_time(observed_at)
    if when > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValidationError("candidate observed_at cannot be in the future")
    service.registry.get(project_id)
    return service.ingest_memory_candidates(
        project_id=project_id,
        input_kind=input_kind,
        observed_at=observed_at,
        candidates=candidates,
        workstream=workstream or None,
    )


def register_project_github_markdown(
    service: AtlasService,
    *,
    project_id: str,
    source_id: str,
    source_path: str,
    title: str,
    fetch: FetchFn | None = None,
) -> dict[str, Any]:
    """Register one *already-canonical* Markdown file and sync it.

    Only the registered project's default GitHub repository/ref can be used.
    Preflight fetch/secret validation occurs before registry mutation. Exact
    retries are idempotent; conflicting IDs or paths are rejected. A failed
    sync is not reported as success and cannot confer canonical authority.
    """
    if not isinstance(project_id, str):
        raise ValidationError("invalid project_id")
    project = service.registry.get(project_id)
    if not project.enabled:
        raise ValidationError("project is disabled")
    if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise ValidationError("invalid source_id")
    if not isinstance(source_path, str):
        raise ValidationError("invalid source_path")
    try:
        validate_source_path(source_path)
    except ValidationError as exc:
        raise ValidationError("invalid source_path") from None
    if (
        not source_path.startswith(_ALLOWED_MARKDOWN_PREFIXES)
        or not source_path.endswith(".md")
        or len(source_path) > 240
        or any(part.startswith(".") for part in source_path.split("/"))
    ):
        raise ValidationError("only non-hidden docs/*.md paths are allowed")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > _TITLE_LIMIT
        or "\n" in title or "\r" in title
        or contains_unsafe_secret(title)
    ):
        raise ValidationError("source title is invalid")

    existing = project.sources.get(source_id)
    if existing is not None and (
        existing.provider != "github"
        or existing.source_path != source_path
        or (existing.ref is not None and existing.ref != project.default_ref)
        or not existing.enabled
    ):
        raise ValidationError("source_id conflicts with registered source")

    source = CanonicalSource(
        source_id=source_id,
        project_id=project_id,
        provider="github",
        repository=project.repository,
        ref=project.default_ref,
        source_path=source_path,
        title=existing.title if existing else title,
    )

    # Fetch the exact whitelisted GitHub contents path once, before mutation.
    # Do not leak user-supplied paths, HTTP errors, tokens or remote exceptions
    # back through MCP.
    try:
        fetched = (fetch or fetch_github_file)(source, None)
    except Exception as exc:
        raise ValidationError("canonical GitHub source fetch failed") from exc
    if (
        not isinstance(fetched.content, str)
        or not fetched.content.strip()
        or len(fetched.content.encode("utf-8")) > 1_000_000
        or not isinstance(fetched.source_revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", fetched.source_revision)
        or _contains_unsafe_github_projection_secret(fetched.content)
    ):
        raise ValidationError("canonical GitHub source preflight failed")

    if existing is None:
        # Recheck inside the repository registry lock; concurrent identical
        # writes can race, but cannot change the identity that is validated.
        try:
            service.add_source(
                project_id, source_id=source_id, source_path=source_path,
                title=title,
            )
        except ValidationError:
            concurrent = service.registry.get(project_id).sources.get(source_id)
            if concurrent is None or concurrent.source_path != source_path or concurrent.provider != "github" or not concurrent.enabled:
                raise
            existing = concurrent
    record = service.projections.sync_one(
        source, fetch=lambda _src, _token: fetched,
    )
    if record.sync_state not in {"success", "unchanged"}:
        raise ValidationError("canonical source sync failed")
    return {
        "state": "UNCHANGED" if existing is not None and record.sync_state == "unchanged" else "SYNCED",
        "canonical": False,
        "derived": True,
        "project_id": project_id,
        "source_id": source_id,
        "repository": project.repository,
        "ref": project.default_ref,
        "source_path": source_path,
        "source_revision": record.source_revision,
        "sync_state": record.sync_state,
    }
