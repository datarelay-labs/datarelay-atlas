from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.data_protection import backup_data_root
from atlas.instruction_governance import (
    AUTHORITY as AUDIT_AUTHORITY,
    FILENAME as AUDIT_FILENAME,
    LEDGER_KIND as AUDIT_LEDGER_KIND,
    SCHEMA_VERSION as AUDIT_SCHEMA_VERSION,
    instruction_governance_dashboard,
    publish_instruction_governance_disposition,
)
from atlas.instruction_governance_canary import (
    FILENAME,
    build_instruction_governance_canary_record,
    instruction_governance_canary_dashboard,
    record_instruction_governance_canary,
    validate_instruction_governance_canary_record,
)
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _audit(data_root: Path, *, outcome: str = "CANARY_READY") -> dict:
    inventory = instruction_governance_dashboard(
        data_root, repo_root=ROOT
    )["inventory_digest"]
    changes = [{
        "path": "AGENTS.md",
        "before_digest": "a" * 64,
        "after_digest": "b" * 64,
    }] if outcome == "CANARY_READY" else []
    return {
        "audit_identity": "c" * 64,
        "evaluated_at": "2026-09-30T12:00:00Z",
        "authority": AUDIT_AUTHORITY,
        "outcome": outcome,
        "target_repository": "datarelay-labs/datarelay-atlas",
        "target_head": _head(),
        "engineering_system_revision": "d" * 40,
        "model_provider": "openai",
        "model_name": "gpt-5.6-sol",
        "model_profile": "medium",
        "harness_id": "chatgpt-chat",
        "harness_revision": "2026-09-30",
        "trigger_kind": "MANUAL_AUDIT",
        "trigger_revision": "issue-196",
        "inventory_digest": inventory,
        "behavior_results": [
            {"scenario_id": "chat-primary-implementation", "outcome": "PASS"}
        ],
        "missing_mandatory_scenarios": [],
        "candidate_changes": changes,
        "evaluation_ref": "github:issue-196",
        "canonical_mutation": False,
    }


def _write_audit(data_root: Path, audit: dict) -> None:
    (data_root / AUDIT_FILENAME).write_text(
        json.dumps({
            "schema_version": AUDIT_SCHEMA_VERSION,
            "kind": AUDIT_LEDGER_KIND,
            "authority": AUDIT_AUTHORITY,
            "audits": [audit],
        }),
        encoding="utf-8",
    )


def _prepare_candidate(data_root: Path) -> dict:
    audit = _audit(data_root)
    _write_audit(data_root, audit)
    result = publish_instruction_governance_disposition(
        data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
    )
    disposition = result["disposition"]
    assert disposition["disposition"] == "PR_CANDIDATE"
    return disposition


def _observation(disposition: dict, *, canary_id: str = "canary-1", outcomes=None) -> dict:
    checks = [
        {
            "check_id": f"check-{index}",
            "outcome": outcome,
            "evidence_ref": f"evidence:canary:{index}",
        }
        for index, outcome in enumerate(outcomes or ["PASS", "PASS"], start=1)
    ]
    return {
        "schema_version": 1,
        "kind": "instruction_governance_canary_observation",
        "canary_id": canary_id,
        "evaluated_at": "2026-09-30T14:00:00Z",
        "audit_identity": disposition["audit_identity"],
        "disposition_digest": disposition["disposition_digest"],
        "handoff_digest": disposition["pr_handoff"]["handoff_digest"],
        "checks": checks,
    }


