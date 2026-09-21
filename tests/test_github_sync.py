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
            self.assertEqual(first.sync_state, "ok")
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


if __name__ == "__main__":
    unittest.main()
