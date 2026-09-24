"""Deterministic semantic retrieval over an injected embeddings transport."""

from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import threading
import unittest
import urllib.error
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from atlas.cli import main
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.semantic_retrieval import (
    EmbeddingConfig,
    EmbeddingSemanticProvider,
    HttpEmbeddingClient,
    SemanticDocument,
    cosine_similarity,
    embeddings_url,
    parse_embedding_vectors,
    validate_embedding_config,
)
from atlas.service import AtlasService

UNIQUE_PHRASE = "phase2-semantic-charter-quill"
NEIGHBOR_PHRASE = "phase2-semantic-neighbor-body"
OTHER_PHRASE = "phase2-semantic-other-project"


class ScriptedEmbedder:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if len(self.vectors) != len(texts):
            raise AssertionError(f"expected {len(self.vectors)} texts, got {len(texts)}")
        return [list(vector) for vector in self.vectors]


class _Response:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self, limit: int = -1) -> bytes:
        if limit is None or limit < 0:
            return self.payload
        return self.payload[:limit]

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _config(**overrides: object) -> EmbeddingConfig:
    values: dict[str, object] = {
        "endpoint": "http://127.0.0.1:8080",
        "model": "bge-test",
    }
    values.update(overrides)
    return EmbeddingConfig(**values)  # type: ignore[arg-type]


