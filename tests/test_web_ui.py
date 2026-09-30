import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from wsgiref.util import setup_testing_defaults

from atlas.provenance import CanonicalSource, render_derived_document
from atlas.cli import build_parser
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import _adoption_projection_state, _source_relation, create_app, render_cross_project_search, serve_ui

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
        env.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": "127.0.0.1:8788"})
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
        self.assertIn("no trusted local lifecycle evidence", body)

    def test_source_relation_never_promotes_personal_knowledge(self):
        self.assertEqual(_source_relation({"source_class": "personal", "canonical": False, "derived": True}), "personal reference / non-authoritative")
        self.assertEqual(_source_relation({"source_class": "engineering", "canonical": False, "derived": True}), "canonical engineering source reference")
        self.assertEqual(_source_relation({"source_class": "personal", "canonical": True, "derived": False}), "personal reference / non-authoritative")

    def test_project_search_shows_attributable_escaped_hit(self):
        state, body = self.get("/projects/demo", "q=needle")
        self.assertEqual(state["status"], "200 OK")
        self.assertIn("README.md", body)
        self.assertIn("a" * 40, body)
        self.assertIn("DERIVED", body)
        self.assertIn("canonical engineering source reference", body)
        self.assertIn("Unsafe &lt;title&gt;", body)
        self.assertNotIn("Unsafe <title>", body)

    def test_untrusted_host_is_rejected_before_rendering_data(self):
        env = {}
        setup_testing_defaults(env)
        env.update({"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": "", "HTTP_HOST": "attacker.example"})
        state = {}
        def start(status, headers):
            state.update(status=status, headers=dict(headers))
        body = b"".join(self.app(env, start)).decode()
        self.assertEqual(state["status"], "400 Bad Request")
        self.assertIn("Untrusted request", body)
        self.assertNotIn("&lt;Demo &amp; Co&gt;", body)

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

    def test_cli_lifecycle_commands_are_read_only_and_bounded(self):
        show = build_parser().parse_args(["--data-root", str(self.root), "lifecycle", "show", "demo"])
        self.assertEqual(show.project_id, "demo")
        self.assertEqual(show.func.__name__, "cmd_lifecycle_show")
        validate = build_parser().parse_args(["--data-root", str(self.root), "lifecycle", "validate", "demo"])
        self.assertEqual(validate.func.__name__, "cmd_lifecycle_validate")

    def test_cli_web_defaults_are_loopback_only(self):
        args = build_parser().parse_args(["--data-root", str(self.root), "web", "serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8788)
        for host in ("0.0.0.0", "::1"):
            with self.assertRaisesRegex(ValidationError, "loopback-only"):
                serve_ui(self.root, host=host, port=8788)

    def test_corrupt_projection_state_is_not_reported_as_404(self):
        (self.root / "projections/projections.json").write_text("{broken")
        state, body = self.get("/projects/demo")
        self.assertEqual(state["status"], "500 Internal Server Error")
        self.assertIn("Atlas state unavailable", body)

    def test_adoption_state_uses_configured_engineering_metadata_source(self):
        metadata = SimpleNamespace(provider="github", enabled=True, source_path=".engineering/project.yaml")
        other = SimpleNamespace(provider="github", enabled=True, source_path="README.md")
        project = SimpleNamespace(engineering_metadata_path=".engineering/project.yaml", sources={"engineering-meta": metadata, "readme": other})
        self.assertEqual(_adoption_projection_state(project, [{"source_id": "engineering-meta", "sync_state": "success"}]), "OBSERVED")
        self.assertEqual(_adoption_projection_state(project, [{"source_id": "engineering-meta", "sync_state": "error"}]), "UNKNOWN")
        self.assertEqual(_adoption_projection_state(SimpleNamespace(engineering_metadata_path=".engineering/project.yaml", sources={"readme": other}), []), "UNKNOWN")

    def test_knowledge_coverage_reports_projection_gaps_without_inventing_analysis(self):
        _, body = self.get("/projects/demo")
        self.assertIn("Knowledge coverage", body)
        self.assertIn("COMPLETE", body)
        self.assertIn("1/1 configured sources projected", body)
        self.assertIn("Contradictions", body)
        self.assertIn("no derived contradiction analysis in this slice", body)
        self.assertIn("Unanswered questions", body)
        self.assertIn("no derived question analysis in this slice", body)

    def test_lifecycle_detail_route_preserves_independent_truth(self):
        state, body = self.get("/projects/demo/lifecycle")
        self.assertEqual(state["status"], "200 OK")
        self.assertIn("Lifecycle evidence", body)
        self.assertIn("Work / PR", body)
        self.assertIn("CI", body)
        self.assertIn("Tests", body)
        self.assertIn("Release", body)
        self.assertIn("Browser gates", body)
        self.assertIn("does not infer one channel from another", body)
        self.assertNotIn(">PASS<", body)

    def test_lifecycle_evidence_is_bounded(self):
        _, body = self.get("/projects/demo")
        self.assertIn("Lifecycle evidence", body)
        self.assertIn("GitHub Work / PR", body)
        self.assertIn("no trusted local lifecycle evidence", body)
        self.assertIn("no exact-candidate CI evidence loaded", body)
        self.assertIn("no exact-candidate test evidence loaded", body)
        self.assertIn("no exact-candidate release evidence loaded", body)
        self.assertIn("Browser gates", body)
        self.assertIn("browser gates are configured but no execution evidence is loaded", body)

    def test_valid_local_lifecycle_snapshot_surfaces_active_packet(self):
        snapshot = {
            "schema_version": 1, "kind": "cursor_github_reconciliation",
            "observed_at": "2026-09-30T00:00:00Z", "repositories": ["datarelay-labs/demo"],
            "observations": [{
                "repository": "datarelay-labs/demo", "issue_number": 162, "issue_state": "OPEN",
                "issue_updated_at": "2026-09-30T00:00:00Z", "author_trust": "trusted",
                "packet_status": "ACTIVE", "branch": "feat/ui", "head": "b" * 40,
                "pr_number": 163, "pr_state": "OPEN", "pr_head": "b" * 40,
                "canonical_fact": True, "reasons": []}],
            "summary": {"observed_count": 1, "canonical_count": 1, "noncanonical_count": 0},
        }
        (self.root / "github-lifecycle.json").write_text(json.dumps(snapshot))
        _, body = self.get("/projects/demo")
        self.assertIn("OBSERVED", body)
        self.assertIn("AI Work #162 ACTIVE", body)
        self.assertIn("feat/ui", body)
        self.assertIn("b" * 40, body)
        self.assertIn("PR #163 OPEN", body)

    def test_noncanonical_head_mismatch_is_stale_not_observed(self):
        snapshot = {
            "schema_version": 1, "kind": "cursor_github_reconciliation",
            "observed_at": "2026-09-30T00:00:00Z", "repositories": ["datarelay-labs/demo"],
            "observations": [{
                "repository": "datarelay-labs/demo", "issue_number": 171, "issue_state": "OPEN",
                "issue_updated_at": "2026-09-30T00:00:00Z", "author_trust": "trusted",
                "packet_status": "ACTIVE", "branch": "feat/lifecycle", "head": "b" * 40,
                "pr_number": 172, "pr_state": "OPEN", "pr_head": "c" * 40,
                "canonical_fact": False, "reasons": ["PR_HEAD_MISMATCH"]}],
            "summary": {"observed_count": 1, "canonical_count": 0, "noncanonical_count": 1},
        }
        (self.root / "github-lifecycle.json").write_text(json.dumps(snapshot))
        _, body = self.get("/projects/demo")
        self.assertIn("STALE", body)
        self.assertIn("PR_HEAD_MISMATCH", body)
        self.assertNotIn("AI Work #171 ACTIVE", body)

    def test_exact_candidate_lifecycle_channels_are_independent(self):
        head = "b" * 40
        snapshot = {
            "schema_version": 1, "kind": "cursor_github_reconciliation",
            "observed_at": "2026-09-30T00:00:00Z", "repositories": ["datarelay-labs/demo"],
            "observations": [{"repository": "datarelay-labs/demo", "issue_number": 171, "issue_state": "OPEN",
                "issue_updated_at": "2026-09-30T00:00:00Z", "author_trust": "trusted", "packet_status": "ACTIVE",
                "branch": "feat/lifecycle", "head": head, "pr_number": 172, "pr_state": "OPEN", "pr_head": head,
                "canonical_fact": True, "reasons": []}],
            "summary": {"observed_count": 1, "canonical_count": 1, "noncanonical_count": 0}}
        evidence = {"schema_version": 1, "kind": "atlas_lifecycle_evidence", "observed_at": "2026-09-30T00:01:00Z",
            "repository": "datarelay-labs/demo", "candidate_head": head,
            "channels": {"ci": {"outcome": "PASS", "detail": "run 123"}, "tests": {"outcome": "FAIL", "detail": "2 failed"},
                "browser": {"outcome": "BLOCKED", "detail": "host libraries missing"}}}
        (self.root / "github-lifecycle.json").write_text(json.dumps(snapshot))
        (self.root / "lifecycle-evidence.json").write_text(json.dumps(evidence))
        _, body = self.get("/projects/demo")
        self.assertIn("PASS: run 123", body)
        self.assertIn("FAIL: 2 failed", body)
        self.assertIn("BLOCKED: host libraries missing", body)
        self.assertIn("no exact-candidate release evidence loaded", body)

    def test_different_candidate_lifecycle_evidence_is_stale(self):
        head = "b" * 40
        snapshot = {"schema_version": 1, "kind": "cursor_github_reconciliation", "observed_at": "2026-09-30T00:00:00Z",
            "repositories": ["datarelay-labs/demo"], "observations": [{"repository": "datarelay-labs/demo", "issue_number": 171,
                "issue_state": "OPEN", "issue_updated_at": "2026-09-30T00:00:00Z", "author_trust": "trusted", "packet_status": "ACTIVE",
                "branch": "feat/lifecycle", "head": head, "pr_number": None, "pr_state": "NONE", "pr_head": None,
                "canonical_fact": True, "reasons": []}], "summary": {"observed_count": 1, "canonical_count": 1, "noncanonical_count": 0}}
        evidence = {"schema_version": 1, "kind": "atlas_lifecycle_evidence", "observed_at": "2026-09-30T00:01:00Z",
            "repository": "datarelay-labs/demo", "candidate_head": "c" * 40, "channels": {"ci": {"outcome": "PASS", "detail": "old run"}}}
        (self.root / "github-lifecycle.json").write_text(json.dumps(snapshot)); (self.root / "lifecycle-evidence.json").write_text(json.dumps(evidence))
        _, body = self.get("/projects/demo")
        self.assertIn("STALE", body)
        self.assertIn("PASS for different candidate", body)
        self.assertNotIn("PASS: old run", body)

    def test_lifecycle_evidence_requires_utc_observation_time(self):
        evidence = {"schema_version": 1, "kind": "atlas_lifecycle_evidence", "observed_at": "yesterday",
            "repository": "datarelay-labs/demo", "candidate_head": "b" * 40, "channels": {"ci": {"outcome": "PASS", "detail": "run"}}}
        (self.root / "lifecycle-evidence.json").write_text(json.dumps(evidence))
        _, body = self.get("/projects/demo")
        self.assertIn("UNAVAILABLE", body)

    def test_invalid_lifecycle_channel_evidence_fails_closed(self):
        evidence = {"schema_version": 1, "kind": "atlas_lifecycle_evidence", "observed_at": "2026-09-30T00:01:00Z",
            "repository": "wrong/repo", "candidate_head": "b" * 40, "channels": {"ci": {"outcome": "PASS", "detail": "run"}}}
        (self.root / "lifecycle-evidence.json").write_text(json.dumps(evidence))
        _, body = self.get("/projects/demo")
        self.assertIn("UNAVAILABLE", body)
        self.assertIn("local lifecycle evidence failed validation", body)

    def test_invalid_local_lifecycle_snapshot_fails_closed_in_ui(self):
        (self.root / "github-lifecycle.json").write_text("{broken")
        _, body = self.get("/projects/demo")
        self.assertIn("UNAVAILABLE", body)
        self.assertIn("failed validation", body)

    def test_cross_project_search_marks_personal_hits_non_authoritative(self):
        project = SimpleNamespace(project_id="notes", display_name="Notes", enabled=True)
        hit = SimpleNamespace(title="Note", path="ideas.md", content="needle", identity="idea@local", provenance={
            "repository": "local://atlas-personal", "ref": "local", "source_path": "ideas.md",
            "source_revision": "c" * 40, "source_class": "personal", "canonical": False, "derived": True})
        service = SimpleNamespace(list_projects=lambda: [project], search=lambda project_id, query, limit: [hit])
        body = render_cross_project_search(service, "needle").body.decode()
        self.assertIn("personal reference / non-authoritative", body)
        self.assertIn("DERIVED", body)
        self.assertIn("local://atlas-personal · local · ideas.md", body)
        self.assertIn("idea@local", body)

    def test_cross_project_search_preserves_project_provenance(self):
        svc = AtlasService(self.root)
        svc.register_project(project_id="other", repository="datarelay-labs/other", display_name="Other")
        state, body = self.get("/search", "q=needle")
        self.assertEqual(state["status"], "200 OK")
        self.assertIn("Cross-project search", body)
        self.assertIn("datarelay-labs/demo", body)
        self.assertIn("README.md", body)
        self.assertIn("canonical engineering source reference", body)
        self.assertIn("DERIVED", body)
        self.assertIn("readme@main", body)
        self.assertIn("datarelay-labs/demo · main · README.md", body)
        self.assertIn("1 attributable result(s) across enabled projects", body)
        self.assertNotIn("datarelay-labs/other ·", body)

if __name__ == "__main__":
    unittest.main()
