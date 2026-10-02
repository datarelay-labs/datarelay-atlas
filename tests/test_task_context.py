import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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
