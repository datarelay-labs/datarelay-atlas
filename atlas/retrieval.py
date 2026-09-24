"""Project-scoped keyword retrieval and hybrid RRF fusion.

Semantic backends are EXTERNAL-DEPENDENCY providers; Athena repo is not used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from atlas.provenance import Provenance, provenance_dict
from atlas.security import require_project_scope

RRF_K = 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+")


def normalize_path(path: str) -> str:
    return path.strip().strip("/")


def snippet(text: str, limit: int = 400) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


@dataclass(frozen=True)
class ClassicHit:
    path: str
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SemanticHit:
    path: str
    title: str | None = None
    content: str | None = None
    score: float = 0.0
    headings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MergedHit:
    score: float
    match: str
    path: str
    title: str | None
    content: str
    headings: dict[str, str]


@dataclass(frozen=True)
class RetrievalHit:
    project_id: str
    path: str
    title: str | None
    content: str
    match: str
    score: float
    provenance: dict[str, object]
    # Projection identity (`source_id@ref`). `path` stays the user-visible source path.
    identity: str = ""


class SemanticProvider(Protocol):
    def search(self, project_id: str, query: str, limit: int) -> list[SemanticHit]:
        ...


class NullSemanticProvider:
    def search(self, project_id: str, query: str, limit: int) -> list[SemanticHit]:
        return []


def merge_search_hits(
    classic: list[ClassicHit],
    semantic: list[SemanticHit],
    limit: int,
) -> list[MergedHit]:
    """Reciprocal Rank Fusion over path keys (PoC-parity behavior, Atlas-native code)."""

    @dataclass
    class Bucket:
        classic: ClassicHit | None = None
        classic_rank: int | None = None
        semantic: list[SemanticHit] = field(default_factory=list)
        semantic_rank: int | None = None

    pages: dict[str, Bucket] = {}

    def bucket(key: str) -> Bucket:
        rec = pages.get(key)
        if rec is None:
            rec = Bucket()
            pages[key] = rec
        return rec

    for i, hit in enumerate(classic):
        key = normalize_path(hit.path or "")
        if not key:
            continue
        rec = bucket(key)
        if rec.classic is None:
            rec.classic = hit
            rec.classic_rank = i + 1

    for i, hit in enumerate(semantic):
        key = normalize_path(hit.path or "")
        if not key:
            continue
        rec = bucket(key)
        rec.semantic.append(hit)
        if rec.semantic_rank is None:
            rec.semantic_rank = i + 1

    ranked = []
    for key, rec in pages.items():
        score = 0.0
        if rec.classic_rank is not None:
            score += 1.0 / (RRF_K + rec.classic_rank)
        if rec.semantic_rank is not None:
            score += 1.0 / (RRF_K + rec.semantic_rank)
        ranked.append((score, key, rec))
    ranked.sort(key=lambda item: item[0], reverse=True)

    results: list[MergedHit] = []
    for score, key, rec in ranked:
        if len(results) >= limit:
            break
        if rec.classic is not None and rec.semantic:
            match = "both"
        elif rec.classic is not None:
            match = "classic"
        else:
            match = "semantic"
        title = None
        content = ""
        headings: dict[str, str] = {}
        if rec.classic is not None:
            title = rec.classic.title
            content = rec.classic.description or ""
        if rec.semantic:
            sem = rec.semantic[0]
            title = title or sem.title
            content = sem.content or content
            headings = dict(sem.headings)
        results.append(
            MergedHit(
                score=score,
                match=match,
                path=key,
                title=title,
                content=content,
                headings=headings,
            )
        )
    return results


@dataclass
class IndexedDocument:
    project_id: str
    path: str
    title: str
    text: str
    provenance: Provenance


class KeywordIndex:
    def __init__(self) -> None:
        self._docs: list[IndexedDocument] = []

    def clear(self) -> None:
        self._docs.clear()

    def add(self, doc: IndexedDocument) -> None:
        self._docs.append(doc)

    def extend(self, docs: Iterable[IndexedDocument]) -> None:
        self._docs.extend(docs)

    def search(self, project_id: str, query: str, limit: int = 8) -> list[ClassicHit]:
        require_project_scope(project_id)
        tokens = [t.lower() for t in _TOKEN_RE.findall(query) if t.strip()]
        if not tokens:
            return []
        scored: list[tuple[int, IndexedDocument]] = []
        for doc in self._docs:
            if doc.project_id != project_id:
                continue
            hay = doc.text.lower()
            score = sum(hay.count(tok) for tok in tokens)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda item: item[0], reverse=True)
        hits: list[ClassicHit] = []
        for score, doc in scored[:limit]:
            hits.append(
                ClassicHit(
                    path=doc.path,
                    title=doc.title,
                    description=snippet(doc.text),
                )
            )
        return hits


class Retriever:
    def __init__(
        self,
        index: KeywordIndex,
        semantic: SemanticProvider | None = None,
        provenance_by_path: dict[tuple[str, str], Provenance] | None = None,
    ) -> None:
        self.index = index
        self.semantic = semantic or NullSemanticProvider()
        self.provenance_by_path = provenance_by_path or {}

    def search(self, project_id: str, query: str, limit: int = 8) -> list[RetrievalHit]:
        require_project_scope(project_id)
        classic = self.index.search(project_id, query, limit=limit)
        semantic = self.semantic.search(project_id, query, limit=limit)
        merged = merge_search_hits(classic, semantic, limit)
        out: list[RetrievalHit] = []
        for hit in merged:
            prov = self.provenance_by_path.get((project_id, hit.path))
            if prov is None:
                # Fail closed: retrieval without provenance is not returned.
                continue
            out.append(
                RetrievalHit(
                    project_id=project_id,
                    path=normalize_path(prov.source_path),
                    title=hit.title,
                    content=snippet(hit.content),
                    match=hit.match,
                    score=hit.score,
                    provenance=dict(provenance_dict(prov)),
                    identity=hit.path,
                )
            )
        return out
