from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from atlas.cli import main
from atlas.data_protection import backup_data_root
from atlas.decision_plane import (
    append_decision_observation,
    build_optional_context_candidates,
    decision_canary_readiness,
    decision_plane_dashboard,
    validate_decision_observation,
)
from atlas.decision_plane_canary import (
    FILENAME as CANARY_FILENAME,
    build_decision_canary_admission,
    decision_canary_dashboard,
    publish_decision_canary_admission,
    validate_decision_canary_admission,
    validate_decision_canary_request,
)
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def replay(
    record_id: str,
    *,
    decision_class: str = "OPTIONAL_CONTEXT_SELECTION",
    current_outcome: str = "VERIFIED_SUCCESS",
    model_outcome: str = "VERIFIED_SUCCESS",
    cost_delta: int = -10,
    wall_delta: int = 0,
    frontier_delta: int = 0,
    retry_delta: int = 0,
    observed_at: str = "2026-09-30T12:00:00Z",
) -> dict:
    return {
        "schema_version": 1,
        "kind": "decision_plane_observation",
        "record_id": record_id,
        "mode": "REPLAY",
        "decision_class": decision_class,
        "observed_at": observed_at,
        "candidate_ids": ["AGENTS.md", "docs/ADR"],
        "required_candidate_ids": ["AGENTS.md"],
        "current_choice_ids": ["AGENTS.md", "docs/ADR"],
        "model_choice_ids": ["AGENTS.md"],
        "model_provider": "jev",
        "model_name": "system-one",
        "model_profile": "bounded-v1",
        "confidence": "0.90",
        "current_outcome": current_outcome,
        "model_outcome": model_outcome,
        "cost_delta_milliunits": cost_delta,
        "wall_time_delta_ms": wall_delta,
        "frontier_call_delta": frontier_delta,
        "retry_delta": retry_delta,
    }


def seed_replay_pass(root: Path) -> None:
    for idx in range(5):
        append_decision_observation(
            root,
            replay(
                f"pass-{idx}",
                observed_at=f"2026-09-30T12:0{idx}:00Z",
            ),
        )


def request_for(
    root: Path,
    *,
    decision_class: str = "OPTIONAL_CONTEXT_SELECTION",
    expected_digest: str | None = None,
    evaluated_at: str = "2026-09-30T16:00:00Z",
    expires_at: str = "2026-10-01T16:00:00Z",
    max_decisions: int = 10,
) -> dict:
    digest = expected_digest or decision_canary_readiness(root)["evidence_digest"]
    return {
        "schema_version": 1,
        "kind": "decision_plane_canary_request",
        "request_id": "request-1",
        "decision_class": decision_class,
        "expected_evidence_digest": digest,
        "scope": {
            "project_id": "datarelay-atlas",
            "path_prefixes": ["atlas"],
            "task_kinds": ["DEVELOPMENT"],
        },
        "max_canary_decisions": max_decisions,
        "evaluated_at": evaluated_at,
        "expires_at": expires_at,
    }


