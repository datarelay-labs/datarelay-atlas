import contextlib
from datetime import datetime, timezone
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from atlas.engineering_evidence import (
    AUTHORITY,
    FILENAME,
    PRODUCER_REVISION,
    SUPPORTED_SCHEMAS,
    _read_regular,
    engineering_evidence_dashboard,
    import_engineering_evidence,
    validate_engineering_evidence_store,
)
from atlas.cli import build_parser
from atlas.data_protection import backup_data_root, restore_test
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.registry import ProjectRegistry
from atlas.service import AtlasService
from atlas.web_ui import create_app


class EngineeringEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "data"
        self.input_root = self.base / "inputs"
        self.root.mkdir()
        self.input_root.mkdir()
        self.registry = ProjectRegistry(self.root)
        self.registry.register(
            project_id="demo",
            repository="datarelay-labs/demo",
            display_name="Demo",
        )
        self.head = "b" * 40
        self.repo_root = Path(__file__).resolve().parents[1]
    def tearDown(self):
        self.tmp.cleanup()

    def _schema(self, family: str) -> Path:
        return self.repo_root / str(SUPPORTED_SCHEMAS[family]["path"])

    def _artifact(self, family: str, payload: object, *, suffix: str = ".json") -> Path:
        path = self.input_root / f"{family}-artifact{suffix}"
        if suffix == ".yaml":
            import yaml
            path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _import(self, family: str, payload: object, *, observed_at: str = "2026-10-02T07:00:00Z"):
        schema = self._schema(family)
        suffix = ".yaml" if family == "runtime" else ".json"
        artifact = self._artifact(family, payload, suffix=suffix)
        return import_engineering_evidence(
            self.root,
            self.registry,
            project_id="demo",
            family=family,
            artifact_path=artifact,
            schema_path=schema,
            producer_revision=PRODUCER_REVISION,
            workstream="u2-evidence",
            subject_head=self.head,
            observed_at=observed_at,
        )

    def _efficiency(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "efficiency-telemetry",
            "run_id": "a" * 32,
            "repo": "datarelay-labs/demo",
            "workstream": "u2-evidence",
            "task_kind": "DEVELOPMENT",
            "profile": {
                "provider": "openai",
                "model": "gpt",
                "reasoning": "high",
                "toolset": "chat",
            },
            "profile_switches": [],
            "started_at": "2026-10-02T06:59:00Z",
            "finished_at": "2026-10-02T07:00:00Z",
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

    def _behavior(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "behavior-eval-run",
            "runner": "deterministic",
            "head": self.head,
            "provider": "openai",
            "model": "gpt",
            "scenarios": [
                {
                    "id": "BEH-SAFE",
                    "mandatory": True,
                    "safety": True,
                    "status": "PASS",
                    "evidence": "PASS",
                }
            ],
        }

    def _trust(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "trust-evidence-receipt",
            "target_repo": "datarelay-labs/demo",
            "workstream": "u2-evidence",
            "intent_revision": 1,
            "subject_head": self.head,
            "feature_id": "u2-evidence",
            "oracle": "EXACT_HEAD",
            "items": [
                {
                    "authority": "test",
                    "id": "unit",
                    "status": "PASS",
                    "subject_head": self.head,
                    "intent_revision": 1,
                    "reference": "tests:unit",
                    "external_digest": "c" * 64,
                }
            ],
        }

    def _runtime(self) -> dict:
        return {
            "version": 1,
            "authorities": {
                "health": "operations.health_command",
                "smoke": "release.public_smoke_command",
                "e2e": "release.operational_e2e_command",
            },
            "capabilities": {
                "start": {"support": "unsupported"},
                "logs": {"support": "unsupported"},
                "browser": {"support": "unsupported"},
                "metrics": {"support": "unsupported"},
                "traces": {"support": "unsupported"},
                "cleanup": {"support": "unsupported"},
            },
        }

    def test_imports_four_families_without_raw_artifact_bodies(self):
        results = [
            self._import("efficiency", self._efficiency()),
            self._import("behavior", self._behavior(), observed_at="2026-10-02T07:00:01Z"),
            self._import("trust", self._trust(), observed_at="2026-10-02T07:00:02Z"),
            self._import("runtime", self._runtime(), observed_at="2026-10-02T07:00:03Z"),
        ]
        self.assertTrue(all(item["state"] == "IMPORTED" for item in results))
        store = validate_engineering_evidence_store(self.root)
        self.assertEqual(len(store["records"]), 4)
        durable = (self.root / FILENAME).read_text(encoding="utf-8")
        self.assertNotIn('"run_id"', durable)
        self.assertNotIn('"reference": "tests:unit"', durable)
        self.assertNotIn('"command"', durable)
        self.assertNotIn(str(self.input_root), durable)
        self.assertIn(AUTHORITY, durable)
    def test_duplicate_is_idempotent_and_conflict_is_rejected(self):
        first = self._import("efficiency", self._efficiency())
        second = self._import("efficiency", self._efficiency())
        self.assertEqual(first["state"], "IMPORTED")
        self.assertEqual(second["state"], "DUPLICATE")
        changed = self._efficiency()
        changed["duration_seconds"] = 61
        with self.assertRaisesRegex(ValidationError, "conflicting"):
            self._import("efficiency", changed)

    def test_store_capacity_rejects_growth_without_corrupting_existing_store(self):
        self._import("efficiency", self._efficiency())
        path = self.root / FILENAME
        before = path.read_bytes()
        with mock.patch("atlas.engineering_evidence.MAX_STORE_BYTES", len(before) + 16):
            with self.assertRaisesRegex(ValidationError, "bounded store size"):
                self._import(
                    "efficiency",
                    self._efficiency(),
                    observed_at="2026-10-02T07:00:01Z",
                )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(validate_engineering_evidence_store(self.root)["records"]), 1)

    def test_trust_item_attribution_must_match_receipt(self):
        payload = self._trust()
        imported = self._import("trust", payload)
        self.assertEqual(imported["record"]["summary"]["intent_revision"], 1)

        bad_head = self._trust()
        bad_head["items"][0]["subject_head"] = "d" * 40
        with self.assertRaisesRegex(ValidationError, "item attribution"):
            self._import("trust", bad_head, observed_at="2026-10-02T07:00:01Z")

        bad_intent = self._trust()
        bad_intent["items"][0]["intent_revision"] = 2
        with self.assertRaisesRegex(ValidationError, "item attribution"):
            self._import("trust", bad_intent, observed_at="2026-10-02T07:00:02Z")

    def test_schema_revision_and_attribution_mismatch_fail_closed(self):
        payload = self._efficiency()
        schema = self._schema("efficiency")
        artifact = self._artifact("efficiency", payload)
        with self.assertRaisesRegex(ValidationError, "producer revision"):
            import_engineering_evidence(
                self.root,
                self.registry,
                project_id="demo",
                family="efficiency",
                artifact_path=artifact,
                schema_path=schema,
                producer_revision="d" * 40,
                workstream="u2-evidence",
                subject_head=self.head,
                observed_at="2026-10-02T07:00:00Z",
            )
        tampered_schema = self.input_root / "tampered-schema.json"
        tampered_schema.write_text('{"type":"object"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "schema digest"):
            import_engineering_evidence(
                self.root,
                self.registry,
                project_id="demo",
                family="efficiency",
                artifact_path=artifact,
                schema_path=tampered_schema,
                producer_revision=PRODUCER_REVISION,
                workstream="u2-evidence",
                subject_head=self.head,
                observed_at="2026-10-02T07:00:00Z",
            )
        bad = self._efficiency()
        bad["repo"] = "datarelay-labs/other"
        with self.assertRaisesRegex(ValidationError, "attribution"):
            self._import("efficiency", bad)

    def test_fifo_input_is_rejected_before_open(self):
        fifo = self.input_root / "evidence.fifo"
        os.mkfifo(fifo)
        with mock.patch("atlas.engineering_evidence.os.open") as open_mock:
            with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                _read_regular(fifo, limit=1024, label="fifo")
        open_mock.assert_not_called()

    def test_regular_read_uses_nofollow_nonblocking_flags(self):
        path = self.input_root / "regular.txt"
        path.write_bytes(b"bounded")
        real_open = os.open
        with mock.patch(
            "atlas.engineering_evidence.os.open",
            wraps=real_open,
        ) as open_mock:
            self.assertEqual(
                _read_regular(path, limit=1024, label="regular"),
                b"bounded",
            )
        flags = open_mock.call_args.args[1]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_NONBLOCK)

    def test_unsafe_paths_duplicate_keys_and_secret_content_fail_closed(self):
        schema = self._schema("efficiency")
        artifact = self.input_root / "dup.json"
        artifact.write_text('{"kind":"efficiency-telemetry","kind":"other"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            import_engineering_evidence(
                self.root,
                self.registry,
                project_id="demo",
                family="efficiency",
                artifact_path=artifact,
                schema_path=schema,
                producer_revision=PRODUCER_REVISION,
                workstream="u2-evidence",
                subject_head=self.head,
                observed_at="2026-10-02T07:00:00Z",
            )
        target = self._artifact("efficiency", self._efficiency())
        link = self.input_root / "artifact-link.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValidationError, "path is unsafe"):
            import_engineering_evidence(
                self.root,
                self.registry,
                project_id="demo",
                family="efficiency",
                artifact_path=link,
                schema_path=schema,
                producer_revision=PRODUCER_REVISION,
                workstream="u2-evidence",
                subject_head=self.head,
                observed_at="2026-10-02T07:00:00Z",
            )
        secret = self._efficiency()
        secret["profile"]["model"] = "sk-" + ("A" * 24)
        with self.assertRaisesRegex(ValidationError, "sensitive content"):
            self._import("efficiency", secret)

    def _write_lifecycle(self, head: str) -> None:
        observed_at = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        snapshot = {
            "schema_version": 1,
            "kind": "cursor_github_reconciliation",
            "observed_at": observed_at,
            "repositories": ["datarelay-labs/demo"],
            "observations": [
                {
                    "repository": "datarelay-labs/demo",
                    "issue_number": 261,
                    "issue_state": "OPEN",
                    "issue_updated_at": "2026-10-02T07:01:00Z",
                    "author_trust": "trusted",
                    "packet_status": "ACTIVE",
                    "branch": "feat/u2",
                    "head": head,
                    "pr_number": None,
                    "pr_state": "NONE",
                    "pr_head": None,
                    "canonical_fact": True,
                    "reasons": [],
                }
            ],
            "summary": {
                "observed_count": 1,
                "canonical_count": 1,
                "noncanonical_count": 0,
            },
        }
        (self.root / "github-lifecycle.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )

    def test_dashboard_uses_existing_lifecycle_authority_for_currentness(self):
        self._import("efficiency", self._efficiency())
        initial = engineering_evidence_dashboard(self.root, self.registry)
        row = initial["projects"][0]["families"][0]
        self.assertEqual(row["family"], "efficiency")
        self.assertEqual(row["state"], "UNKNOWN")

        self._write_lifecycle(self.head)
        current = engineering_evidence_dashboard(self.root, self.registry)
        row = current["projects"][0]["families"][0]
        self.assertEqual(row["state"], "CURRENT")
        self.assertEqual(row["current_head"], self.head)

        self._write_lifecycle("c" * 40)
        stale = engineering_evidence_dashboard(self.root, self.registry)
        row = stale["projects"][0]["families"][0]
        self.assertEqual(row["state"], "STALE_DIFFERENT_HEAD")
        self.assertEqual(row["latest"]["subject_head"], self.head)

    def test_currentness_prefers_active_canonical_packet_over_completed_head(self):
        self._import("efficiency", self._efficiency())
        self._write_lifecycle(self.head)
        path = self.root / "github-lifecycle.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["observations"].append(
            {
                "repository": "datarelay-labs/demo",
                "issue_number": 260,
                "issue_state": "CLOSED",
                "issue_updated_at": "2026-10-01T07:01:00Z",
                "author_trust": "trusted",
                "packet_status": "COMPLETE",
                "branch": "feat/older",
                "head": "c" * 40,
                "pr_number": None,
                "pr_state": "NONE",
                "pr_head": None,
                "canonical_fact": True,
                "reasons": [],
            }
        )
        snapshot["summary"] = {
            "observed_count": 2,
            "canonical_count": 2,
            "noncanonical_count": 0,
        }
        path.write_text(json.dumps(snapshot), encoding="utf-8")

        dashboard = engineering_evidence_dashboard(self.root, self.registry)
        row = dashboard["projects"][0]["families"][0]
        self.assertEqual(row["state"], "CURRENT")
        self.assertEqual(row["current_head"], self.head)

    def test_scoped_dashboard_does_not_leak_portfolio_record_count(self):
        self._import("efficiency", self._efficiency())
        self.registry.register(
            project_id="other",
            repository="datarelay-labs/other",
            display_name="Other",
        )
        payload = self._efficiency()
        payload["repo"] = "datarelay-labs/other"
        artifact = self._artifact("efficiency-other", payload)
        import_engineering_evidence(
            self.root,
            self.registry,
            project_id="other",
            family="efficiency",
            artifact_path=artifact,
            schema_path=self._schema("efficiency"),
            producer_revision=PRODUCER_REVISION,
            workstream="u2-evidence",
            subject_head=self.head,
            observed_at="2026-10-02T07:00:01Z",
        )

        portfolio = engineering_evidence_dashboard(self.root, self.registry)
        scoped = engineering_evidence_dashboard(
            self.root, self.registry, project_ids=["demo"]
        )
        self.assertEqual(portfolio["record_count"], 2)
        self.assertEqual(scoped["record_count"], 1)
        self.assertEqual(scoped["project_count"], 1)
        self.assertEqual(scoped["projects"][0]["project_id"], "demo")

    def test_durable_store_identity_tampering_fails_closed(self):
        self._import("efficiency", self._efficiency())
        path = self.root / FILENAME
        original = json.loads(path.read_text(encoding="utf-8"))
        cases = {
            "producer release": ("producer_release", "v9.9.9"),
            "repository identity": ("repository", "datarelay-labs/tampered"),
            "family type": ("family", []),
            "artifact kind type": ("artifact_kind", {}),
            "schema path": ("schema_path", "schemas/other.json"),
            "schema digest": ("schema_sha256", "0" * 64),
            "summary digest": ("summary_sha256", "0" * 64),
            "logical identity": ("logical_id", "0" * 64),
            "record identity": ("record_id", "0" * 64),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                tampered = json.loads(json.dumps(original))
                tampered["records"][0][field] = value
                path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaises(ValidationError):
                    validate_engineering_evidence_store(self.root)

        tampered = json.loads(json.dumps(original))
        tampered["records"][0]["summary"]["duration_seconds"] = 999
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "summary identity"):
            validate_engineering_evidence_store(self.root)

    def test_cli_mcp_web_and_backup_surface_same_bounded_state(self):
        self._import("efficiency", self._efficiency())
        self._write_lifecycle(self.head)
        service = AtlasService(self.root)
        expected = service.engineering_evidence_dashboard(["demo"])
        self.assertEqual(expected["state_counts"]["CURRENT"], 1)

        parser = build_parser()
        args = parser.parse_args(
            [
                "--data-root",
                str(self.root),
                "engineering-evidence",
                "show",
                "--project-id",
                "demo",
            ]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(args.func(args), 0)
        cli_payload = json.loads(output.getvalue())
        self.assertEqual(cli_payload, expected)

        tools = AtlasContextTools(
            retriever_factory=service.project_retriever,
            engineering_evidence_factory=service.engineering_evidence_dashboard,
        )
        names = {item["name"] for item in tools.list_tools(default_read_scopes())}
        self.assertIn("get_engineering_evidence", names)
        mcp_result = tools.call(
            "get_engineering_evidence",
            {"project_ids": ["demo"]},
            scopes=default_read_scopes(),
        )
        self.assertTrue(mcp_result.ok)
        self.assertEqual(mcp_result.data, expected)

        app = create_app(self.root)
        env = {}
        setup_testing_defaults(env)
        env.update(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/engineering-evidence",
                "QUERY_STRING": "",
                "HTTP_HOST": "127.0.0.1:8788",
            }
        )
        state: dict[str, object] = {}

        def start(status, headers):
            state.update(status=status, headers=dict(headers))

        body = b"".join(app(env, start)).decode()
        self.assertEqual(state["status"], "200 OK")
        self.assertIn("Engineering System evidence", body)
        self.assertIn("CURRENT", body)
        self.assertIn(self.head, body)
        self.assertNotIn('"run_id"', body)

        backup = self.base / "backup"
        restored = self.base / "restored"
        backup_result = backup_data_root(self.root, backup)
        self.assertEqual(backup_result["status"], "ok")
        restore_result = restore_test(backup, restored)
        self.assertEqual(restore_result["status"], "ok")
        original_store = validate_engineering_evidence_store(self.root)
        restored_store = validate_engineering_evidence_store(restored)
        self.assertEqual(restored_store, original_store)
        self.assertFalse((restored / "github-lifecycle.json").exists())

    def test_corrupt_store_is_unavailable_not_trusted(self):
        (self.root / FILENAME).write_text("{broken", encoding="utf-8")
        dashboard = engineering_evidence_dashboard(self.root, self.registry)
        self.assertEqual(dashboard["state"], "UNAVAILABLE")
        self.assertEqual(dashboard["authority"], AUTHORITY)


if __name__ == "__main__":
    unittest.main()
