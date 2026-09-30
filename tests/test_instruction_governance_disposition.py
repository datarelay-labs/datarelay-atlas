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
    AUTHORITY,
    DISPOSITION_FILENAME,
    FILENAME,
    LEDGER_KIND,
    SCHEMA_VERSION,
    build_instruction_governance_disposition,
    instruction_governance_dashboard,
    instruction_governance_disposition_dashboard,
    publish_instruction_governance_disposition,
    validate_instruction_governance_disposition,
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


def _audit(data_root: Path, outcome: str = "CANARY_READY") -> dict:
    inventory = instruction_governance_dashboard(
        data_root, repo_root=ROOT
    )["inventory_digest"]
    changes = (
        [{
            "path": "AGENTS.md",
            "before_digest": "a" * 64,
            "after_digest": "b" * 64,
        }]
        if outcome in {"CANARY_READY", "REJECTED"}
        else []
    )
    behavior_outcome = "FAIL" if outcome == "REJECTED" else "PASS"
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
        "trigger_revision": "issue-191",
        "inventory_digest": inventory,
        "behavior_results": [
            {"scenario_id": "chat-primary-implementation", "outcome": behavior_outcome}
        ],
        "missing_mandatory_scenarios": [],
        "candidate_changes": changes,
        "evaluation_ref": "github:issue-191",
        "canonical_mutation": False,
    }


def _write_audit(data_root: Path, audit: dict) -> None:
    (data_root / FILENAME).write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "kind": LEDGER_KIND,
            "authority": AUTHORITY,
            "audits": [audit],
        }),
        encoding="utf-8",
    )