class DecisionCanaryTests(unittest.TestCase):
    def test_generated_mandatory_repository_paths_are_valid_observation_ids(self):
        candidates = build_optional_context_candidates(
            ROOT,
            ["docs/contracts/decision-plane-shadow-replay.md"],
        )
        self.assertIn(".engineering/project.yaml", candidates["required_candidate_ids"])
        observation = replay("mandatory-hidden-path")
        observation["candidate_ids"] = candidates["candidate_ids"]
        observation["required_candidate_ids"] = candidates["required_candidate_ids"]
        observation["current_choice_ids"] = candidates["candidate_ids"]
        observation["model_choice_ids"] = candidates["required_candidate_ids"]
        self.assertEqual(validate_decision_observation(observation)["record_id"], "mandatory-hidden-path")
        schema = json.loads(
            (CONTRACTS / "decision-plane-observation.schema.json").read_text()
        )
        Draft202012Validator(schema).validate(observation)

    def test_public_schema_fixtures_and_runtime_are_valid(self):
        readiness_schema = json.loads(
            (CONTRACTS / "decision-plane-canary-readiness.schema.json").read_text()
        )
        readiness_fixture = json.loads(
            (FIXTURES / "decision-plane-canary-readiness.example.json").read_text()
        )
        request_schema = json.loads(
            (CONTRACTS / "decision-plane-canary-request.schema.json").read_text()
        )
        request_fixture = json.loads(
            (FIXTURES / "decision-plane-canary-request.example.json").read_text()
        )
        admission_schema = json.loads(
            (CONTRACTS / "decision-plane-canary-admission.schema.json").read_text()
        )
        admission_fixture = json.loads(
            (FIXTURES / "decision-plane-canary-admission.example.json").read_text()
        )
        for schema in (readiness_schema, request_schema, admission_schema):
            Draft202012Validator.check_schema(schema)
        Draft202012Validator(readiness_schema).validate(readiness_fixture)
        Draft202012Validator(request_schema).validate(request_fixture)
        registry = Registry().with_resource(
            request_schema["$id"], Resource.from_contents(request_schema)
        )
        Draft202012Validator(admission_schema, registry=registry).validate(admission_fixture)
        self.assertEqual(validate_decision_canary_request(request_fixture), request_fixture)
        self.assertEqual(
            validate_decision_canary_admission(admission_fixture),
            admission_fixture,
        )

    def test_empty_ledger_is_not_ready_for_canary_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = decision_canary_readiness(root)
            self.assertEqual(readiness["state"], "UNKNOWN")
            self.assertEqual(readiness["rollout_state"], "SHADOW")
            self.assertEqual(readiness["authority"], "CANARY_READINESS_EVIDENCE_ONLY")
            self.assertEqual(readiness["activation_authority"], "NO_ACTIVATION_AUTHORITY")
            self.assertEqual(readiness["execution_authority"], "NONE")
            self.assertEqual(readiness["eligible_class_count"], 0)
            self.assertEqual(readiness["eligible_decision_classes"], [])
            self.assertTrue(
                all(
                    item["readiness"] == "CANARY_REQUEST_NOT_ELIGIBLE"
                    for item in readiness["classes"]
                )
            )

    def test_replay_pass_is_request_readiness_not_execution_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_replay_pass(root)
            dashboard = decision_plane_dashboard(root)
            assessment = dashboard["class_assessments"]["OPTIONAL_CONTEXT_SELECTION"]
            self.assertEqual(assessment["assessment"], "REPLAY_PASS")

            readiness = decision_canary_readiness(root)
            classes = {item["decision_class"]: item for item in readiness["classes"]}
            context = classes["OPTIONAL_CONTEXT_SELECTION"]
            focused = classes["FOCUSED_CHECK_SELECTION"]
            self.assertEqual(context["readiness"], "CANARY_REQUEST_ELIGIBLE")
            self.assertEqual(context["replay_assessment"], "REPLAY_PASS")
            self.assertEqual(context["fallback"], "CURRENT_DECISION")
            self.assertEqual(context["candidate_authority"], "ATLAS_VALIDATED_CANDIDATES_ONLY")
            self.assertEqual(context["execution_authority"], "NONE")
            self.assertEqual(focused["readiness"], "CANARY_REQUEST_NOT_ELIGIBLE")
            self.assertEqual(focused["replay_assessment"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(readiness["eligible_decision_classes"], ["OPTIONAL_CONTEXT_SELECTION"])
            self.assertEqual(dashboard["rollout_state"], "SHADOW")
            self.assertEqual(dashboard["activation_authority"], "NO_ACTIVATION_AUTHORITY")
            self.assertEqual(dashboard["canary_readiness"], readiness)

    def test_replay_fail_never_becomes_request_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in range(5):
                append_decision_observation(
                    root,
                    replay(
                        f"fail-{idx}",
                        current_outcome="VERIFIED_SUCCESS",
                        model_outcome="VERIFIED_FAILURE",
                        cost_delta=0,
                        wall_delta=100,
                        retry_delta=1,
                        observed_at=f"2026-09-30T13:0{idx}:00Z",
                    ),
                )
            readiness = decision_canary_readiness(root)
            context = next(
                item
                for item in readiness["classes"]
                if item["decision_class"] == "OPTIONAL_CONTEXT_SELECTION"
            )
            self.assertEqual(context["replay_assessment"], "REPLAY_FAIL")
            self.assertEqual(context["readiness"], "CANARY_REQUEST_NOT_ELIGIBLE")
            self.assertEqual(readiness["eligible_class_count"], 0)

    def test_readiness_digest_is_deterministic_and_changes_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = decision_canary_readiness(root)["evidence_digest"]
            self.assertEqual(first, decision_canary_readiness(root)["evidence_digest"])
            append_decision_observation(root, replay("one"))
            second = decision_canary_readiness(root)["evidence_digest"]
            self.assertNotEqual(first, second)
            self.assertEqual(second, decision_canary_readiness(root)["evidence_digest"])

    def test_scoped_admission_requires_exact_digest_and_replay_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_replay_pass(root)
            admission = build_decision_canary_admission(root, request_for(root))
            self.assertEqual(admission["decision"], "CANARY_ELIGIBLE")
            self.assertEqual(admission["authority"], "CANARY_ADMISSION_ONLY")
            self.assertEqual(admission["activation_authority"], "NO_ACTIVATION_AUTHORITY")
            self.assertEqual(admission["execution_authority"], "NONE")
            self.assertEqual(admission["current_rollout_state"], "SHADOW")
            self.assertEqual(admission["requested_rollout_state"], "CANARY")
            self.assertEqual(admission["scope"]["project_id"], "datarelay-atlas")
            self.assertEqual(admission["max_canary_decisions"], 10)
            self.assertEqual(admission["false_routing_count"], 0)

    def test_digest_mismatch_expiry_and_invalid_budget_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_replay_pass(root)
            mismatch = build_decision_canary_admission(
                root,
                request_for(root, expected_digest="f" * 64),
            )
            self.assertEqual(mismatch["decision"], "CANARY_NOT_ELIGIBLE")
            self.assertIn("EVIDENCE_DIGEST_MISMATCH", mismatch["reasons"])

            expired = build_decision_canary_admission(
                root,
                request_for(
                    root,
                    evaluated_at="2026-10-01T16:00:00Z",
                    expires_at="2026-10-01T16:00:00Z",
                ),
            )
            self.assertEqual(expired["decision"], "CANARY_NOT_ELIGIBLE")
            self.assertIn("REQUEST_EXPIRED", expired["reasons"])

            with self.assertRaises(ValidationError):
                validate_decision_canary_request(request_for(root, max_decisions=101))
            with self.assertRaises(ValidationError):
                validate_decision_canary_request(
                    request_for(
                        root,
                        evaluated_at="2026-09-30T16:00:00Z",
                        expires_at="2026-10-10T16:00:00Z",
                    )
                )

    def test_false_routing_blocks_admission_even_when_replay_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in range(5):
                current = "VERIFIED_SUCCESS"
                model = "VERIFIED_SUCCESS"
                if idx == 0:
                    model = "VERIFIED_FAILURE"
                elif idx == 1:
                    current = "VERIFIED_FAILURE"
                append_decision_observation(
                    root,
                    replay(
                        f"false-route-{idx}",
                        current_outcome=current,
                        model_outcome=model,
                        observed_at=f"2026-09-30T14:0{idx}:00Z",
                    ),
                )
            dashboard = decision_plane_dashboard(root)
            self.assertEqual(
                dashboard["class_assessments"]["OPTIONAL_CONTEXT_SELECTION"]["assessment"],
                "REPLAY_PASS",
            )
            admission = build_decision_canary_admission(root, request_for(root))
            self.assertEqual(admission["decision"], "CANARY_NOT_ELIGIBLE")
            self.assertIn("FALSE_ROUTING_OBSERVED", admission["reasons"])
            self.assertEqual(admission["false_routing_count"], 1)

    def test_tampered_admission_cannot_mint_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_replay_pass(root)
            admission = build_decision_canary_admission(root, request_for(root))

            tampered = dict(admission)
            tampered["current_evidence_digest"] = "f" * 64
            with self.assertRaisesRegex(ValidationError, "derived decision mismatch"):
                validate_decision_canary_admission(tampered)

            tampered = dict(admission)
            tampered["false_routing_count"] = 1
            with self.assertRaisesRegex(ValidationError, "derived decision mismatch"):
                validate_decision_canary_admission(tampered)

            tampered = dict(admission)
            tampered["decision"] = "CANARY_NOT_ELIGIBLE"
            tampered["reasons"] = ["REPLAY_NOT_PASS"]
            with self.assertRaisesRegex(ValidationError, "derived decision mismatch"):
                validate_decision_canary_admission(tampered)

    def test_published_snapshot_becomes_stale_after_replay_evidence_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_replay_pass(root)
            published = publish_decision_canary_admission(root, request_for(root))
            self.assertEqual(published["binding_state"], "CURRENT")
            self.assertEqual(published["effective_decision"], "CANARY_ELIGIBLE")
            append_decision_observation(
                root,
                replay("later", observed_at="2026-09-30T17:00:00Z"),
            )
            stale = decision_canary_dashboard(root)
            self.assertEqual(stale["binding_state"], "STALE")
            self.assertEqual(stale["effective_decision"], "CANARY_NOT_ELIGIBLE")
            self.assertIn("stale", stale["detail"])

    def test_canary_snapshot_is_rebuildable_cache_not_backup_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            seed_replay_pass(root)
            publish_decision_canary_admission(root, request_for(root))
            self.assertTrue((root / CANARY_FILENAME).is_file())
            backup_data_root(root, base / "backup")
            self.assertFalse((base / "backup" / CANARY_FILENAME).exists())

    def test_cli_web_and_mcp_expose_readiness_and_admission_without_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_replay_pass(root)
            request_path = Path(tmp).parent / f"{Path(tmp).name}-canary-request.json"
            request_path.write_text(json.dumps(request_for(root)), encoding="utf-8")
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        main([
                            "--data-root", str(root), "decision-plane", "canary-publish",
                            "--request", str(request_path),
                        ]),
                        0,
                    )
                published = json.loads(out.getvalue())
                self.assertEqual(published["effective_decision"], "CANARY_ELIGIBLE")

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        main(["--data-root", str(root), "decision-plane", "canary-readiness"]),
                        0,
                    )
                readiness = json.loads(out.getvalue())
                self.assertEqual(readiness["eligible_class_count"], 1)
                self.assertEqual(readiness["execution_authority"], "NONE")

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        main(["--data-root", str(root), "decision-plane", "canary-show"]),
                        0,
                    )
                shown = json.loads(out.getvalue())
                self.assertEqual(shown["binding_state"], "CURRENT")
                self.assertEqual(shown["activation_authority"], "NO_ACTIVATION_AUTHORITY")

                svc = AtlasService(root)
                tools = AtlasContextTools(
                    retriever_factory=svc.project_retriever,
                    decision_canary_factory=svc.decision_canary_readiness,
                    decision_canary_admission_factory=svc.decision_canary_dashboard,
                )
                names = {tool["name"] for tool in tools.list_tools(default_read_scopes())}
                self.assertIn("get_decision_canary_readiness", names)
                self.assertIn("get_decision_plane_canary", names)

                result = tools.call(
                    "get_decision_canary_readiness",
                    {},
                    scopes=default_read_scopes(),
                )
                self.assertTrue(result.ok)
                self.assertEqual(result.data["eligible_class_count"], 1)
                admission_result = tools.call(
                    "get_decision_plane_canary",
                    {},
                    scopes=default_read_scopes(),
                )
                self.assertTrue(admission_result.ok)
                self.assertEqual(admission_result.data["effective_decision"], "CANARY_ELIGIBLE")
                self.assertEqual(admission_result.data["execution_authority"], "NONE")

                app = create_app(root)
                env = {}
                setup_testing_defaults(env)
                env.update(
                    {
                        "REQUEST_METHOD": "GET",
                        "PATH_INFO": "/decision-plane",
                        "QUERY_STRING": "",
                        "HTTP_HOST": "127.0.0.1:8788",
                    }
                )
                state = {}

                def start(status, headers):
                    state.update(status=status, headers=dict(headers))

                body = b"".join(app(env, start)).decode()
                self.assertEqual(state["status"], "200 OK")
                self.assertIn("Canary admission readiness", body)
                self.assertIn("CANARY_READINESS_EVIDENCE_ONLY", body)
                self.assertIn("CANARY_REQUEST_ELIGIBLE", body)
                self.assertIn("Canary admission", body)
                self.assertIn("CANARY_ADMISSION_ONLY", body)
                self.assertIn("CANARY_ELIGIBLE", body)
                self.assertIn("binding CURRENT", body)
                self.assertIn("NO_ACTIVATION_AUTHORITY", body)
                self.assertIn("CURRENT_DECISION", body)
            finally:
                request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
