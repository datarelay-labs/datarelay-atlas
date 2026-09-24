"""Atlas semantic retrieval over a TEI-compatible embeddings endpoint.

Design gate for this slice:
- Goal: rank the same integrity-checked projections with an external
  ``/v1/embeddings`` endpoint and existing reciprocal-rank fusion.
- Non-goals: pgvector, a persistent vector store, cloud embedding APIs,
  cross-project search, and model-specific prefix rules.
- Contract: optional search CLI endpoint/model/prefix/timeout. Unconfigured
  search stays keyword-only.
- State: embeddings are request-scoped and are not stored.
- Security: operator-supplied http(s) endpoint, no secrets in Git, fail closed
  on transport and payload errors.
- Similarity: cosine similarity, higher first, then projection identity.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from atlas.provenance import ValidationError
from atlas.retrieval import SemanticHit, snippet
from atlas.security import require_project_scope

MAX_PREFIX_CHARS = 256
MAX_MODEL_CHARS = 256
MAX_TIMEOUT_SECONDS = 600.0
MAX_RESPONSE_BYTES = 8_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0

_NO_EMBEDDINGS = "embedding endpoint returned no embeddings"
_NON_NUMERIC = "embedding endpoint returned a non-numeric embedding value"
_NON_FINITE = "embedding endpoint returned a non-finite embedding value"
_ZERO_NORM = "embedding endpoint returned a zero-norm embedding vector"
_DIMENSION_MISMATCH = "embedding dimension mismatch"
_MALFORMED_JSON = "embedding endpoint returned malformed JSON"
_REQUEST_FAILED = "embedding endpoint request failed"


@dataclass(frozen=True)
class EmbeddingConfig:
    endpoint: str
    model: str
    query_prefix: str = ""
    document_prefix: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class SemanticDocument:
    project_id: str
    path: str
    title: str | None
    text: str


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def validate_embedding_config(config: EmbeddingConfig) -> EmbeddingConfig:
    """Reject unbounded or secret-bearing endpoint configuration."""
    if not isinstance(config.endpoint, str) or not isinstance(config.model, str):
        raise ValidationError("embedding endpoint must be an http(s) URL")
    endpoint = config.endpoint.strip()
    model = config.model.strip()
    parsed = urllib.parse.urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValidationError("embedding endpoint must be an http(s) URL")
    if not model or len(model) > MAX_MODEL_CHARS or any(ch.isspace() for ch in model):
        raise ValidationError("embedding model is required when semantic retrieval is configured")
    _validate_prefix("query", config.query_prefix)
    _validate_prefix("document", config.document_prefix)
    timeout = config.timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
        or float(timeout) > MAX_TIMEOUT_SECONDS
    ):
        raise ValidationError("embedding timeout must be a positive number")
    return EmbeddingConfig(
        endpoint=endpoint,
        model=model,
        query_prefix=config.query_prefix,
        document_prefix=config.document_prefix,
        timeout_seconds=float(timeout),
    )


def embedding_config_from_cli(
    *,
    endpoint: str | None,
    model: str | None,
    query_prefix: str | None,
    document_prefix: str | None,
    timeout_seconds: float,
) -> EmbeddingConfig | None:
    """Return None when search should stay keyword-only."""
    endpoint_text = (endpoint or "").strip()
    model_text = (model or "").strip()
    query_text = query_prefix or ""
    document_text = document_prefix or ""
    if not endpoint_text and not model_text and not query_text and not document_text:
        return None
    if not endpoint_text:
        raise ValidationError("embedding endpoint is required when semantic retrieval is configured")
    if not model_text:
        raise ValidationError("embedding model is required when semantic retrieval is configured")
    return validate_embedding_config(
        EmbeddingConfig(
            endpoint=endpoint_text,
            model=model_text,
            query_prefix=query_text,
            document_prefix=document_text,
            timeout_seconds=timeout_seconds,
        )
    )


def embeddings_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/v1/embeddings"):
        return base
    return base + "/v1/embeddings"


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Deterministic cosine similarity in ``[-1, 1]``.

    Zero-norm query or document vectors fail closed. Callers rank higher scores
    first and break ties by projection identity.
    """
    if len(left) != len(right) or not left:
        raise ValidationError(_DIMENSION_MISMATCH)
    dot = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if not math.isfinite(left_norm) or not math.isfinite(right_norm):
        raise ValidationError(_NON_FINITE)
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValidationError(_ZERO_NORM)
    score = dot / (left_norm * right_norm)
    if not math.isfinite(score):
        raise ValidationError(_NON_FINITE)
    if score > 1.0:
        return 1.0
    if score < -1.0:
        return -1.0
    return score


