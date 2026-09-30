import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from atlas.provenance import CanonicalSource, render_derived_document
from atlas.cli import build_parser
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app, serve_ui

class WebUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        svc = AtlasService(self.root)
        svc.register_project(project_id="demo", repository="datarelay-labs/demo", display_name="<Demo & Co>")
        svc.add_source("demo", source_id="readme", source_path="README.md", title="Unsafe <title>")
        source = CanonicalSource(source_id="readme", project_id="demo", provider="github", repository="datarelay-labs/demo", ref="main", source_path="README.md", title="Unsafe <title>")
        text = render_derived_document(source, "<script>alert(1)</script> needle", "a" * 40)
        path = self.root / "projections/demo/readme.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        meta = {"projections": {"demo/readme": {
            "source_id": "readme", "project_id": "demo", "projection_path": "demo/readme.md",
            "content_digest": hashlib.sha256(text.encode()).hexdigest(), "source_revision": "a" * 40,
            "fetched_at": "2026-09-30T00:00:00+00:00", "sync_state": "success", "projector": "atlas.projection/v1",
            "provenance": {"project_id": "demo", "provider": "github", "repository": "datarelay-labs/demo", "ref": "main",
                "source_path": "README.md", "source_revision": "a" * 40, "derived": True, "canonical": False,
                "source_class": "engineering", "engineering_authority": True}}}}
        (self.root / "projections/projections.json").write_text(json.dumps(meta))
        self.app = create_app(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path, query="", method="GET"):
        env = {}
        setup_testing_defaults(env)
        env.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query})
        state = {}
        def start(status, headers):
            state.update(status=status, headers=dict(headers))
        body = b"".join(self.app(env, start)).decode()
        return state, body

    def test_project_list_escapes_source_data(self):
        state, body = self.get("/")
        self.assertEqual(state["status"], "200 OK")
        self.assertIn("&lt;Demo &amp; Co&gt;", body)
        self.assertNotIn("<Demo & Co>", body)
        self.assertIn("default-src 'none'", state["headers"]["Content-Security-Policy"])

    def test_project_search_shows_attributable_escaped_hit(self):
        state, body = self.get("/projects/demo", "q=needle")
        self.assertEqual(state["status"], "200 OK")
        self.assertIn("README.md", body)
        self.assertIn("a" * 40, body)
        self.assertIn("DERIVED", body)
        self.assertIn("canonical source reference", body)
        self.assertIn("Unsafe &lt;title&gt;", body)
        self.assertNotIn("Unsafe <title>", body)

    def test_unknown_project_and_write_fail_closed(self):
        self.assertEqual(self.get("/projects/missing")[0]["status"], "404 Not Found")
        self.assertEqual(self.get("/", method="POST")[0]["status"], "405 Method Not Allowed")

    def test_query_is_bounded(self):
        self.assertEqual(self.get("/projects/demo", "q=" + ("x" * 257))[0]["status"], "400 Bad Request")

    def test_lifecycle_is_explicit_unknown(self):
        _, body = self.get("/projects/demo")
        self.assertIn("Lifecycle", body)
        self.assertIn("UNKNOWN", body)
        self.assertNotIn(">PASS<", body)

    def test_cli_web_defaults_are_loopback_only(self):
        args = build_parser().parse_args(["--data-root", str(self.root), "web", "serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8788)
        with self.assertRaisesRegex(ValidationError, "loopback-only"):
            serve_ui(self.root, host="0.0.0.0", port=8788)

if __name__ == "__main__":
    unittest.main()