class CosineAndPayloadTests(unittest.TestCase):
    def test_cosine_is_deterministic(self):
        self.assertEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)
        with self.assertRaises(ValidationError) as mismatch:
            cosine_similarity([1.0], [1.0, 0.0])
        self.assertEqual(str(mismatch.exception), "embedding dimension mismatch")
        for left, right in (
            ([0.0, 0.0], [1.0, 0.0]),
            ([1.0, 0.0], [0.0, 0.0]),
        ):
            with self.assertRaises(ValidationError) as zero_norm:
                cosine_similarity(left, right)
            self.assertEqual(
                str(zero_norm.exception),
                "embedding endpoint returned a zero-norm embedding vector",
            )

    def test_payload_orders_by_index_and_rejects_bad_vectors(self):
        ordered = parse_embedding_vectors(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
            2,
        )
        self.assertEqual(ordered, [[1.0, 0.0], [0.0, 1.0]])

        cases = [
            ({"data": []}, 1, "embedding endpoint returned no embeddings"),
            ({"data": [{"index": 0, "embedding": []}]}, 1, "embedding endpoint returned no embeddings"),
            ({"data": [{"embedding": [1.0, 0.0]}]}, 1, "embedding endpoint returned no embeddings"),
            ({"data": [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [0.0, 1.0]}]}, 2, "embedding endpoint returned no embeddings"),
            ({"data": [{"index": 2, "embedding": [1.0]}]}, 1, "embedding endpoint returned no embeddings"),
            ({"data": [{"index": 0, "embedding": [True]}]}, 1, "embedding endpoint returned a non-numeric embedding value"),
            ({"data": [{"index": 0, "embedding": [float("nan")]}]}, 1, "embedding endpoint returned a non-finite embedding value"),
            (
                {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 0.0]}]},
                2,
                "embedding dimension mismatch",
            ),
        ]
        for payload, expected, message in cases:
            with self.assertRaises(ValidationError) as caught:
                parse_embedding_vectors(payload, expected)
            self.assertEqual(str(caught.exception), message)

    def test_provider_ranks_cosine_then_identity(self):
        documents = [
            SemanticDocument("demo", "note@main", "note", "note body"),
            SemanticDocument("demo", "charter@main", "charter", "charter body"),
        ]
        embedder = ScriptedEmbedder([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
        provider = EmbeddingSemanticProvider(
            project_id="demo",
            documents=documents,
            client=embedder,
            query_prefix="q:",
            document_prefix="d:",
        )
        hits = provider.search("demo", " query ", limit=8)
        self.assertEqual([hit.path for hit in hits], ["charter@main", "note@main"])
        self.assertEqual(hits[0].score, 1.0)
        self.assertEqual(hits[1].score, 0.0)
        self.assertEqual(embedder.calls[0][0], "q:query")
        self.assertTrue(all(text.startswith("d:") for text in embedder.calls[0][1:]))

        tied = ScriptedEmbedder([[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]])
        tied_provider = EmbeddingSemanticProvider(
            project_id="demo",
            documents=documents,
            client=tied,
        )
        tied_hits = tied_provider.search("demo", "same", limit=1)
        self.assertEqual([hit.path for hit in tied_hits], ["charter@main"])

    def test_provider_skips_embed_without_query_or_scope(self):
        embedder = ScriptedEmbedder([[1.0]])
        provider = EmbeddingSemanticProvider(
            project_id="demo",
            documents=[SemanticDocument("demo", "charter@main", "charter", "body")],
            client=embedder,
        )
        self.assertEqual(provider.search("demo", "   ", limit=8), [])
        self.assertEqual(provider.search("other", "query", limit=8), [])
        self.assertEqual(embedder.calls, [])


class ProjectionSemanticTests(unittest.TestCase):
    def _seed(self, tmp: str) -> AtlasService:
        svc = AtlasService(Path(tmp))
        svc.register_project(
            project_id="datarelay-atlas",
            repository="datarelay-labs/datarelay-atlas",
        )
        svc.add_source(
            "datarelay-atlas",
            source_id="charter",
            source_path="docs/product/PRODUCT-CHARTER.md",
        )
        svc.add_source(
            "datarelay-atlas",
            source_id="neighbor",
            source_path="docs/product/NEIGHBOR.md",
        )
        svc.add_source(
            "datarelay-atlas",
            source_id="disabled-note",
            source_path="docs/product/DISABLED.md",
            enabled=False,
        )

        def fetch(source, token):  # noqa: ARG001
            if source.source_id == "charter":
                return FetchedSource(content=f"# Charter\n\n{UNIQUE_PHRASE}\n", source_revision="rev-charter")
            if source.source_id == "neighbor":
                return FetchedSource(content=f"# Neighbor\n\n{NEIGHBOR_PHRASE}\n", source_revision="rev-neighbor")
            return FetchedSource(content="should-not-index", source_revision="rev-disabled")

        svc.sync_project("datarelay-atlas", fetch=fetch)
        return svc

    def test_keyword_only_when_semantic_is_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            hits = svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].match, "classic")
            self.assertEqual(hits[0].provenance["source_revision"], "rev-charter")

    def test_hybrid_and_semantic_hits_keep_provenance_and_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            svc.register_project(project_id="other-proj", repository="datarelay-labs/datarelay-atlas")
            svc.add_source("other-proj", source_id="note", source_path="docs/product/OTHER.md")

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content=OTHER_PHRASE, source_revision="rev-other")

            svc.sync_project("other-proj", fetch=fetch)
            # query, charter@main, neighbor@main. Charter is closest and a keyword hit.
            embedder = ScriptedEmbedder([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
            hits = svc.search(
                "datarelay-atlas",
                UNIQUE_PHRASE,
                embedding=_config(),
                embedder=embedder,
            )
            self.assertEqual(
                [hit.path for hit in hits],
                [
                    "docs/product/PRODUCT-CHARTER.md",
                    "docs/product/NEIGHBOR.md",
                ],
            )
            charter, neighbor = hits
            self.assertEqual(charter.match, "both")
            self.assertEqual(charter.provenance["source_revision"], "rev-charter")
            self.assertEqual(charter.provenance["source_path"], "docs/product/PRODUCT-CHARTER.md")
            self.assertEqual(charter.project_id, "datarelay-atlas")
            self.assertFalse(charter.provenance["canonical"])
            self.assertTrue(charter.provenance["derived"])
            self.assertEqual(neighbor.match, "semantic")
            self.assertEqual(neighbor.provenance["source_revision"], "rev-neighbor")
            self.assertEqual(neighbor.provenance["source_path"], "docs/product/NEIGHBOR.md")
            self.assertGreater(charter.score, neighbor.score)
            joined = "\n".join(text for call in embedder.calls for text in call)
            self.assertIn(UNIQUE_PHRASE, joined)
            self.assertIn(NEIGHBOR_PHRASE, joined)
            self.assertNotIn("should-not-index", joined)
            self.assertNotIn(OTHER_PHRASE, joined)

            semantic_only = ScriptedEmbedder([[0.0, 1.0], [1.0, 0.0], [0.0, 1.0]])
            only = svc.search(
                "datarelay-atlas",
                "no-keyword-overlap",
                embedding=_config(query_prefix="query: ", document_prefix="doc: "),
                embedder=semantic_only,
            )
            self.assertEqual(len(only), 2)
            self.assertTrue(all(hit.match == "semantic" for hit in only))
            self.assertEqual(semantic_only.calls[0][0], "query: no-keyword-overlap")
            self.assertTrue(all(text.startswith("doc: ") for text in semantic_only.calls[0][1:]))

    def test_shared_source_path_stays_independently_ranked(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = AtlasService(Path(tmp))
            svc.register_project(project_id="datarelay-atlas", repository="datarelay-labs/datarelay-atlas")
            svc.add_source(
                "datarelay-atlas",
                source_id="main-src",
                source_path="docs/shared.md",
                ref="main",
            )
            svc.add_source(
                "datarelay-atlas",
                source_id="rel-src",
                source_path="docs/shared.md",
                ref="v1",
            )

            def fetch(source, token):  # noqa: ARG001
                if source.source_id == "main-src":
                    return FetchedSource(content="main body", source_revision="rev-main")
                return FetchedSource(content="release body", source_revision="rev-release")

            svc.sync_project("datarelay-atlas", fetch=fetch)
            embedder = ScriptedEmbedder([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
            hits = svc.search(
                "datarelay-atlas",
                "semantic-only-query",
                embedding=_config(),
                embedder=embedder,
            )
            by_ref = {hit.provenance["ref"]: hit for hit in hits}
            self.assertEqual(set(by_ref), {"main", "v1"})
            self.assertEqual(by_ref["main"].provenance["source_revision"], "rev-main")
            self.assertEqual(by_ref["v1"].provenance["source_revision"], "rev-release")
            self.assertTrue(all(hit.match == "semantic" for hit in hits))

    def test_integrity_failure_does_not_embed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            charter = next(row for row in svc.projection_records("datarelay-atlas") if row["source_id"] == "charter")
            (svc.projections.root / charter["projection_path"]).write_bytes(b"TAMPERED")

            class Boom:
                def embed(self, texts: list[str]) -> list[list[float]]:
                    raise AssertionError("embedded before integrity check")

            with self.assertRaises(ValidationError) as mismatch:
                svc.search(
                    "datarelay-atlas",
                    UNIQUE_PHRASE,
                    embedding=_config(),
                    embedder=Boom(),
                )
            self.assertEqual(
                str(mismatch.exception),
                "projection bytes digest mismatch: datarelay-atlas/charter",
            )

    def test_embedder_failures_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            mismatch = ScriptedEmbedder([[1.0, 0.0], [1.0], [1.0, 0.0]])
            with self.assertRaises(ValidationError) as dim:
                svc.search("datarelay-atlas", "query", embedding=_config(), embedder=mismatch)
            self.assertEqual(str(dim.exception), "embedding dimension mismatch")
            non_finite = ScriptedEmbedder([[1.0], [math.inf], [1.0]])
            with self.assertRaises(ValidationError) as finite:
                svc.search("datarelay-atlas", "query", embedding=_config(), embedder=non_finite)
            self.assertEqual(
                str(finite.exception),
                "embedding endpoint returned a non-finite embedding value",
            )
            zero_query = ScriptedEmbedder([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
            with self.assertRaises(ValidationError) as zero_query_error:
                svc.search("datarelay-atlas", "query", embedding=_config(), embedder=zero_query)
            self.assertEqual(
                str(zero_query_error.exception),
                "embedding endpoint returned a zero-norm embedding vector",
            )
            zero_doc = ScriptedEmbedder([[1.0, 0.0], [0.0, 0.0], [0.0, 1.0]])
            with self.assertRaises(ValidationError) as zero_doc_error:
                svc.search("datarelay-atlas", "query", embedding=_config(), embedder=zero_doc)
            self.assertEqual(
                str(zero_doc_error.exception),
                "embedding endpoint returned a zero-norm embedding vector",
            )


class HttpEmbeddingClientTests(unittest.TestCase):
    def test_embeddings_url_accepts_root_v1_and_explicit_path(self):
        explicit = "http://127.0.0.1:8080/v1/embeddings"
        self.assertEqual(embeddings_url("http://127.0.0.1:8080"), explicit)
        self.assertEqual(embeddings_url("http://127.0.0.1:8080/"), explicit)
        self.assertEqual(embeddings_url("http://127.0.0.1:8080/v1"), explicit)
        self.assertEqual(embeddings_url("http://127.0.0.1:8080/v1/"), explicit)
        self.assertEqual(embeddings_url(explicit), explicit)
        self.assertEqual(embeddings_url(explicit + "/"), explicit)
        self.assertNotIn("/v1/v1/embeddings", embeddings_url("https://embeddings.internal/v1"))

    def test_endpoint_rejects_query_fragment_and_userinfo(self):
        for endpoint in (
            "http://127.0.0.1:8080?model=bge",
            "http://127.0.0.1:8080/v1?",
            "http://127.0.0.1:8080/v1/embeddings#section",
            "http://127.0.0.1:8080#",
            "http://user:pass@127.0.0.1:8080/v1",
            "http://user@127.0.0.1:8080",
            "http://@127.0.0.1:8080/v1",
        ):
            with self.assertRaises(ValidationError) as caught:
                validate_embedding_config(_config(endpoint=endpoint))
            self.assertEqual(str(caught.exception), "embedding endpoint must be an http(s) URL")

    def test_posts_openai_compatible_request_and_sorts_indexes(self):
        captured: dict[str, object] = {}

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["content_type"] = request.get_header("Content-type")
            return _Response(
                json.dumps(
                    {
                        "data": [
                            {"index": 1, "embedding": [0.0, 1.0]},
                            {"index": 0, "embedding": [1.0, 0.0]},
                        ]
                    }
                ).encode("utf-8")
            )

        client = HttpEmbeddingClient(_config(timeout_seconds=5), opener=opener)
        self.assertEqual(client.embed(["query", "doc"]), [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(captured["url"], "http://127.0.0.1:8080/v1/embeddings")
        self.assertEqual(captured["timeout"], 5.0)
        self.assertEqual(captured["body"], {"model": "bge-test", "input": ["query", "doc"]})
        self.assertEqual(captured["content_type"], "application/json")

        def full_path(request, timeout=None):  # noqa: ARG001
            captured["full"] = request.full_url
            return _Response(json.dumps({"data": [{"index": 0, "embedding": [1.0]}]}).encode("utf-8"))

        full = HttpEmbeddingClient(
            _config(endpoint="http://127.0.0.1:8080/v1/embeddings"),
            opener=full_path,
        )
        self.assertEqual(full.embed(["q"]), [[1.0]])
        self.assertEqual(captured["full"], "http://127.0.0.1:8080/v1/embeddings")

        def v1_base(request, timeout=None):  # noqa: ARG001
            captured["v1"] = request.full_url
            return _Response(json.dumps({"data": [{"index": 0, "embedding": [1.0]}]}).encode("utf-8"))

        v1 = HttpEmbeddingClient(_config(endpoint="http://127.0.0.1:8080/v1"), opener=v1_base)
        self.assertEqual(v1.embed(["q"]), [[1.0]])
        self.assertEqual(captured["v1"], "http://127.0.0.1:8080/v1/embeddings")

    def test_default_batch_size_splits_thirty_three_inputs_in_order(self):
        captured: list[list[str]] = []

        def opener(request, timeout=None):  # noqa: ARG001
            body = json.loads(request.data.decode("utf-8"))
            inputs = body["input"]
            self.assertLessEqual(len(inputs), 32)
            captured.append(list(inputs))
            data = [
                {"index": len(inputs) - 1 - offset, "embedding": [float(inputs[len(inputs) - 1 - offset]), 1.0]}
                for offset in range(len(inputs))
            ]
            return _Response(json.dumps({"data": data}).encode("utf-8"))

        texts = [str(index) for index in range(33)]
        client = HttpEmbeddingClient(_config(), opener=opener)
        self.assertEqual(client.max_batch_size, 32)
        self.assertEqual(client.embed(texts), [[float(index), 1.0] for index in range(33)])
        self.assertEqual(captured, [texts[:32], texts[32:]])

        calls = {"count": 0}

        def mismatch(request, timeout=None):  # noqa: ARG001
            body = json.loads(request.data.decode("utf-8"))
            calls["count"] += 1
            width = 2 if calls["count"] == 1 else 1
            data = [{"index": index, "embedding": [1.0] * width} for index in range(len(body["input"]))]
            return _Response(json.dumps({"data": data}).encode("utf-8"))

        with self.assertRaises(ValidationError) as cross_batch:
            HttpEmbeddingClient(_config(), opener=mismatch).embed(texts)
        self.assertEqual(str(cross_batch.exception), "embedding dimension mismatch")

        for batch_size in (0, -1, 33, True, 1.5):
            with self.assertRaises(ValidationError) as invalid:
                HttpEmbeddingClient(_config(), opener=opener, max_batch_size=batch_size)  # type: ignore[arg-type]
            self.assertEqual(
                str(invalid.exception),
                "embedding client batch size must be a positive integer at most 32",
            )

    def test_transport_and_payload_failures(self):
        def fail(request, timeout=None):  # noqa: ARG001
            raise urllib.error.URLError(TimeoutError("timed out"))

        client = HttpEmbeddingClient(_config(), opener=fail)
        with self.assertRaises(ValidationError) as timed:
            client.embed(["q"])
        self.assertEqual(str(timed.exception), "embedding endpoint request failed")

        def http_error(request, timeout=None):  # noqa: ARG001
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8080/v1/embeddings",
                503,
                "unavailable",
                HTTPMessage(),
                io.BytesIO(b""),
            )

        with self.assertRaises(ValidationError) as status:
            HttpEmbeddingClient(_config(), opener=http_error).embed(["q"])
        self.assertEqual(str(status.exception), "embedding endpoint returned HTTP 503")

        def malformed(request, timeout=None):  # noqa: ARG001
            return _Response(b"not-json")

        with self.assertRaises(ValidationError) as bad_json:
            HttpEmbeddingClient(_config(), opener=malformed).embed(["q"])
        self.assertEqual(str(bad_json.exception), "embedding endpoint returned malformed JSON")

        def empty(request, timeout=None):  # noqa: ARG001
            return _Response(json.dumps({"data": []}).encode("utf-8"))

        with self.assertRaises(ValidationError) as missing:
            HttpEmbeddingClient(_config(), opener=empty).embed(["q"])
        self.assertEqual(str(missing.exception), "embedding endpoint returned no embeddings")

        def numeric(request, timeout=None):  # noqa: ARG001
            return _Response(json.dumps({"data": [{"index": 0, "embedding": [1, "x"]}]}).encode("utf-8"))

        with self.assertRaises(ValidationError) as non_numeric:
            HttpEmbeddingClient(_config(), opener=numeric).embed(["q"])
        self.assertEqual(str(non_numeric.exception), "embedding endpoint returned a non-numeric embedding value")

        def oversized(request, timeout=None):  # noqa: ARG001
            return _Response(b'{"data":[{"index":0,"embedding":[1]}]}')

        with self.assertRaises(ValidationError) as huge:
            HttpEmbeddingClient(_config(), opener=oversized, max_response_bytes=8).embed(["q"])
        self.assertEqual(str(huge.exception), "embedding endpoint returned an oversized response")

        def rejected(request, timeout=None):  # noqa: ARG001
            return _Response(b"nope", status=500)

        with self.assertRaises(ValidationError) as non_2xx:
            HttpEmbeddingClient(_config(), opener=rejected).embed(["q"])
        self.assertEqual(str(non_2xx.exception), "embedding endpoint returned HTTP 500")

    def test_redirect_fails_closed(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/elsewhere")
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HttpEmbeddingClient(
                _config(endpoint=f"http://127.0.0.1:{server.server_address[1]}", timeout_seconds=2)
            )
            with self.assertRaises(ValidationError) as redirected:
                client.embed(["hello"])
            self.assertEqual(str(redirected.exception), "embedding endpoint returned HTTP 302")
        finally:
            server.shutdown()
            server.server_close()

    def test_config_and_cli_fail_closed_without_network(self):
        with self.assertRaises(ValidationError):
            HttpEmbeddingClient(_config(endpoint="http://user:pass@127.0.0.1:8080"))
        with self.assertRaises(ValidationError) as file_url:
            HttpEmbeddingClient(_config(endpoint="file:///tmp/embeddings"))
        self.assertEqual(str(file_url.exception), "embedding endpoint must be an http(s) URL")
        with self.assertRaises(ValidationError) as prefix:
            HttpEmbeddingClient(_config(query_prefix="q" * 257))
        self.assertIn("query prefix", str(prefix.exception))

        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--data-root",
                        tmp,
                        "search",
                        "datarelay-atlas",
                        "query",
                        "--embedding-model",
                        "bge-test",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn(
                "embedding endpoint is required when semantic retrieval is configured",
                stderr.getvalue(),
            )

            stderr_file = io.StringIO()
            with contextlib.redirect_stderr(stderr_file):
                file_code = main(
                    [
                        "--data-root",
                        tmp,
                        "search",
                        "datarelay-atlas",
                        "query",
                        "--embedding-endpoint",
                        "file:///tmp/embeddings",
                        "--embedding-model",
                        "bge-test",
                    ]
                )
            self.assertEqual(file_code, 1)
            self.assertIn("embedding endpoint must be an http(s) URL", stderr_file.getvalue())


if __name__ == "__main__":
    unittest.main()
