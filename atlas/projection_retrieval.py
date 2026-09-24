"""Build a project-scoped keyword Retriever from ProjectionStore documents.

The index is rebuilt in memory from successful projection bytes and metadata.
No separate search index is persisted.
"""

from __future__ import annotations

import hashlib
import json

from atlas.projection import ProjectionStore
from atlas.registry import REF_RE, SOURCE_ID_RE
from atlas.provenance import (
    PROJECT_ID_RE,
    REPO_RE,
    Provenance,
    ValidationError,
    rendered_projection_identity,
    validate_source_path,
)
from atlas.retrieval import IndexedDocument, KeywordIndex, Retriever, normalize_path
from atlas.security import require_project_scope
from atlas.semantic_retrieval import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingSemanticProvider,
    HttpEmbeddingClient,
    SemanticDocument,
    validate_embedding_config,
)

INDEXABLE_SYNC_STATES = frozenset({"success", "unchanged", "ok"})
_REQUIRED_PROVENANCE = (
    "project_id",
    "provider",
    "repository",
    "ref",
    "source_path",
    "source_revision",
)


def build_keyword_retriever(
    store: ProjectionStore,
    project_id: str,
    *,
    embedding: EmbeddingConfig | None = None,
    embedder: EmbeddingClient | None = None,
) -> Retriever:
    """Index one project's successful projections with metadata provenance.

    ``embedding`` opts into in-memory semantic ranking over the same records.
    Omit it to keep keyword-only retrieval.
    """
    require_project_scope(project_id)
    if embedding is not None:
        embedding = validate_embedding_config(embedding)
    elif embedder is not None:
        raise ValidationError("embedding endpoint is required when semantic retrieval is configured")
    records = _load_records(store, project_id)
    index = KeywordIndex()
    provenance_by_path: dict[tuple[str, str], Provenance] = {}
    semantic_documents: list[SemanticDocument] = []
    seen_identities: set[str] = set()

    for meta in records:
        if meta.get("sync_state") not in INDEXABLE_SYNC_STATES:
            continue
        label = _record_label(meta, project_id)
        provenance = _provenance_from_record(meta, project_id=project_id, label=label)
        path = normalize_path(provenance.source_path)
        identity = _projection_identity(meta, provenance, label)
        if identity in seen_identities:
            raise ValidationError(f"duplicate projection identity: {identity}")
        seen_identities.add(identity)
        text = _read_projection_text(
            store,
            meta.get("projection_path"),
            label,
            meta.get("content_digest"),
        )
        _require_rendered_identity(text, provenance, label)
        index.add(
            IndexedDocument(
                project_id=project_id,
                path=identity,
                title=path,
                text=text,
                provenance=provenance,
            )
        )
        provenance_by_path[(project_id, identity)] = provenance
        semantic_documents.append(
            SemanticDocument(
                project_id=project_id,
                path=identity,
                title=path,
                text=text,
            )
        )

    semantic = None
    if embedding is not None:
        client = embedder or HttpEmbeddingClient(embedding)
        semantic = EmbeddingSemanticProvider(
            project_id=project_id,
            documents=semantic_documents,
            client=client,
            query_prefix=embedding.query_prefix,
            document_prefix=embedding.document_prefix,
        )
    return Retriever(index, semantic=semantic, provenance_by_path=provenance_by_path)


def _load_records(store: ProjectionStore, project_id: str) -> list[dict]:
    try:
        return store.list_records(project_id=project_id)
    except json.JSONDecodeError as exc:
        raise ValidationError("corrupt projection metadata") from exc


def _projection_identity(meta: dict, provenance: Provenance, label: str) -> str:
    """Index key from the canonical projection source_id and configured ref.

    ``source_path`` is not unique. Distinct registry sources may share it.
    """
    source_id = meta.get("source_id")
    if not isinstance(source_id, str) or not SOURCE_ID_RE.match(source_id):
        raise ValidationError(f"malformed projection provenance: {label}")
    if not REF_RE.match(provenance.ref):
        raise ValidationError(f"malformed projection provenance: {label}")
    expected_rel = f"{provenance.project_id}/{source_id}.md"
    if meta.get("projection_path") != expected_rel:
        raise ValidationError(f"projection provenance mismatch: {label}")
    return f"{source_id}@{provenance.ref}"


def _record_label(meta: dict, project_id: str) -> str:
    source_id = meta.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        return f"{project_id}/{source_id}"
    key = meta.get("key")
    if isinstance(key, str) and key.strip():
        return key
    return project_id


def _provenance_from_record(meta: dict, *, project_id: str, label: str) -> Provenance:
    raw = meta.get("provenance")
    if not isinstance(raw, dict):
        raise ValidationError(f"malformed projection provenance: {label}")
    if meta.get("project_id") != project_id:
        raise ValidationError(f"malformed projection provenance: {label}")
    values: dict[str, str] = {}
    for field in _REQUIRED_PROVENANCE:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"malformed projection provenance: {label}")
        values[field] = value
    if values["project_id"] != project_id:
        raise ValidationError(f"malformed projection provenance: {label}")
    if meta.get("source_revision") != values["source_revision"]:
        raise ValidationError(f"projection provenance mismatch: {label}")
    if raw.get("canonical") is not False or raw.get("derived") is not True:
        raise ValidationError(f"malformed projection provenance: {label}")
    if values["provider"] != "github" or not REPO_RE.match(values["repository"]):
        raise ValidationError(f"malformed projection provenance: {label}")
    if not PROJECT_ID_RE.match(values["project_id"]):
        raise ValidationError(f"malformed projection provenance: {label}")
    try:
        validate_source_path(values["source_path"])
    except ValidationError as exc:
        raise ValidationError(f"malformed projection provenance: {label}") from exc
    return Provenance(
        project_id=values["project_id"],
        provider=values["provider"],
        repository=values["repository"],
        ref=values["ref"],
        source_path=values["source_path"],
        source_revision=values["source_revision"],
        derived=True,
        canonical=False,
    )


def _read_projection_text(
    store: ProjectionStore,
    rel: object,
    label: str,
    content_digest: object,
) -> str:
    if not isinstance(rel, str) or not rel.strip():
        raise ValidationError(f"projection bytes missing: {label}")
    if rel.startswith("/") or "\\" in rel or any(part in {"", ".", ".."} for part in rel.split("/")):
        raise ValidationError(f"projection bytes missing: {label}")
    root = store.root.resolve()
    try:
        candidate = (store.root / rel).resolve()
    except OSError as exc:
        raise ValidationError(f"projection bytes unreadable: {label}") from exc
    if not candidate.is_relative_to(root):
        raise ValidationError(f"projection bytes missing: {label}")
    if not candidate.is_file():
        raise ValidationError(f"projection bytes missing: {label}")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(f"projection bytes unreadable: {label}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if not isinstance(content_digest, str) or digest != content_digest:
        raise ValidationError(f"projection bytes digest mismatch: {label}")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError(f"projection bytes unreadable: {label}") from exc


def _require_rendered_identity(text: str, provenance: Provenance, label: str) -> None:
    """Returned identity must match the digest-bound rendered projection contract."""
    try:
        rendered = rendered_projection_identity(text)
    except ValidationError as exc:
        raise ValidationError(f"projection provenance mismatch: {label}") from exc
    expected = {
        "project_id": provenance.project_id,
        "provider": provenance.provider,
        "repository": provenance.repository,
        "ref": provenance.ref,
        "source_path": provenance.source_path,
        "source_revision": provenance.source_revision,
    }
    if rendered != expected:
        raise ValidationError(f"projection provenance mismatch: {label}")
