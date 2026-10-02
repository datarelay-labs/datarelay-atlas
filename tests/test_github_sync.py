import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from atlas.github_sync import FetchedSource, fetch_github_file
from atlas.projection import ProjectionStore
from atlas.provenance import CanonicalSource, ValidationError


SOURCE = CanonicalSource(
    source_id="charter",
    project_id="datarelay-atlas",
    provider="github",
    repository="datarelay-labs/datarelay-atlas",
    ref="main",
    source_path="docs/product/PRODUCT-CHARTER.md",
    title="Product Charter",
)


class GitHubSyncTests(unittest.TestCase):
    def test_fetch_decodes_base64_payload(self):
        payload = {
            "type": "file",
            "encoding": "base64",
            "content": "SGVsbG8gQ2Fub25pY2Fs",
            "sha": "deadbeef",
        }
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        def opener(request, timeout=30):  # noqa: ARG001
            self.assertIn("datarelay-labs/datarelay-atlas", request.full_url)
            return response

        fetched = fetch_github_file(SOURCE, token="unused", opener=opener)
        self.assertEqual(fetched.content, "Hello Canonical")
        self.assertEqual(fetched.source_revision, "deadbeef")

    def test_projection_rebuild_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectionStore(Path(tmp))

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content="# Body\n\nalpha", source_revision="rev1")

            first = store.sync_one(SOURCE, fetch=fetch)
            second = store.sync_one(SOURCE, fetch=fetch)
            self.assertEqual(first.sync_state, "success")
            self.assertEqual(second.sync_state, "unchanged")
            self.assertEqual(first.content_digest, second.content_digest)
            text = (Path(tmp) / first.projection_path).read_text(encoding="utf-8")
            self.assertIn("Derived knowledge", text)
            self.assertIn("rev1", text)
            self.assertNotIn("wiki_path", text)

    def test_fetch_error_records_fail_closed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectionStore(Path(tmp))

            def fetch(source, token):  # noqa: ARG001
                raise ValidationError("GitHub returned HTTP 404")

            record = store.sync_one(SOURCE, fetch=fetch)
            self.assertEqual(record.sync_state, "error")
            # Stale projection must not be marked newly current.
            docs = store.list_documents(SOURCE.project_id)
            self.assertEqual(docs, [])

    def test_secret_like_github_body_is_rejected_before_projection_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProjectionStore(root)
            secret_value = "sk-" + ("A" * 24)

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(
                    content=f"# Unsafe\n\nOPENAI_API_KEY={secret_value}\n",
                    source_revision="unsafe-rev",
                )

            record = store.sync_one(SOURCE, fetch=fetch)
            self.assertEqual(record.sync_state, "error")
            self.assertEqual(record.projection_path, "")
            self.assertFalse((root / store.projection_key(SOURCE)).exists())
            self.assertEqual(store.list_documents(SOURCE.project_id), [])
            durable = (root / "projections.json").read_text(encoding="utf-8")
            self.assertIn("fetched source content looks secret", durable)
            self.assertNotIn(secret_value, durable)

    def test_secret_like_refresh_preserves_prior_safe_projection_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProjectionStore(root)

            def safe_fetch(source, token):  # noqa: ARG001
                return FetchedSource(content="# Safe\n\nalpha", source_revision="safe-rev")

            first = store.sync_one(SOURCE, fetch=safe_fetch)
            projection = root / first.projection_path
            safe_bytes = projection.read_bytes()
            secret_value = "sk-" + ("B" * 24)

            def unsafe_fetch(source, token):  # noqa: ARG001
                return FetchedSource(
                    content=f"# Unsafe\n\nGITHUB_TOKEN={secret_value}\n",
                    source_revision="unsafe-rev",
                )

            rejected = store.sync_one(SOURCE, fetch=unsafe_fetch)
            self.assertEqual(rejected.sync_state, "error")
            self.assertEqual(rejected.projection_path, first.projection_path)
            self.assertEqual(rejected.content_digest, first.content_digest)
            self.assertEqual(rejected.source_revision, first.source_revision)
            self.assertEqual(projection.read_bytes(), safe_bytes)
            self.assertEqual(store.list_documents(SOURCE.project_id), [])
            meta = json.loads((root / "projections.json").read_text(encoding="utf-8"))
            entry = meta["projections"][store.meta_key(SOURCE)]
            self.assertEqual(entry["error"], "fetched source content looks secret")
            self.assertIn("prior_provenance", entry)
            self.assertNotIn(secret_value, json.dumps(meta, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
