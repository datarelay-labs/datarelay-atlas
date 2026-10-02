"""Bounded provider-neutral task-context bootstrap for approved AI clients."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from atlas.engineering_evidence import engineering_evidence_dashboard
from atlas.lifecycle_intelligence import lifecycle_view_payload
from atlas.projection import ProjectionStore
from atlas.projection_retrieval import build_keyword_retriever
from atlas.provenance import ValidationError
from atlas.registry import ProjectRecord, ProjectRegistry
from atlas.secrets import contains_unsafe_secret

SCHEMA_VERSION = 1
KIND = "atlas_task_context"
AUTHORITY = "DERIVED_READ_ONLY"
MAX_CONTEXT_BYTES = 24 * 1024
MAX_WORK_PACKETS = 6
MAX_KNOWLEDGE_REFS = 4
MAX_DETAIL_CHARS = 512
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURRENTNESS = {
    "OBSERVED": "CURRENT",
    "STALE": "STALE",
    "UNAVAILABLE": "UNAVAILABLE",
    "UNKNOWN": "UNKNOWN",
}

def resolve_task_project(
    registry: ProjectRegistry,
    *,
    project_id: str | None = None,
    repository: str | None = None,
) -> ProjectRecord:
    project_value = project_id.strip() if isinstance(project_id, str) else ""
    repository_value = repository.strip() if isinstance(repository, str) else ""
    if bool(project_value) == bool(repository_value):
        raise ValidationError("exactly one of project_id or repository is required")
    if project_value:
        project = registry.get(project_value)
    else:
        matches = [
            item
            for item in registry.list_projects()
            if item.repository == repository_value
        ]
        if not matches:
            raise ValidationError(f"unknown repository: {repository_value}")
        if len(matches) != 1:
            raise ValidationError(f"ambiguous repository: {repository_value}")
        project = matches[0]
    if not project.enabled:
        raise ValidationError(f"project is disabled: {project.project_id}")
    return project


def normalize_workstream(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("workstream must be a string")
    normalized = value.strip()
    if not normalized:
        return None
    if _WORKSTREAM.fullmatch(normalized) is None:
        raise ValidationError("workstream is invalid")
    return normalized


def _bounded_text(value: object, *, limit: int = MAX_DETAIL_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + "…[TRUNCATED]"


def _knowledge_query(workstream: str) -> str:
    return " ".join(part for part in re.split(r"[._-]+", workstream) if part)


def _knowledge_refs(
    projections: ProjectionStore,
    project_id: str,
    workstream: str | None,
) -> list[dict[str, object]]:
    if workstream is None:
        return []
    query = _knowledge_query(workstream)
    if not query:
        return []
    retriever = build_keyword_retriever(
        projections,
        project_id,
        source_classes=frozenset({"engineering"}),
    )
    hits = retriever.search(project_id, query, limit=MAX_KNOWLEDGE_REFS)
    refs: list[dict[str, object]] = []
    for hit in hits:
        provenance = dict(hit.provenance)
        refs.append(
            {
                "identity": hit.identity,
                "path": hit.path,
                "repository": provenance.get("repository"),
                "ref": provenance.get("ref"),
                "source_path": provenance.get("source_path"),
                "source_revision": provenance.get("source_revision"),
                "source_class": provenance.get("source_class", "engineering"),
                "canonical": provenance.get("canonical", False),
                "derived": provenance.get("derived", True),
            }
        )
    return refs


def _lifecycle_summary(payload: dict[str, object]) -> dict[str, object]:
    work = dict(payload["work"])
    canonical = [
        item
        for item in payload["work_packets"]
        if item["canonical"] is True
    ]
    priority = {"ACTIVE": 0, "BLOCKED": 1, "PAUSED": 2, "COMPLETE": 3}
    current_priority = min(
        (priority.get(str(item["packet_status"]), 9) for item in canonical),
        default=9,
    )
    selected = [
        item
        for item in canonical
        if priority.get(str(item["packet_status"]), 9) == current_priority
    ]
    current_heads = {str(item["head"]) for item in selected}
    current_head = next(iter(current_heads)) if len(current_heads) == 1 else None
    packets = [
        {
            "issue_number": item["issue_number"],
            "packet_status": item["packet_status"],
            "branch": item["branch"],
            "head": item["head"],
            "pr_number": item["pr_number"],
            "pr_state": item["pr_state"],
            "pr_head": item["pr_head"],
        }
        for item in selected[:MAX_WORK_PACKETS]
    ]
    if packets:
        detail_parts = []
        for item in packets:
            pr = (
                f"PR #{item['pr_number']} {item['pr_state']}"
                if item["pr_number"] is not None
                else "no PR"
            )
            detail_parts.append(
                f"AI Work #{item['issue_number']} {item['packet_status']} · "
                f"{item['branch']} · {item['head']} · {pr}"
            )
        if len(selected) > len(packets):
            detail_parts.append(f"{len(selected) - len(packets)} more current packet(s)")
        work_detail = _bounded_text(" ; ".join(detail_parts))
    else:
        work_detail = _bounded_text(work["detail"])

    def channel(name: str) -> dict[str, object]:
        raw = dict(payload[name])
        state = str(raw["state"])
        detail = _bounded_text(raw["detail"])
        candidate_head = raw["candidate_head"]
        if candidate_head is not None:
            if current_head is None:
                if state != "UNAVAILABLE":
                    state = "UNKNOWN"
                    detail = _bounded_text(
                        "evidence cannot be bound to a unique current task head"
                    )
            elif candidate_head != current_head and state != "UNAVAILABLE":
                previous_state = state
                state = "STALE_DIFFERENT_HEAD"
                detail = _bounded_text(
                    f"{previous_state} evidence is for different task head "
                    f"{candidate_head}; current task head is {current_head}"
                )
        return {
            "state": state,
            "detail": detail,
            "candidate_head": candidate_head,
            "evidence_ref": raw["evidence_ref"],
        }

    return {
        "work": {
            "state": work["state"],
            "detail": work_detail,
            "current_head": current_head,
            "canonical_packets": packets,
        },
        "channels": {
            name: channel(name)
            for name in (
                "ci",
                "tests",
                "release",
                "surface_reconciliation",
                "full_user_e2e",
            )
        },
    }


def _evidence_summary(
    dashboard: dict[str, object],
    *,
    workstream: str | None,
    current_head: str | None,
) -> tuple[dict[str, object], str]:
    project_rows = list(dashboard.get("projects") or [])
    project_row = project_rows[0] if project_rows else None
    families: list[dict[str, object]] = []
    current_workstream_matches = 0
    if isinstance(project_row, dict):
        for family in project_row.get("families") or []:
            latest = family.get("latest")
            latest_ref = None
            if isinstance(latest, dict):
                latest_ref = {
                    "record_id": latest.get("record_id"),
                    "artifact_kind": latest.get("artifact_kind"),
                    "workstream": latest.get("workstream"),
                    "subject_head": latest.get("subject_head"),
                    "observed_at": latest.get("observed_at"),
                    "summary_sha256": latest.get("summary_sha256"),
                }
                if (
                    workstream is not None
                    and current_head is not None
                    and family.get("state") == "CURRENT"
                    and latest.get("workstream") == workstream
                    and latest.get("subject_head") == current_head
                ):
                    current_workstream_matches += 1
            families.append(
                {
                    "family": family.get("family"),
                    "state": family.get("state"),
                    "current_head": family.get("current_head"),
                    "record_count": family.get("record_count"),
                    "latest": latest_ref,
                }
            )
    binding = "NOT_REQUESTED"
    if workstream is not None:
        binding = (
            "CURRENT_EVIDENCE_MATCH"
            if current_workstream_matches
            else "QUERY_HINT_ONLY"
        )
    return (
        {
            "state": dashboard.get("state"),
            "authority": dashboard.get("authority"),
            "producer_release": dashboard.get("producer_release"),
            "families": families,
        },
        binding,
    )


def build_task_context(
    data_root: Path,
    registry: ProjectRegistry,
    projections: ProjectionStore,
    *,
    project: ProjectRecord,
    workstream: str | None,
    engineering_system: dict[str, Any],
) -> dict[str, object]:
    workstream = normalize_workstream(workstream)
    lifecycle_raw = lifecycle_view_payload(Path(data_root), project.repository)
    lifecycle = _lifecycle_summary(lifecycle_raw)
    work_state = str(lifecycle["work"]["state"])
    currentness = _CURRENTNESS.get(work_state, "UNKNOWN")
    if work_state == "OBSERVED" and lifecycle["work"]["current_head"] is None:
        currentness = "UNKNOWN"
    evidence_raw = engineering_evidence_dashboard(
        Path(data_root),
        registry,
        project_ids=[project.project_id],
    )
    evidence, workstream_binding = _evidence_summary(
        evidence_raw,
        workstream=workstream,
        current_head=lifecycle["work"]["current_head"],
    )
    refs = _knowledge_refs(projections, project.project_id, workstream)
    query_hint = _knowledge_query(workstream) if workstream else None

    jit: list[dict[str, object]] = [
        {
            "tool": "get_project_intelligence",
            "reason": "retrieve derived links, decisions, contradictions and gaps only when needed",
            "argument_hint": {"project_id": project.project_id},
        },
        {
            "tool": "get_engineering_evidence",
            "reason": "retrieve full bounded Engineering System evidence metadata only when needed",
            "argument_hint": {"project_ids": [project.project_id]},
        },
    ]
    if query_hint:
        jit.insert(
            0,
            {
                "tool": "search_project",
                "reason": "retrieve source text relevant to the requested workstream only when needed",
                "argument_hint": {
                    "project_id": project.project_id,
                    "query": query_hint,
                    "limit": MAX_KNOWLEDGE_REFS,
                },
            },
        )
    if refs:
        jit.append(
            {
                "tool": "get_provenance",
                "reason": "revalidate one selected projection reference before relying on source identity",
                "argument_hint": {
                    "project_id": project.project_id,
                    "identity": refs[0]["identity"],
                },
            }
        )

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "project": {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "repository": project.repository,
            "default_ref": project.default_ref,
        },
        "request": {
            "workstream": workstream,
            "workstream_binding": workstream_binding,
        },
        "currentness": {
            "state": currentness,
            "basis": "CANONICAL_LIFECYCLE_EVIDENCE",
            "current_head": lifecycle["work"]["current_head"],
        },
        "engineering_system": engineering_system,
        "lifecycle": lifecycle,
        "engineering_evidence": evidence,
        "knowledge_refs": refs,
        "jit_retrieval": jit,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ValidationError("task context exceeds bounded response size")
    if contains_unsafe_secret(encoded):
        raise ValidationError("task context contains unsafe sensitive content")
    return payload
