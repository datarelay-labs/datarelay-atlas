"""Bounded runtime observability regressions."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from atlas.cli import main
from atlas.github_sync import FetchedSource
from atlas.local_markdown import IMPORT_DIRNAME, SNAPSHOT_DIRNAME
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.projection import ProjectionStore
from atlas.provenance import ValidationError
from atlas.provider_dashboard import FILENAME as PROVIDER_DASHBOARD_FILENAME
from atlas.registry import ProjectRegistry
from atlas.runtime_observability import _completion_count, runtime_observability_snapshot
from atlas.service import AtlasService
from atlas.web_ui import render_operations
from atlas.work_controller import (
    COMPLETION_PROCESSED_DIRNAME,
    WorkControllerStore,
    WorkstreamRecord,
    enqueue_completion_event,
)

HEAD = "a" * 40
OBSERVED_AT = "2026-10-01T00:00:00Z"
SOURCE_FACTS = {
    "repository": "datarelay-labs/datarelay-atlas",
    "source_revision": HEAD,
    "clean": True,
}
PYTHON_FACTS = {"implementation": "cpython", "version": "3.12.0"}


class RuntimeObservabilityTests(unittest.TestCase):
    def _seed(self, root: Path) -> AtlasService:
        registry = ProjectRegistry(root)
        registry.register(
            project_id="alpha",
            repository="datarelay-labs/alpha",
        )
        registry.add_source(
            "alpha",
            source_id="charter",
            source_path="docs/charter.md",
        )
        registry.register(
            project_id="beta",
            repository="datarelay-labs/beta",
            enabled=False,
        )
        registry.add_source(
            "beta",
            source_id="notes",
            source_path="docs/notes.md",
        )

        source = registry.canonical_sources("alpha")[0]
        projections = ProjectionStore(
            root / "projections",
            snapshot_root=root / SNAPSHOT_DIRNAME,
        )
        projections.sync_one(
            source,
            fetch=lambda _source, _token: FetchedSource(
                content="# Charter\n\nalpha observability",
                source_revision="b" * 40,
            ),
        )

        WorkControllerStore(root).put(
            WorkstreamRecord(
                workstream="observe-alpha",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=219,
                branch="feat/phase5-bounded-runtime-observability",
                worktree_path="/tmp/observe-alpha",
                expected_head=HEAD,
                state="AUDITING",
            )
        )
        event = {
            "event_id": "evt-inbox",
            "workstream": "observe-alpha",
            "issue_number": 219,
            "branch": "feat/phase5-bounded-runtime-observability",
            "head": HEAD,
            "attempt": 1,
        }
        enqueue_completion_event(root, event)

        processed_event = dict(event, event_id="evt-processed")
        pending = enqueue_completion_event(root, processed_event)
        processed_dir = root / COMPLETION_PROCESSED_DIRNAME
        processed_dir.mkdir(parents=True, exist_ok=True)
        pending.replace(processed_dir / pending.name)
        (root / PROVIDER_DASHBOARD_FILENAME).write_text(
            "{}\n",
            encoding="utf-8",
        )
        return AtlasService(root)

    def _snapshot(self, root: Path, **kwargs: object) -> dict[str, object]:
        return runtime_observability_snapshot(
            root,
            observed_at=OBSERVED_AT,
            source_facts=SOURCE_FACTS,
            python_facts=PYTHON_FACTS,
            **kwargs,
        )

    def test_fixed_fixture_is_deterministic_and_matches_canonical_readers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            first = self._snapshot(root)
            second = self._snapshot(root)

            self.assertEqual(first, second)
            self.assertEqual(first["authority"], "OBSERVABILITY_ONLY")
            self.assertEqual(first["registry"]["project_count"], 2)
            self.assertEqual(first["registry"]["enabled_project_count"], 1)
            self.assertEqual(first["registry"]["source_count"], 2)
            self.assertEqual(first["registry"]["enabled_source_count"], 1)

            self.assertEqual(first["projections"]["project_count"], 1)
            self.assertEqual(first["projections"]["record_count"], 1)
            self.assertEqual(first["projections"]["document_count"], 1)
            self.assertEqual(first["controller"]["workstream_count"], 1)
            self.assertEqual(first["controller"]["state_counts"]["AUDITING"], 1)
            self.assertEqual(first["completion"]["inbox_count"], 1)
            self.assertEqual(first["completion"]["processed_count"], 1)

            self.assertEqual(first["storage"]["durable_file_count"], 4)
            self.assertGreater(first["storage"]["durable_byte_count"], 0)
            self.assertGreaterEqual(first["storage"]["rebuildable_file_count"], 2)
            self.assertEqual(first["derived_cache"]["present_root_count"], 1)
            self.assertEqual(first["derived_cache"]["file_count"], 1)
            self.assertEqual(first["release_authority"], "NONE")
            self.assertEqual(first["pass_authority"], "NONE")
            self.assertEqual(first["repair_authority"], "NONE")

    def test_digest_excludes_observation_time_but_binds_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            first = self._snapshot(root)
            second = runtime_observability_snapshot(
                root,
                observed_at="2026-10-01T00:01:00Z",
                source_facts=SOURCE_FACTS,
                python_facts=PYTHON_FACTS,
            )
            self.assertNotEqual(first["observed_at"], second["observed_at"])
            self.assertEqual(first["snapshot_digest"], second["snapshot_digest"])

    def test_unobservable_source_is_explicit_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            snapshot = runtime_observability_snapshot(
                root,
                repo_root=root / "missing-repository",
                observed_at=OBSERVED_AT,
                python_facts=PYTHON_FACTS,
            )
            self.assertEqual(snapshot["source"]["state"], "UNKNOWN")
            self.assertEqual(snapshot["source"]["head"], "UNKNOWN")
            self.assertEqual(snapshot["source"]["clean"], "UNKNOWN")

    def test_unexpected_state_fails_closed_without_echoing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            secret = "should-never-appear-in-observability"
            (root / "unexpected.bin").write_text(secret, encoding="utf-8")
            with self.assertRaises(ValidationError) as caught:
                self._snapshot(root)
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn("unexpected.bin", str(caught.exception))

    def test_broken_completion_queue_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            inbox = root / "completion-inbox"
            for entry in inbox.iterdir():
                entry.unlink()
            inbox.rmdir()
            inbox.symlink_to(root / "missing-completion-inbox", target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "completion queue is unsafe"):
                _completion_count(root, "completion-inbox")
            with self.assertRaises(ValidationError):
                self._snapshot(root)

    def test_broken_derived_cache_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            cache = root / PROVIDER_DASHBOARD_FILENAME
            cache.unlink()
            cache.symlink_to(root / "missing-cache-target")
            with self.assertRaisesRegex(ValidationError, "derived cache is unsafe"):
                self._snapshot(root)

    def test_personal_import_contents_are_not_traversed_or_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            import_root = root / IMPORT_DIRNAME
            import_root.mkdir()
            (import_root / "ignored-link").symlink_to(root / "missing-import")
            snapshot = self._snapshot(root)
            self.assertEqual(snapshot["storage"]["durable_file_count"], 4)

    def test_cli_failure_has_no_traceback_or_raw_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "raw-registry-secret"
            (root / "registry.json").write_text(
                '{"schema_version": 999, "projects": {}, "secret": "'
                + secret
                + '"}\n',
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--data-root", str(root), "ops", "observe"])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn(secret, stderr.getvalue())

    def test_cli_web_and_mcp_surface_the_same_bounded_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._seed(root)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["--data-root", str(root), "ops", "observe"])
            self.assertEqual(code, 0)
            cli_snapshot = json.loads(stdout.getvalue())
            self.assertEqual(cli_snapshot["authority"], "OBSERVABILITY_ONLY")

            readiness = service.operations_readiness()
            nested = readiness["observability"]
            self.assertEqual(nested["authority"], "OBSERVABILITY_ONLY")
            self.assertEqual(
                nested["registry"]["project_count"],
                cli_snapshot["registry"]["project_count"],
            )

            response = render_operations(service)
            html = response.body.decode("utf-8")
            self.assertEqual(response.status, "200 OK")
            self.assertIn("Runtime observability", html)
            self.assertIn("OBSERVABILITY_ONLY", html)
            self.assertIn(str(nested["snapshot_digest"]), html)

            tools = AtlasContextTools(
                retriever_factory=service.project_retriever,
                operations_readiness_factory=service.operations_readiness,
            )
            result = tools.call(
                "get_operations_readiness",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(
                result.data["observability"]["snapshot_digest"],
                nested["snapshot_digest"],
            )
            self.assertEqual(
                result.data["observability"]["authority"],
                "OBSERVABILITY_ONLY",
            )


if __name__ == "__main__":
    unittest.main()
