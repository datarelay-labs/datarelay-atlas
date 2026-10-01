"""Deterministic, non-authoritative intelligence derived from validated projections."""
from __future__ import annotations
from dataclasses import dataclass
import re
from atlas.projection import ProjectionStore
from atlas.projection_retrieval import iter_validated_projections
from atlas.provenance import provenance_dict

_REPO = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])")
_BODY_SEPARATOR = "\n---\n"
_ADR = re.compile(r"(?i)(?:docs/decisions/)?(ADR-[0-9]{4,})(?:[A-Za-z0-9_.-]*)")
_ADR_PATH = re.compile(r"(?i)(?:^|/)(ADR-[0-9]{4,})(?:[-_.][^/]*)?\.md$")
_QUESTION = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:TODO(?:\([^)]*\))?\s*[:：]\s*(.+)|QUESTION\s*[:：]\s*(.+))\s*$")
_CONTRADICTION = re.compile(r"(?im)^\s*(?:[-*]\s*)?CONTRADICTION\s*[:：]\s*(.+)\s*$")
_MERGE_CONFLICT = re.compile(r"(?m)^<<<<<<<[^\n]*$.*?^=======$.*?^>>>>>>>[^\n]*$", re.DOTALL)
_HEADING = re.compile(r"(?m)^#{1,6}\s+([^#\n][^\n]{0,199})\s*$")
_MAX_ITEMS = 500

@dataclass(frozen=True)
class DerivedItem:
    kind: str
    value: str
    source_project_id: str
    source_identity: str
    provenance: dict[str, object]

def _build_derived_intelligence(
    store: ProjectionStore,
    project_ids: list[str],
    *,
    repository_project_ids: list[str] | None = None,
) -> tuple[list[DerivedItem], bool]:
    items: list[DerivedItem] = []
    seen: set[tuple[str,str,str,str]] = set()
    contradiction_detected = False
    projections = [
        projection
        for project_id in sorted(set(project_ids))
        for projection in iter_validated_projections(store, project_id)
    ]
    repository_scope = (
        sorted(set(repository_project_ids))
        if repository_project_ids is not None
        else sorted(set(project_ids))
    )
    repository_projections = (
        projections
        if repository_scope == sorted(set(project_ids))
        else [
            projection
            for project_id in repository_scope
            for projection in iter_validated_projections(store, project_id)
        ]
    )
    known_repositories = {
        projection.provenance.repository
        for projection in repository_projections
        if projection.provenance.engineering_authority
    }
    for projection in projections:
        prov = provenance_dict(projection.provenance)
        own_repo = str(prov["repository"])
        body = (
            projection.text.split(_BODY_SEPARATOR, 1)[1]
            if _BODY_SEPARATOR in projection.text
            else ""
        )
        contradiction_values = sorted(
            {
                match.strip()[:240]
                for match in _CONTRADICTION.findall(body)
                if match.strip()
            }
        )
        merge_conflict = _MERGE_CONFLICT.search(body) is not None
        if contradiction_values or merge_conflict:
            contradiction_detected = True

        if len(items) >= _MAX_ITEMS:
            continue

        for repo in sorted(known_repositories):
            if repo == own_repo:
                continue
            pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(repo)}(?=$|[\s),;:!?])|(?<![A-Za-z0-9_.-]){re.escape(repo)}\.(?=$|\s)"
            if re.search(pattern, body):
                _append(items, seen, "cross_project_link", repo, projection.project_id, projection.identity, prov)
        for heading in sorted({match.strip() for match in _HEADING.findall(body) if match.strip()}):
            _append(items, seen, "concept_heading", heading, projection.project_id, projection.identity, prov)
        for adr in sorted({match.upper() for match in _ADR.findall(body)}):
            _append(items, seen, "decision_backlink", adr, projection.project_id, projection.identity, prov)
        for match in _QUESTION.finditer(body):
            value = next((part.strip() for part in match.groups() if part and part.strip()), "")
            if value:
                _append(items, seen, "unanswered_question", value[:240], projection.project_id, projection.identity, prov)
        for value in contradiction_values:
            _append(items, seen, "contradiction_evidence", value, projection.project_id, projection.identity, prov)
        if merge_conflict:
            _append(items, seen, "contradiction_evidence", "unresolved merge-conflict markers", projection.project_id, projection.identity, prov)
    return sorted(items, key=_sort_key), contradiction_detected


def build_derived_intelligence(store: ProjectionStore, project_ids: list[str]) -> list[DerivedItem]:
    items, _contradiction_detected = _build_derived_intelligence(store, project_ids)
    return items

def _append(items, seen, kind, value, project_id, identity, provenance):
    key=(kind,value,project_id,identity)
    if key in seen or len(items) >= _MAX_ITEMS: return
    seen.add(key)
    items.append(DerivedItem(kind=kind,value=value,source_project_id=project_id,source_identity=identity,provenance=dict(provenance)))

