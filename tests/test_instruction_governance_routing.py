from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.instruction_governance import (
    AUTHORITY,
    FILENAME,
    LEDGER_KIND,
    SCHEMA_VERSION,
    instruction_governance_dashboard,
    instruction_governance_routing,
)
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()


def _audit(data_root: Path, outcome: str) -> dict:
    inventory = instruction_governance_dashboard(
        data_root,
        repo_root=ROOT,
    )["inventory_digest"]
    changes = (
        [
            {
                "path": "AGENTS.md",
                "before_digest": "a" * 64,
                "after_digest": "b" * 64,
            }
        ]
        if outcome in {"CANARY_READY", "REJECTED"}
        else []
    )
    return {
        "audit_identity": "c" * 64,
        "evaluated_at": "2026-09-30T12:00:00Z",
        "authority": AUTHORITY,
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
        "trigger_revision": "issue-190",
        "inventory_digest": inventory,
        "behavior_results": [
            {"scenario_id": "chat-primary-implementation", "outcome": "PASS"}
        ],
        "missing_mandatory_scenarios": [],
        "candidate_changes": changes,
        "evaluation_ref": "github:issue-190",
        "canonical_mutation": False,
    }


def _write_ledger(data_root: Path, audit: dict) -> None:
    (data_root / FILENAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": LEDGER_KIND,
                "authority": AUTHORITY,
                "audits": [audit],
            }
        ),
        encoding="utf-8",
    )


class InstructionGovernanceRoutingTests(unittest.TestCase):
    def test_public_schema_fixture(self):
        schema = json.loads(
            (CONTRACTS / "instruction-governance-routing.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "instruction-governance-routing.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)

    def test_four_audit_outcomes_map_deterministically(self):
        expected = {
            "NO_CHANGE": "NO_CHANGE",
            "REJECTED": "REJECTED",
            "HUMAN_REQUIRED": "HUMAN_REQUIRED",
            "CANARY_READY": "CANARY_PR_REQUIRED",
        }
        for outcome, route in expected.items():
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                audit = _audit(root, outcome)
                _write_ledger(root, audit)
                result = instruction_governance_routing(root, repo_root=ROOT)
                self.assertEqual(result["state"], "CURRENT")
                self.assertEqual(result["route_action"], route)
                self.assertEqual(result["reasons"], [])
                self.assertEqual(result["mutation_authority"], "NONE")
                self.assertEqual(result["audit_identity"], audit["audit_identity"])
                self.assertEqual(result["target_head"], _head())
                if outcome == "CANARY_READY":
                    self.assertEqual(
                        result["next_effect"],
                        "SEPARATE_CANARY_AND_PR_GOVERNANCE_REQUIRED",
                    )
                else:
                    self.assertEqual(result["next_effect"], "NONE")

    def test_no_audit_and_stale_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(empty["state"], "UNKNOWN")
            self.assertEqual(empty["route_action"], "HUMAN_REQUIRED")
            self.assertEqual(empty["reasons"], ["NO_AUDIT_EVIDENCE"])

            audit = _audit(root, "CANARY_READY")
            audit["target_head"] = "0" * 40
            _write_ledger(root, audit)
            stale_head = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(stale_head["route_action"], "HUMAN_REQUIRED")
            self.assertIn("TARGET_HEAD_STALE", stale_head["reasons"])

            audit = _audit(root, "CANARY_READY")
            audit["inventory_digest"] = "f" * 64
            _write_ledger(root, audit)
            stale_inventory = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(stale_inventory["route_action"], "HUMAN_REQUIRED")
            self.assertIn("MANAGED_INVENTORY_STALE", stale_inventory["reasons"])

    def test_tampered_stored_audit_cannot_mint_canary_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = _audit(root, "CANARY_READY")
            audit["canonical_mutation"] = True
            _write_ledger(root, audit)
            result = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(result["route_action"], "HUMAN_REQUIRED")
            self.assertEqual(result["reasons"], ["AUDIT_EVIDENCE_INVALID"])

            audit = _audit(root, "CANARY_READY")
            audit["candidate_changes"][0]["after_digest"] = "not-a-digest"
            _write_ledger(root, audit)
            result = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(result["route_action"], "HUMAN_REQUIRED")
            self.assertEqual(result["reasons"], ["AUDIT_EVIDENCE_INVALID"])

    def test_inconsistent_candidate_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = _audit(root, "CANARY_READY")
            audit["candidate_changes"] = []
            _write_ledger(root, audit)
            result = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(result["route_action"], "HUMAN_REQUIRED")
            self.assertIn("CANARY_READY_WITHOUT_CHANGES", result["reasons"])

            audit = _audit(root, "NO_CHANGE")
            audit["candidate_changes"] = [
                {
                    "path": "AGENTS.md",
                    "before_digest": "a" * 64,
                    "after_digest": "b" * 64,
                }
            ]
            _write_ledger(root, audit)
            result = instruction_governance_routing(root, repo_root=ROOT)
            self.assertEqual(result["route_action"], "HUMAN_REQUIRED")
            self.assertIn("NO_CHANGE_WITH_CANDIDATE_CHANGES", result["reasons"])

    def test_cli_web_and_mcp_expose_same_current_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_ledger(root, _audit(root, "CANARY_READY"))
            svc = AtlasService(root)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(
                        [
                            "--data-root",
                            str(root),
                            "instruction-governance",
                            "route",
                        ]
                    ),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["route_action"], "CANARY_PR_REQUIRED")

            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                instruction_governance_routing_factory=svc.instruction_governance_routing,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_instruction_governance_routing", names)
            mcp = tools.call(
                "get_instruction_governance_routing",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(mcp.ok)
            self.assertEqual(mcp.data["route_action"], "CANARY_PR_REQUIRED")

            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/instruction-governance",
                    "QUERY_STRING": "",
                    "HTTP_HOST": "127.0.0.1:8788",
                }
            )
            state = {}

            def start(status, headers):
                state.update(status=status, headers=dict(headers))

            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Candidate routing", body)
            self.assertIn("CANARY_PR_REQUIRED", body)
            self.assertIn("ROUTING_ADVISORY_ONLY", body)
            self.assertIn("Mutation authority", body)


if __name__ == "__main__":
    unittest.main()