class InstructionGovernanceDispositionTests(unittest.TestCase):
    def test_public_schema_fixture_and_runtime_validation(self):
        schema = json.loads(
            (CONTRACTS / "instruction-governance-disposition.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "instruction-governance-disposition.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_instruction_governance_disposition(fixture), fixture)

    def test_four_audit_outcomes_map_to_bounded_dispositions(self):
        expected = {
            "NO_CHANGE": "NO_CHANGE",
            "CANARY_READY": "PR_CANDIDATE",
            "HUMAN_REQUIRED": "HUMAN_REQUIRED",
            "REJECTED": "REJECTED",
        }
        for outcome, disposition in expected.items():
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                data_root = Path(tmp)
                audit = _audit(data_root, outcome)
                _write_audit(data_root, audit)
                result = build_instruction_governance_disposition(
                    data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
                )
                self.assertEqual(result["disposition"], disposition)
                self.assertEqual(result["mutation_authority"], "NONE")
                self.assertEqual(result["audit_identity"], audit["audit_identity"])
                if disposition == "PR_CANDIDATE":
                    handoff = result["pr_handoff"]
                    self.assertIsInstance(handoff, dict)
                    self.assertEqual(handoff["required_route"], "ORDINARY_PR_OR_MANAGED_ADOPTION")
                    self.assertEqual(handoff["merge_authority"], "NONE")
                    self.assertEqual(handoff["release_authority"], "NONE")
                    self.assertEqual(handoff["target_head"], _head())
                else:
                    self.assertIsNone(result["pr_handoff"])

    def test_pr_candidate_requires_all_pass_nonempty_changes_and_no_missing_mandatory(self):
        cases = [
            ("behavior", lambda audit: audit["behavior_results"][0].update(outcome="UNKNOWN"), "PR_CANDIDATE_BEHAVIOR_NOT_ALL_PASS"),
            ("changes", lambda audit: audit.update(candidate_changes=[]), "PR_CANDIDATE_WITHOUT_CHANGES"),
            ("missing", lambda audit: audit.update(missing_mandatory_scenarios=["mandatory-one"]), "PR_CANDIDATE_MISSING_MANDATORY_SCENARIOS"),
        ]
        for name, mutate, reason in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                data_root = Path(tmp)
                audit = _audit(data_root)
                mutate(audit)
                _write_audit(data_root, audit)
                result = build_instruction_governance_disposition(
                    data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
                )
                self.assertEqual(result["disposition"], "HUMAN_REQUIRED")
                self.assertIn(reason, result["reasons"])
                self.assertIsNone(result["pr_handoff"])

    def test_stale_head_or_inventory_cannot_mint_pr_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            audit = _audit(data_root)
            audit["target_head"] = "0" * 40
            _write_audit(data_root, audit)
            stale = build_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )
            self.assertEqual(stale["disposition"], "HUMAN_REQUIRED")
            self.assertIn("TARGET_HEAD_STALE", stale["reasons"])

            audit = _audit(data_root)
            audit["inventory_digest"] = "f" * 64
            _write_audit(data_root, audit)
            stale = build_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )
            self.assertEqual(stale["disposition"], "HUMAN_REQUIRED")
            self.assertIn("MANAGED_INVENTORY_STALE", stale["reasons"])

    def test_publish_duplicate_is_noop_and_source_tamper_fails_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            audit = _audit(data_root)
            _write_audit(data_root, audit)
            first = publish_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )
            self.assertEqual(first["state"], "PUBLISHED")
            duplicate = publish_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )
            self.assertEqual(duplicate["state"], "DUPLICATE_NOOP")
            dashboard = instruction_governance_disposition_dashboard(
                data_root, repo_root=ROOT
            )
            self.assertEqual(dashboard["binding_state"], "CURRENT")
            self.assertEqual(dashboard["disposition_count"], 1)

            source = json.loads((data_root / FILENAME).read_text())
            source["audits"][0]["evaluation_ref"] = "github:issue-forged"
            (data_root / FILENAME).write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "source binding"):
                instruction_governance_disposition_dashboard(data_root, repo_root=ROOT)

    def test_tampered_disposition_digest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            audit = _audit(data_root)
            _write_audit(data_root, audit)
            publish_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )
            ledger = json.loads((data_root / DISPOSITION_FILENAME).read_text())
            ledger["dispositions"][0]["disposition_digest"] = "0" * 64
            (data_root / DISPOSITION_FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "digest mismatch"):
                instruction_governance_disposition_dashboard(data_root, repo_root=ROOT)

    def test_resigned_tampered_pr_handoff_cannot_drift_from_audit_bound_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            audit = _audit(data_root)
            _write_audit(data_root, audit)
            publish_instruction_governance_disposition(
                data_root, repo_root=ROOT, audit_identity=audit["audit_identity"]
            )
            ledger = json.loads((data_root / DISPOSITION_FILENAME).read_text())
            item = ledger["dispositions"][0]
            handoff = item["pr_handoff"]
            handoff["managed_changes"][0]["after_digest"] = "9" * 64
            handoff_body = {key: value for key, value in handoff.items() if key != "handoff_digest"}
            handoff["handoff_digest"] = hashlib.sha256(
                json.dumps(
                    handoff_body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            disposition_body = {
                key: value for key, value in item.items() if key != "disposition_digest"
            }
            item["disposition_digest"] = hashlib.sha256(
                json.dumps(
                    disposition_body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            (data_root / DISPOSITION_FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "handoff binding"):
                instruction_governance_disposition_dashboard(data_root, repo_root=ROOT)

    def test_cli_web_mcp_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_root = base / "data"
            data_root.mkdir()
            audit = _audit(data_root)
            _write_audit(data_root, audit)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root", str(data_root),
                        "instruction-governance", "disposition-publish",
                        "--audit-identity", audit["audit_identity"],
                    ]),
                    0,
                )
            published = json.loads(out.getvalue())
            self.assertEqual(published["disposition"]["disposition"], "PR_CANDIDATE")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root", str(data_root),
                        "instruction-governance", "disposition-show",
                    ]),
                    0,
                )
            shown = json.loads(out.getvalue())
            self.assertEqual(shown["disposition_count"], 1)

            svc = AtlasService(data_root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                instruction_governance_disposition_factory=svc.instruction_governance_disposition_dashboard,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_instruction_governance_disposition", names)
            result = tools.call(
                "get_instruction_governance_disposition",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["latest_disposition"]["disposition"], "PR_CANDIDATE")

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
            self.assertIn("Disposition / PR handoff", body)
            self.assertIn("PR_CANDIDATE", body)
            self.assertIn("PR_HANDOFF_ADVISORY_ONLY", body)

            backup_data_root(data_root, base / "backup")
            self.assertFalse((base / "backup" / DISPOSITION_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