def _sort_key(item: DerivedItem):
    return (item.kind,item.value,item.source_project_id,item.source_identity)

def _index_row_sort_key(row: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(row.get("source_project_id") or row.get("project_id") or ""),
        str(row.get("source_identity") or ""),
        str(row.get("value") or ""),
    )


def _bounded_index(
    index: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    bounded: dict[str, list[dict[str, object]]] = {}
    remaining = _MAX_ITEMS
    for key in sorted(index):
        if remaining <= 0:
            break
        rows = sorted(index[key], key=_index_row_sort_key)
        selected = rows[:remaining]
        if selected:
            bounded[key] = selected
            remaining -= len(selected)
    return bounded


def derived_intelligence_payload(
    store: ProjectionStore,
    project_ids: list[str],
    *,
    repository_project_ids: list[str] | None = None,
) -> dict[str, object]:
    normalized_ids = sorted(set(project_ids))
    items, contradiction_detected = _build_derived_intelligence(
        store,
        normalized_ids,
        repository_project_ids=repository_project_ids,
    )
    backlinks: dict[str, list[dict[str, str]]] = {}
    concepts: dict[str, list[dict[str, str]]] = {}
    questions: dict[str, list[dict[str, str]]] = {}
    for item in items:
        row = {
            "source_project_id": item.source_project_id,
            "source_identity": item.source_identity,
        }
        if item.kind == "decision_backlink":
            backlinks.setdefault(item.value, []).append(dict(row))
        elif item.kind == "concept_heading":
            concepts.setdefault(item.value, []).append(dict(row))
        elif item.kind == "unanswered_question":
            questions.setdefault(item.source_project_id, []).append(
                {"value": item.value, "source_identity": item.source_identity}
            )

    decision_targets: dict[str, list[dict[str, object]]] = {}
    repository_entities: dict[str, list[dict[str, str]]] = {}
    source_entities: dict[str, list[dict[str, str]]] = {}
    for project_id in normalized_ids:
        for projection in iter_validated_projections(store, project_id):
            prov = dict(provenance_dict(projection.provenance))
            source_entities.setdefault(projection.provenance.source_path, []).append(
                {
                    "project_id": projection.project_id,
                    "source_identity": projection.identity,
                    "source_class": projection.provenance.source_class,
                }
            )
            if projection.provenance.engineering_authority:
                repository_entities.setdefault(projection.provenance.repository, []).append(
                    {
                        "project_id": projection.project_id,
                        "source_identity": projection.identity,
                        "source_path": projection.provenance.source_path,
                    }
                )
            match = _ADR_PATH.search(projection.provenance.source_path)
            if not projection.provenance.engineering_authority or match is None:
                continue
            adr = match.group(1).upper()
            decision_targets.setdefault(adr, []).append(
                {
                    "project_id": projection.project_id,
                    "source_identity": projection.identity,
                    "provenance": prov,
                }
            )

    backlinks = _bounded_index(backlinks)
    concepts = _bounded_index(concepts)
    questions = _bounded_index(questions)
    decision_targets = _bounded_index(decision_targets)
    repository_entities = _bounded_index(repository_entities)
    source_entities = _bounded_index(source_entities)

    contradiction_items = [
        item for item in items if item.kind == "contradiction_evidence"
    ]
    return {
        "derived": True,
        "canonical": False,
        "contradictions": {
            "state": "DETECTED" if contradiction_detected else "NONE_OBSERVED",
            "semantic_state": "UNKNOWN",
            "detail": (
                (
                    f"{len(contradiction_items)} retained explicit contradiction evidence item(s); "
                    "additional evidence may be outside the bounded display set"
                )
                if contradiction_detected
                else "no explicit contradiction markers observed; semantic consistency remains unknown"
            ),
            "items": [
                {
                    "value": item.value,
                    "source_project_id": item.source_project_id,
                    "source_identity": item.source_identity,
                    "provenance": item.provenance,
                }
                for item in contradiction_items
            ],
        },
        "concept_index": dict(sorted(concepts.items())),
        "entities": {
            "repositories": dict(sorted(repository_entities.items())),
            "source_paths": dict(sorted(source_entities.items())),
            "decisions": dict(sorted(decision_targets.items())),
        },
        "decision_backlinks": dict(sorted(backlinks.items())),
        "decision_targets": dict(sorted(decision_targets.items())),
        "unanswered_questions": dict(sorted(questions.items())),
        "items": [
            {
                "kind": item.kind,
                "value": item.value,
                "source_project_id": item.source_project_id,
                "source_identity": item.source_identity,
                "provenance": item.provenance,
            }
            for item in items
        ],
    }
