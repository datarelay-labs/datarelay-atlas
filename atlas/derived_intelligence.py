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
_QUESTION = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:TODO(?:\([^)]*\))?\s*[:：]\s*(.+)|QUESTION\s*[:：]\s*(.+))\s*$")
_HEADING = re.compile(r"(?m)^#{1,6}\s+([^#\n][^\n]{0,199})\s*$")
_MAX_ITEMS = 500

@dataclass(frozen=True)
class DerivedItem:
    kind: str
    value: str
    source_project_id: str
    source_identity: str
    provenance: dict[str, object]

def build_derived_intelligence(store: ProjectionStore, project_ids: list[str]) -> list[DerivedItem]:
    items: list[DerivedItem] = []
    seen: set[tuple[str,str,str,str]] = set()
    projections = [projection for project_id in sorted(set(project_ids)) for projection in iter_validated_projections(store, project_id)]
    known_repositories = {projection.provenance.repository for projection in projections if projection.provenance.engineering_authority}
    for projection in projections:
        prov = provenance_dict(projection.provenance)
        own_repo = str(prov["repository"])
        body = projection.text.split(_BODY_SEPARATOR, 1)[1] if _BODY_SEPARATOR in projection.text else ""
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
        if len(items) >= _MAX_ITEMS:
            return sorted(items, key=_sort_key)
    return sorted(items, key=_sort_key)

def _append(items, seen, kind, value, project_id, identity, provenance):
    key=(kind,value,project_id,identity)
    if key in seen or len(items) >= _MAX_ITEMS: return
    seen.add(key)
    items.append(DerivedItem(kind=kind,value=value,source_project_id=project_id,source_identity=identity,provenance=dict(provenance)))

def _sort_key(item: DerivedItem):
    return (item.kind,item.value,item.source_project_id,item.source_identity)

def derived_intelligence_payload(store: ProjectionStore, project_ids: list[str]) -> dict[str, object]:
    items=build_derived_intelligence(store,project_ids)
    backlinks: dict[str, list[dict[str, str]]] = {}
    for item in items:
        if item.kind != "decision_backlink":
            continue
        backlinks.setdefault(item.value, []).append({"source_project_id": item.source_project_id, "source_identity": item.source_identity})
    for value in backlinks.values():
        value.sort(key=lambda row: (row["source_project_id"], row["source_identity"]))
    return {"derived":True,"canonical":False,"contradictions":{"state":"UNKNOWN","detail":"no contradiction inference in deterministic v1"},
        "decision_backlinks": dict(sorted(backlinks.items())),
        "items":[{"kind":i.kind,"value":i.value,"source_project_id":i.source_project_id,"source_identity":i.source_identity,"provenance":i.provenance} for i in items]}
