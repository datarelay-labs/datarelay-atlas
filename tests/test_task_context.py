import contextlib
from datetime import datetime, timedelta, timezone
import io
import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from atlas.cli import build_parser
from atlas.engineering_evidence import PRODUCER_REVISION, SUPPORTED_SCHEMAS
from atlas.github_sync import FetchedSource
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "task-context.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
WORKSTREAM = "verified-memory-task-context-bootstrap"


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = AtlasService(self.root)
        self.head = "b" * 40
        self.svc.register_project(
            project_id="demo",
            repository="datarelay-labs/demo",
            display_name="Demo",
        )
        self.svc.add_source(
            "demo",
            source_id="context",
            source_path="docs/context.md",
            title="Context",
        )

        def fetch(source, token):  # noqa: ARG001
            return FetchedSource(
                content="# Context\n\nverified memory task context bootstrap BODY-MARKER",
                source_revision="source-rev-1",
            )

        self.svc.sync_project("demo", fetch=fetch)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_lifecycle(self, *, canonical: bool = True) -> None:
        snapshot = {
            "schema_version": 1,
            "kind": "cursor_github_reconciliation",
            "observed_at": "2026-10-03T00:00:00Z",
            "repositories": ["datarelay-labs/demo"],
            "observations": [
                {
                    "repository": "datarelay-labs/demo",
                    "issue_number": 269,
                    "issue_state": "OPEN",
                    "issue_updated_at": "2026-10-03T00:00:00Z",
                    "author_trust": "trusted",
                    "packet_status": "ACTIVE",
                    "branch": "feat/memory",
                    "head": self.head,
                    "pr_number": 270,
                    "pr_state": "OPEN",
                    "pr_head": self.head if canonical else "c" * 40,
                    "canonical_fact": canonical,
                    "reasons": [] if canonical else ["PR_HEAD_MISMATCH"],
                }
            ],
            "summary": {
                "observed_count": 1,
                "canonical_count": 1 if canonical else 0,
                "noncanonical_count": 0 if canonical else 1,
            },
        }
        (self.root / "github-lifecycle.json").write_text(
            json.dumps(snapshot),
            encoding="utf-8",
        )

    def _efficiency(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "efficiency-telemetry",
            "run_id": "a" * 32,
            "repo": "datarelay-labs/demo",
            "workstream": WORKSTREAM,
            "task_kind": "DEVELOPMENT",
            "profile": {
                "provider": "openai",
                "model": "gpt",
                "reasoning": "high",
                "toolset": "chat",
            },
            "profile_switches": [],
            "started_at": "2026-10-03T00:00:00Z",
            "finished_at": "2026-10-03T00:01:00Z",
            "duration_seconds": 60,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost": 0.1,
            },
            "counts": {
                "tool_turns": 2,
                "retries": 0,
                "rereads": 0,
                "compactions": 0,
                "pr_rework": 0,
                "ci_rework": 0,
                "review_rework": 0,
                "human_interventions": 0,
            },
            "validation": {
                "ids": ["ATLAS-UNIT-001"],
                "exact_head": self.head,
                "evidence_state": "EXACT_HEAD",
                "outcome": "PASS",
            },
            "terminal": "PASS",
            "budget": {
                "soft_limit": None,
                "consumed": None,
                "unit": None,
                "state": "UNKNOWN",
                "disposition": "CONTINUE",
            },
        }

    def _import_current_evidence(self) -> None:
        artifact = self.root / "efficiency.json"
        artifact.write_text(json.dumps(self._efficiency()), encoding="utf-8")
        self.svc.import_engineering_evidence(
            project_id="demo",
            family="efficiency",
            artifact_path=artifact,
            schema_path=ROOT / str(SUPPORTED_SCHEMAS["efficiency"]["path"]),
            producer_revision=PRODUCER_REVISION,
            workstream=WORKSTREAM,
            subject_head=self.head,
            observed_at="2026-10-03T00:01:00Z",
        )

    def test_current_context_is_bounded_and_reference_only(self):
        self._write_lifecycle()
        self.svc.import_root.mkdir(parents=True, exist_ok=True)
        (self.svc.import_root / "personal.md").write_text(
            "verified memory task context bootstrap PERSONAL-MARKER",
            encoding="utf-8",
        )
        self.svc.import_personal_markdown(
            "demo",
            source_id="personal-note",
            source_path="personal.md",
        )
        personal_source = next(
            source
            for source in self.svc.registry.canonical_sources("demo")
            if source.source_id == "personal-note"
        )
        self.svc.projections.sync_one(personal_source)

        payload = self.svc.task_context(
            project_id="demo",
            workstream=WORKSTREAM,
        )
        VALIDATOR.validate(payload)
        self.assertEqual(payload["currentness"]["state"], "CURRENT")
        self.assertEqual(payload["currentness"]["current_head"], self.head)
        self.assertEqual(payload["request"]["workstream_binding"], "QUERY_HINT_ONLY")
        self.assertEqual(payload["project"]["repository"], "datarelay-labs/demo")
        self.assertEqual(len(payload["knowledge_refs"]), 1)
        self.assertEqual(
            payload["knowledge_refs"][0]["source_revision"],
            "source-rev-1",
        )
        self.assertEqual(
            payload["knowledge_refs"][0]["source_class"],
            "engineering",
        )
        rendered = json.dumps(payload)
        self.assertNotIn("BODY-MARKER", rendered)
        self.assertNotIn("personal-note", rendered)
        self.assertNotIn("PERSONAL-MARKER", rendered)
        self.assertLess(len(rendered.encode("utf-8")), 24 * 1024)
        self.assertEqual(payload["jit_retrieval"][0]["tool"], "search_project")
        by_repo = self.svc.task_context(
            repository="datarelay-labs/demo",
            workstream=WORKSTREAM,
        )
        self.assertEqual(by_repo["project"], payload["project"])

    def test_workstream_binding_requires_current_evidence(self):
        self._write_lifecycle()
        self._import_current_evidence()
        matched = self.svc.task_context(project_id="demo", workstream=WORKSTREAM)
        VALIDATOR.validate(matched)
        self.assertEqual(
            matched["request"]["workstream_binding"],
            "CURRENT_EVIDENCE_MATCH",
        )
        efficiency = next(
            item
            for item in matched["engineering_evidence"]["families"]
            if item["family"] == "efficiency"
        )
        self.assertEqual(efficiency["state"], "CURRENT")
        self.assertEqual(efficiency["latest"]["subject_head"], self.head)

        other = self.svc.task_context(
            project_id="demo",
            workstream="different-workstream",
        )
        self.assertEqual(other["request"]["workstream_binding"], "QUERY_HINT_ONLY")

    def test_currentness_fails_closed_for_missing_invalid_and_stale_lifecycle(self):
        unknown = self.svc.task_context(project_id="demo")
        self.assertEqual(unknown["currentness"]["state"], "UNKNOWN")

        (self.root / "github-lifecycle.json").write_text("{broken", encoding="utf-8")
        unavailable = self.svc.task_context(project_id="demo")
        self.assertEqual(unavailable["currentness"]["state"], "UNAVAILABLE")

        self._write_lifecycle()
        lifecycle_path = self.root / "github-lifecycle.json"
        expired_time = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        os.utime(lifecycle_path, (expired_time, expired_time))
        expired = self.svc.task_context(project_id="demo")
        self.assertEqual(expired["currentness"]["state"], "STALE")
        self.assertIn("cache expired", expired["lifecycle"]["work"]["detail"])

        self._write_lifecycle()
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()
        os.utime(lifecycle_path, (future_time, future_time))
        future = self.svc.task_context(project_id="demo")
        self.assertEqual(future["currentness"]["state"], "UNAVAILABLE")

        self._write_lifecycle(canonical=False)
        stale = self.svc.task_context(project_id="demo")
        self.assertEqual(stale["currentness"]["state"], "STALE")
        self.assertEqual(stale["lifecycle"]["work"]["canonical_packets"], [])

    def test_current_packet_priority_and_ambiguous_active_heads_fail_safe(self):
        completed_head = "d" * 40
        observations = [
            {
                "repository": "datarelay-labs/demo",
                "issue_number": 269,
                "issue_state": "OPEN",
                "issue_updated_at": "2026-10-03T00:00:00Z",
                "author_trust": "trusted",
                "packet_status": "ACTIVE",
                "branch": "feat/memory",
                "head": self.head,
                "pr_number": 270,
                "pr_state": "OPEN",
                "pr_head": self.head,
                "canonical_fact": True,
                "reasons": [],
            },
            {
                "repository": "datarelay-labs/demo",
                "issue_number": 261,
                "issue_state": "CLOSED",
                "issue_updated_at": "2026-10-02T00:00:00Z",
                "author_trust": "trusted",
                "packet_status": "COMPLETE",
                "branch": "feat/old",
                "head": completed_head,
                "pr_number": 262,
                "pr_state": "MERGED",
                "pr_head": completed_head,
                "canonical_fact": True,
                "reasons": [],
            },
        ]
        snapshot = {
            "schema_version": 1,
            "kind": "cursor_github_reconciliation",
            "observed_at": "2026-10-03T00:00:00Z",
            "repositories": ["datarelay-labs/demo"],
            "observations": observations,
            "summary": {
                "observed_count": 2,
                "canonical_count": 2,
                "noncanonical_count": 0,
            },
        }
        path = self.root / "github-lifecycle.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        old_evidence = {
            "schema_version": 2,
            "kind": "atlas_lifecycle_evidence",
            "observed_at": "2026-10-03T00:01:00Z",
            "repository": "datarelay-labs/demo",
            "candidate_head": completed_head,
            "channels": {
                "ci": {
                    "outcome": "PASS",
                    "detail": "old complete CI",
                    "evidence_ref": "ci:old-complete",
                }
            },
            "human_equivalent_user_tests": {},
        }
        (self.root / "lifecycle-evidence.json").write_text(
            json.dumps(old_evidence),
            encoding="utf-8",
        )
        current = self.svc.task_context(project_id="demo")
        VALIDATOR.validate(current)
        self.assertEqual(current["currentness"]["state"], "CURRENT")
        self.assertEqual(current["currentness"]["current_head"], self.head)
        self.assertEqual(
            [item["issue_number"] for item in current["lifecycle"]["work"]["canonical_packets"]],
            [269],
        )
        self.assertNotIn("AI Work #261", current["lifecycle"]["work"]["detail"])
        self.assertLessEqual(len(current["lifecycle"]["work"]["detail"]), 512)
        self.assertEqual(
            current["lifecycle"]["channels"]["ci"]["state"],
            "STALE_DIFFERENT_HEAD",
        )
        self.assertEqual(
            current["lifecycle"]["channels"]["ci"]["candidate_head"],
            completed_head,
        )

        observations[1].update(
            {
                "issue_number": 270,
                "issue_state": "OPEN",
                "packet_status": "ACTIVE",
                "branch": "feat/other",
                "head": completed_head,
                "pr_number": 271,
                "pr_state": "OPEN",
                "pr_head": completed_head,
            }
        )
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self._import_current_evidence()
        ambiguous = self.svc.task_context(
            project_id="demo",
            workstream=WORKSTREAM,
        )
        VALIDATOR.validate(ambiguous)
        self.assertEqual(ambiguous["currentness"]["state"], "UNKNOWN")
        self.assertIsNone(ambiguous["currentness"]["current_head"])
        self.assertEqual(
            ambiguous["request"]["workstream_binding"],
            "QUERY_HINT_ONLY",
        )
        self.assertEqual(
            {item["issue_number"] for item in ambiguous["lifecycle"]["work"]["canonical_packets"]},
            {269, 270},
        )
        self.assertEqual(
            ambiguous["lifecycle"]["channels"]["ci"]["state"],
            "UNKNOWN",
        )

    def test_malformed_engineering_metadata_is_unavailable(self):
        self.svc.add_source(
            "demo",
            source_id="engineering-meta",
            source_path=".engineering/project.yaml",
            title="Engineering System metadata",
        )

        def fetch(source, token):  # noqa: ARG001
            if source.source_id == "engineering-meta":
                return FetchedSource(
                    content="""engineering_system:
  version: 1
  baseline: false
  mode: adopted
  ci_mode: shared
project:
  name: demo
""",
                    source_revision="metadata-rev-1",
                )
            return FetchedSource(
                content="""# Context

verified memory task context bootstrap BODY-MARKER""",
                source_revision="source-rev-1",
            )

        self.svc.sync_project("demo", fetch=fetch)
        payload = self.svc.task_context(project_id="demo")
        VALIDATOR.validate(payload)
        self.assertEqual(payload["engineering_system"]["state"], "UNAVAILABLE")
        self.assertEqual(
            payload["engineering_system"]["detail"],
            "Engineering System metadata projection has invalid field types",
        )
        self.assertIsNone(payload["engineering_system"]["version"])
        self.assertIsNone(payload["engineering_system"]["baseline"])
        json.dumps(payload, allow_nan=False)

    def test_syntactically_invalid_engineering_metadata_is_unavailable(self):
        self.svc.add_source(
            "demo",
            source_id="engineering-meta",
            source_path=".engineering/project.yaml",
            title="Engineering System metadata",
        )

        def fetch(source, token):  # noqa: ARG001
            if source.source_id == "engineering-meta":
                return FetchedSource(
                    content="engineering_system: [\n",
                    source_revision="metadata-rev-invalid-yaml",
                )
            return FetchedSource(
                content="# Context\n\nverified memory task context bootstrap BODY-MARKER",
                source_revision="source-rev-1",
            )

        self.svc.sync_project("demo", fetch=fetch)
        payload = self.svc.task_context(project_id="demo")
        VALIDATOR.validate(payload)
        self.assertEqual(payload["engineering_system"]["state"], "UNAVAILABLE")
        self.assertEqual(
            payload["engineering_system"]["detail"],
            "Engineering System metadata projection is malformed",
        )
        json.dumps(payload, allow_nan=False)

    def test_bounded_size_matches_cli_transport_serialization(self):
        oversized_name = "가" * 4100
        root = Path(self.tmp.name) / "transport-bound"
        svc = AtlasService(root)
        svc.register_project(
            project_id="localized",
            repository="datarelay-labs/localized",
            display_name=oversized_name,
        )
        compact = json.dumps(
            {"display_name": oversized_name},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertLess(len(compact.encode("utf-8")), 24 * 1024)
        with self.assertRaisesRegex(
            ValidationError,
            "task context exceeds bounded response size",
        ):
            svc.task_context(project_id="localized")

    def test_identity_and_workstream_validation_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.svc.task_context()
        with self.assertRaises(ValidationError):
            self.svc.task_context(
                project_id="demo",
                repository="datarelay-labs/demo",
            )
        with self.assertRaises(ValidationError):
            self.svc.task_context(project_id="demo", workstream="../bad")

        self.svc.register_project(
            project_id="demo-two",
            repository="datarelay-labs/demo",
        )
        with self.assertRaises(ValidationError):
            self.svc.task_context(repository="datarelay-labs/demo")

    def test_currentness_uses_file_stat_bound_to_validated_snapshot_bytes(self):
        from atlas import lifecycle_intelligence as lifecycle_module

        self._write_lifecycle()
        path = self.root / "github-lifecycle.json"
        facts, observations, metadata, _ = (
            lifecycle_module.load_github_reconciliation_snapshot(
                path,
                include_file_stat=True,
            )
        )
        expired_stat = SimpleNamespace(
            st_mtime=(datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        )
        with patch.object(
            lifecycle_module,
            "load_github_reconciliation_snapshot",
            return_value=(facts, observations, metadata, expired_stat),
        ):
            result = self.svc.task_context(project_id="demo")
        self.assertEqual(result["currentness"]["state"], "STALE")
        self.assertIn("cache expired", result["lifecycle"]["work"]["detail"])

    def test_publish_github_snapshot_is_validated_atomic_and_cli_exposed(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        snapshot = {
            "schema_version": 1,
            "kind": "cursor_github_reconciliation",
            "observed_at": (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "repositories": ["datarelay-labs/demo"],
            "observations": [{
                "repository": "datarelay-labs/demo",
                "issue_number": 299,
                "issue_state": "OPEN",
                "issue_updated_at": "2026-10-03T01:00:00Z",
                "author_trust": "trusted",
                "packet_status": "ACTIVE",
                "branch": "fix/freshness",
                "head": self.head,
                "pr_number": None,
                "pr_state": "NONE",
                "pr_head": None,
                "canonical_fact": True,
                "reasons": [],
            }],
            "summary": {"observed_count": 1, "canonical_count": 1, "noncanonical_count": 0},
        }
        incoming = self.root / "incoming-lifecycle.json"
        incoming.write_text(json.dumps(snapshot), encoding="utf-8")
        result = self.svc.publish_github_lifecycle_snapshot(incoming)
        self.assertEqual(result["state"], "PUBLISHED")
        self.assertEqual(result["authority"], "DERIVED_READ_ONLY")
        published = self.root / "github-lifecycle.json"
        self.assertEqual(published.stat().st_mode & 0o777, 0o600)
        current = self.svc.task_context(project_id="demo")
        self.assertEqual(current["currentness"]["state"], "CURRENT")
        self.assertEqual(current["currentness"]["current_head"], self.head)

        duplicate = self.root / "duplicate-lifecycle.json"
        duplicate.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertEqual(
            self.svc.publish_github_lifecycle_snapshot(duplicate)["state"],
            "UNCHANGED",
        )

        parser = build_parser()
        cli_input = self.root / "incoming-lifecycle-cli.json"
        snapshot["observed_at"] = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        cli_input.write_text(json.dumps(snapshot), encoding="utf-8")
        args = parser.parse_args([
            "--data-root", str(self.root), "lifecycle", "publish-github-snapshot",
            "--snapshot", str(cli_input),
        ])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["state"], "PUBLISHED")

        before = published.read_bytes()

        older = self.root / "older-lifecycle.json"
        older_payload = dict(snapshot)
        older_payload["observed_at"] = (now - timedelta(seconds=10)).isoformat().replace(
            "+00:00", "Z"
        )
        older.write_text(json.dumps(older_payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "older than the current cache"):
            self.svc.publish_github_lifecycle_snapshot(older)
        self.assertEqual(published.read_bytes(), before)

        stale = self.root / "stale-lifecycle.json"
        stale_payload = dict(snapshot)
        stale_payload["observed_at"] = (now - timedelta(hours=2)).isoformat().replace(
            "+00:00", "Z"
        )
        stale.write_text(json.dumps(stale_payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "too old to publish"):
            self.svc.publish_github_lifecycle_snapshot(stale)
        self.assertEqual(published.read_bytes(), before)

        future = self.root / "future-lifecycle.json"
        future_payload = dict(snapshot)
        future_payload["observed_at"] = (now + timedelta(minutes=10)).isoformat().replace(
            "+00:00", "Z"
        )
        future.write_text(json.dumps(future_payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "too far in the future"):
            self.svc.publish_github_lifecycle_snapshot(future)
        self.assertEqual(published.read_bytes(), before)

        same_time_conflict = self.root / "same-time-conflict.json"
        conflict_payload = json.loads(before)
        conflict_payload["observations"][0]["issue_number"] = 300
        same_time_conflict.write_text(json.dumps(conflict_payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "conflicts at the current observed_at"):
            self.svc.publish_github_lifecycle_snapshot(same_time_conflict)
        self.assertEqual(published.read_bytes(), before)

        bad = self.root / "bad-lifecycle.json"
        bad.write_text('{"kind":"wrong"}', encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.svc.publish_github_lifecycle_snapshot(bad)
        self.assertEqual(published.read_bytes(), before)

        symlink_input = self.root / "symlink-lifecycle.json"
        symlink_input.symlink_to(cli_input)
        with self.assertRaisesRegex(ValidationError, "input is unsafe"):
            self.svc.publish_github_lifecycle_snapshot(symlink_input)
        self.assertEqual(published.read_bytes(), before)

        published.write_text("{broken", encoding="utf-8")
        repaired = self.svc.publish_github_lifecycle_snapshot(cli_input)
        self.assertEqual(repaired["state"], "PUBLISHED")
        self.assertEqual(
            self.svc.task_context(project_id="demo")["currentness"]["state"],
            "CURRENT",
        )

        victim = self.root / "victim.json"
        victim.write_text("keep", encoding="utf-8")
        published.unlink()
        published.symlink_to(victim)
        with self.assertRaisesRegex(ValidationError, "destination is unsafe"):
            self.svc.publish_github_lifecycle_snapshot(cli_input)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_publish_requires_complete_registered_github_repository_set(self):
        self.svc.register_project(
            project_id="peer",
            repository="datarelay-labs/peer",
            display_name="Peer",
        )
        self.svc.add_source(
            "peer",
            source_id="context",
            source_path="README.md",
            title="Peer",
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        partial = {
            "schema_version": 1,
            "kind": "cursor_github_reconciliation",
            "observed_at": now.isoformat().replace("+00:00", "Z"),
            "repositories": ["datarelay-labs/demo"],
            "observations": [],
            "summary": {
                "observed_count": 0,
                "canonical_count": 0,
                "noncanonical_count": 0,
            },
        }
        path = self.root / "partial-lifecycle.json"
        path.write_text(json.dumps(partial), encoding="utf-8")
        with self.assertRaisesRegex(
            ValidationError,
            "repository set does not match",
        ):
            self.svc.publish_github_lifecycle_snapshot(path)
        self.assertFalse((self.root / "github-lifecycle.json").exists())

    def test_cli_and_mcp_expose_equivalent_read_only_context(self):
        self._write_lifecycle()
        parser = build_parser()
        args = parser.parse_args(
            [
                "--data-root",
                str(self.root),
                "task-context",
                "show",
                "--project-id",
                "demo",
                "--workstream",
                WORKSTREAM,
            ]
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = args.func(args)
        self.assertEqual(rc, 0)
        cli_payload = json.loads(out.getvalue())

        tools = AtlasContextTools(
            retriever_factory=self.svc.project_retriever,
            task_context_factory=lambda project_id, repository, workstream: self.svc.task_context(
                project_id=project_id,
                repository=repository,
                workstream=workstream,
            ),
        )
        names = {item["name"] for item in tools.list_tools(default_read_scopes())}
        self.assertIn("get_task_context", names)
        result = tools.call(
            "get_task_context",
            {"project_id": "demo", "workstream": WORKSTREAM},
            scopes=default_read_scopes(),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data, cli_payload)
        denied = tools.call(
            "get_task_context",
            {"project_id": "demo"},
            scopes=["atlas.write"],
        )
        self.assertFalse(denied.ok)
        self.assertEqual(denied.error, "unauthorized")


if __name__ == "__main__":
    unittest.main()