class InstructionGovernanceCanaryTests(unittest.TestCase):
    def test_public_schema_fixture_and_runtime_validation(self):
        schema = json.loads(
            (CONTRACTS / "instruction-governance-canary-record.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "instruction-governance-canary-record.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_instruction_governance_canary_record(fixture), fixture)

    def test_pass_canary_emits_only_non_mutating_adoption_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            disposition = _prepare_candidate(data_root)
            record = build_instruction_governance_canary_record(
                data_root,
                repo_root=ROOT,
                observation=_observation(disposition),
            )
            self.assertEqual(record["result"], "PASS")
            request = record["adoption_request"]
            self.assertIsInstance(request, dict)
            self.assertEqual(request["required_route"], "ORDINARY_PR_OR_MANAGED_ADOPTION")
            for key in (
                "mutation_authority", "merge_authority", "release_authority",
                "default_branch_authority",
            ):
                self.assertEqual(request[key], "NONE")
            self.assertEqual(request["target_head"], _head())
            self.assertEqual(request["managed_changes"], disposition["candidate_changes"])

    def test_fail_or_human_required_cannot_request_adoption(self):
        for outcome in ("FAIL", "HUMAN_REQUIRED"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                data_root = Path(tmp)
                disposition = _prepare_candidate(data_root)
                record = build_instruction_governance_canary_record(
                    data_root,
                    repo_root=ROOT,
                    observation=_observation(disposition, outcomes=["PASS", outcome]),
                )
                self.assertEqual(record["result"], outcome)
                self.assertIsNone(record["adoption_request"])

    def test_wrong_binding_and_non_pr_disposition_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            disposition = _prepare_candidate(data_root)
            observation = _observation(disposition)
            observation["handoff_digest"] = "0" * 64
            with self.assertRaisesRegex(ValidationError, "handoff digest mismatch"):
                build_instruction_governance_canary_record(
                    data_root, repo_root=ROOT, observation=observation
                )

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            audit = _audit(data_root, outcome="NO_CHANGE")
            _write_audit(data_root, audit)
            disposition = publish_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )["disposition"]
            observation = {
                "schema_version": 1,
                "kind": "instruction_governance_canary_observation",
                "canary_id": "no-change-canary",
                "evaluated_at": "2026-09-30T14:00:00Z",
                "audit_identity": disposition["audit_identity"],
                "disposition_digest": disposition["disposition_digest"],
                "handoff_digest": "0" * 64,
                "checks": [{"check_id": "x", "outcome": "PASS", "evidence_ref": "evidence:x"}],
            }
            with self.assertRaisesRegex(ValidationError, "requires PR_CANDIDATE"):
                build_instruction_governance_canary_record(
                    data_root, repo_root=ROOT, observation=observation
                )

    def test_duplicate_canary_or_handoff_replay_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            disposition = _prepare_candidate(data_root)
            observation = _observation(disposition)
            record_instruction_governance_canary(
                data_root, repo_root=ROOT, observation=observation
            )
            with self.assertRaisesRegex(ValidationError, "replay"):
                record_instruction_governance_canary(
                    data_root, repo_root=ROOT, observation=observation
                )
            replay = _observation(disposition, canary_id="different-canary")
            with self.assertRaisesRegex(ValidationError, "replay"):
                record_instruction_governance_canary(
                    data_root, repo_root=ROOT, observation=replay
                )

    def test_source_disposition_drift_marks_existing_canary_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            disposition = _prepare_candidate(data_root)
            record_instruction_governance_canary(
                data_root, repo_root=ROOT, observation=_observation(disposition)
            )
            source = json.loads((data_root / AUDIT_FILENAME).read_text())
            source["audits"][0]["inventory_digest"] = "9" * 64
            (data_root / AUDIT_FILENAME).write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaises(ValidationError):
                # The disposition layer itself fails source binding before the canary can
                # claim that stale source evidence is current.
                build_instruction_governance_canary_record(
                    data_root,
                    repo_root=ROOT,
                    observation=_observation(disposition, canary_id="stale-canary"),
                )

    def test_tampered_adoption_request_fails_closed_even_if_resigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            disposition = _prepare_candidate(data_root)
            record_instruction_governance_canary(
                data_root, repo_root=ROOT, observation=_observation(disposition)
            )
            ledger = json.loads((data_root / FILENAME).read_text())
            item = ledger["records"][0]
            request = item["adoption_request"]
            request["managed_changes"][0]["after_digest"] = "8" * 64
            request_body = {key: value for key, value in request.items() if key != "request_digest"}
            request["request_digest"] = hashlib.sha256(
                json.dumps(
                    request_body, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ).encode()
            ).hexdigest()
            (data_root / FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "binding"):
                instruction_governance_canary_dashboard(data_root, repo_root=ROOT)

    def test_cli_web_mcp_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_root = base / "data"
            data_root.mkdir()
            disposition = _prepare_candidate(data_root)
            observation_path = base / "canary.json"
            observation_path.write_text(
                json.dumps(_observation(disposition)), encoding="utf-8"
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root", str(data_root),
                        "instruction-governance", "canary-record",
                        "--observation", str(observation_path),
                    ]),
                    0,
                )
            recorded = json.loads(out.getvalue())
            self.assertEqual(recorded["latest_record"]["result"], "PASS")

            svc = AtlasService(data_root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                instruction_governance_canary_factory=svc.instruction_governance_canary_dashboard,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_instruction_governance_canary", names)
            result = tools.call(
                "get_instruction_governance_canary", {}, scopes=default_read_scopes()
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["binding_state"], "CURRENT")

            app = create_app(data_root)
            env = {}
            setup_testing_defaults(env)
            env.update({
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/instruction-governance",
                "QUERY_STRING": "",
                "HTTP_HOST": "127.0.0.1:8788",
            })
            state = {}
            def start(status, headers):
                state.update(status=status, headers=dict(headers))
            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Canary / adoption gate", body)
            self.assertIn("CANARY_EVIDENCE_ONLY", body)

            backup_data_root(data_root, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