def parse_embedding_vectors(payload: object, expected: int) -> list[list[float]]:
    """Parse an OpenAI-compatible embeddings payload in input order."""
    if not isinstance(payload, dict):
        raise ValidationError(_NO_EMBEDDINGS)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise ValidationError(_NO_EMBEDDINGS)
    if expected == 0:
        return []
    by_index: dict[int, list[float]] = {}
    width: int | None = None
    for item in data:
        if not isinstance(item, dict):
            raise ValidationError(_NO_EMBEDDINGS)
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= expected:
            raise ValidationError(_NO_EMBEDDINGS)
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValidationError(_NO_EMBEDDINGS)
        if index in by_index:
            raise ValidationError(_NO_EMBEDDINGS)
        vector = [_embedding_component(value) for value in embedding]
        if width is None:
            width = len(vector)
        elif len(vector) != width:
            raise ValidationError(_DIMENSION_MISMATCH)
        by_index[index] = vector
    if len(by_index) != expected:
        raise ValidationError(_NO_EMBEDDINGS)
    return [by_index[index] for index in range(expected)]


class EmbeddingSemanticProvider:
    """Rank integrity-checked projection documents with injected embeddings."""

    def __init__(
        self,
        *,
        project_id: str,
        documents: Sequence[SemanticDocument],
        client: EmbeddingClient,
        query_prefix: str = "",
        document_prefix: str = "",
    ) -> None:
        require_project_scope(project_id)
        self.project_id = project_id
        self.client = client
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        ordered = tuple(sorted(documents, key=lambda doc: doc.path))
        for doc in ordered:
            if doc.project_id != project_id or not doc.path:
                raise ValidationError("projection provenance mismatch")
        self.documents = ordered

    def search(self, project_id: str, query: str, limit: int) -> list[SemanticHit]:
        require_project_scope(project_id)
        if project_id != self.project_id:
            return []
        if not isinstance(query, str) or not query.strip():
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        if not self.documents:
            return []
        inputs = [self.query_prefix + query.strip()]
        inputs.extend(self.document_prefix + doc.text for doc in self.documents)
        vectors = _coerce_vectors(self.client.embed(inputs), expected=len(inputs))
        ranked = [
            (cosine_similarity(vectors[0], vector), doc.path, doc)
            for doc, vector in zip(self.documents, vectors[1:])
        ]
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [
            SemanticHit(
                path=doc.path,
                title=doc.title,
                content=snippet(doc.text),
                score=score,
            )
            for score, _path, doc in ranked[:limit]
        ]


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise ValidationError(f"embedding endpoint returned HTTP {code}")


class HttpEmbeddingClient:
    """POST ``/v1/embeddings`` and fail closed on transport or payload errors."""

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        opener: Callable[..., object] | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.config = validate_embedding_config(config)
        self.max_response_bytes = max_response_bytes
        if opener is not None:
            self._open = opener
        else:
            built = urllib.request.build_opener(_RejectRedirect)

            def _open(request, timeout=None):
                return built.open(request, timeout=timeout)

            self._open = _open

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise ValidationError(_NO_EMBEDDINGS)
        if not texts:
            return []
        body = json.dumps(
            {"model": self.config.model, "input": texts},
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            embeddings_url(self.config.endpoint),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "datarelay-atlas-embeddings",
            },
            method="POST",
        )
        try:
            with self._open(request, timeout=self.config.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = getattr(response, "code", 200)
                if isinstance(status, bool) or not isinstance(status, int) or status < 200 or status >= 300:
                    code = status if isinstance(status, int) and not isinstance(status, bool) else "unknown"
                    raise ValidationError(f"embedding endpoint returned HTTP {code}")
                raw = response.read(self.max_response_bytes + 1)
        except ValidationError:
            raise
        except urllib.error.HTTPError as exc:
            raise ValidationError(f"embedding endpoint returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ValidationError(_REQUEST_FAILED) from exc
        if len(raw) > self.max_response_bytes:
            raise ValidationError("embedding endpoint returned an oversized response")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(_MALFORMED_JSON) from exc
        return parse_embedding_vectors(payload, len(texts))


def _validate_prefix(label: str, prefix: object) -> None:
    if not isinstance(prefix, str) or len(prefix) > MAX_PREFIX_CHARS or "\n" in prefix or "\r" in prefix:
        raise ValidationError(
            f"embedding {label} prefix must be a single line of at most {MAX_PREFIX_CHARS} characters"
        )


def _embedding_component(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(_NON_NUMERIC)
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(_NON_FINITE)
    return number


def _coerce_vectors(vectors: object, *, expected: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise ValidationError(_NO_EMBEDDINGS)
    parsed: list[list[float]] = []
    width: int | None = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValidationError(_NO_EMBEDDINGS)
        numbers = [_embedding_component(value) for value in vector]
        if width is None:
            width = len(numbers)
        elif len(numbers) != width:
            raise ValidationError(_DIMENSION_MISMATCH)
        parsed.append(numbers)
    return parsed
