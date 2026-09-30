from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.data_protection import backup_data_root
from atlas.decision_plane import append_decision_observation
from atlas.decision_plane_canary import (
    decision_canary_dashboard,
    publish_decision_canary_admission,
)
from atlas.decision_plane_limited_active import (
    FILENAME,
    commit_optional_context_effect,
    decision_canary_admission_digest,
    limited_active_dashboard,
    validate_optional_context_effect_receipt,
)
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _replay(record_id: str, observed_at: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "decision_plane_observation",
        "record_id": record_id,
        "mode": "REPLAY",
        "decision_class": "OPTIONAL_CONTEXT_SELECTION",
        "observed_at": observed_at,
        "candidate_ids": ["AGENTS.md", "atlas/service.py"],
        "required_candidate_ids": ["AGENTS.md"],
        "current_choice_ids": ["AGENTS.md", "atlas/service.py"],
        "model_choice_ids": ["AGENTS.md"],
        "model_provider": "jev",
        "model_name": "system-one",
        "model_profile": "bounded-v1",
        "confidence": "0.90",
        "current_outcome": "VERIFIED_SUCCESS",
        "model_outcome": "VERIFIED_SUCCESS",
        "cost_delta_milliunits": -10,
        "wall_time_delta_ms": 0,
        "frontier_call_delta": 0,
        "retry_delta": 0,
    }


def _seed_replay_pass(root: Path) -> None:
    for idx in range(5):
        append_decision_observation(
            root,
            _replay(f"replay-{idx}", f"2026-09-30T12:0{idx}:00Z"),
        )


def _publish_admission(root: Path, *, max_decisions: int = 3) -> dict:
    _seed_replay_pass(root)
    from atlas.decision_plane import decision_canary_readiness

    digest = decision_canary_readiness(root)["evidence_digest"]
    published = publish_decision_canary_admission(
        root,
        {
            "schema_version": 1,
            "kind": "decision_plane_canary_request",
            "request_id": "limited-active-canary",
            "decision_class": "OPTIONAL_CONTEXT_SELECTION",
            "expected_evidence_digest": digest,
            "scope": {
                "project_id": "datarelay-atlas",
                "path_prefixes": ["atlas"],
                "task_kinds": ["DEVELOPMENT"],
            },
            "max_canary_decisions": max_decisions,
            "evaluated_at": "2026-09-30T16:00:00Z",
            "expires_at": "2026-10-01T16:00:00Z",
        },
    )
    self_check = published
    assert self_check["binding_state"] == "CURRENT"
    assert self_check["effective_decision"] == "CANARY_ELIGIBLE"
    return self_check["admission"]


def _request(admission: dict, *, activation_id: str = "activation-1", optional_paths=None) -> dict:
    return {
        "schema_version": 1,
        "kind": "decision_plane_optional_context_activation_request",
        "activation_id": activation_id,
        "expected_admission_digest": decision_canary_admission_digest(admission),
        "project_id": "datarelay-atlas",
        "task_kind": "DEVELOPMENT",
        "optional_paths": optional_paths if optional_paths is not None else ["atlas/service.py"],
        "activated_at": "2026-09-30T17:00:00Z",
    }


class RecordingSelector:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def select(self, request):
        self.calls.append(deepcopy(request))
        if isinstance(self.result, BaseException):
            raise self.result
        return deepcopy(self.result)


def _valid_result(selected=None) -> dict:
    return {
        "selected_candidate_ids": selected or [
            ".engineering/project.yaml",
            "AGENTS.md",
        ],
        "provider": "jev",
        "model": "system-one",
        "profile": "bounded-v1",
        "decision_ref": "decision:optional-context:001",
    }


class DecisionPlaneLimitedActiveTests(unittest.TestCase):
    def test_public_receipt_schema_fixture(self):
        schema = json.loads(
            (CONTRACTS / "decision-plane-optional-context-effect-receipt.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "decision-plane-optional-context-effect-receipt.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)

    def test_valid_selector_applies_optional_context_choice_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _publish_admission(root)
            port = RecordingSelector(_valid_result())
            receipt = commit_optional_context_effect(
                root,
                repo_root=ROOT,
                request=_request(admission),
                selector_port=port,
            )
            self.assertEqual(receipt["result"], "APPLIED_CANARY")
            self.assertEqual(
                receipt["selected_candidate_ids"],
                [".engineering/project.yaml", "AGENTS.md"],
            )
            self.assertEqual(receipt["authority"], "OPTIONAL_CONTEXT_SELECTION_ONLY")
            self.assertEqual(receipt["rollout_state"], "LIMITED_ACTIVE")
            for key in (
                "permission_authority",
                "release_authority",
                "deploy_authority",
                "pass_authority",
                "human_required_authority",
            ):
                self.assertEqual(receipt[key], "NONE")
            self.assertEqual(len(port.calls), 1)
            self.assertNotIn("content", json.dumps(port.calls[0]).lower())
            self.assertEqual(port.calls[0]["candidate_ids"], receipt["candidate_ids"])
            self.assertEqual(port.calls[0]["required_candidate_ids"], receipt["required_candidate_ids"])

            body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
            validated = validate_optional_context_effect_receipt(
                body,
                expected_receipt_digest=receipt["receipt_digest"],
            )
            self.assertEqual(validated, body)

    def test_invalid_choices_and_selector_error_fallback_to_full_current_choice(self):
        cases = [
            (
                {
                    **_valid_result(),
                    "selected_candidate_ids": ["atlas/service.py"],
                },
                "CHOICE_MISSING_REQUIRED",
            ),
            (
                {
                    **_valid_result(),
                    "selected_candidate_ids": [
                        ".engineering/project.yaml",
                        "AGENTS.md",
                        "docs/not-a-candidate.md",
                    ],
                },
                "CHOICE_OUTSIDE_CANDIDATES",
            ),
            (
                {
                    **_valid_result(),
                    "selected_candidate_ids": [],
                },
                "CHOICE_EMPTY",
            ),
            (RuntimeError("boom"), "SELECTOR_ERROR"),
        ]
        for idx, (raw, reason) in enumerate(cases):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                admission = _publish_admission(root)
                port = RecordingSelector(raw)
                receipt = commit_optional_context_effect(
                    root,
                    repo_root=ROOT,
                    request=_request(admission, activation_id=f"fallback-{idx}"),
                    selector_port=port,
                )
                self.assertEqual(receipt["result"], "FALLBACK")
                self.assertEqual(receipt["fallback_reason"], reason)
                self.assertEqual(
                    receipt["selected_candidate_ids"],
                    receipt["candidate_ids"],
                )
                self.assertIsNone(receipt["selector_attribution"])
                self.assertEqual(len(port.calls), 1)

    def test_stale_admission_scope_and_window_block_before_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _publish_admission(root)
            append_decision_observation(
                root,
                _replay("later", "2026-09-30T18:00:00Z"),
            )
            port = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "not current"):
                commit_optional_context_effect(
                    root, repo_root=ROOT, request=_request(admission), selector_port=port
                )
            self.assertEqual(port.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _publish_admission(root)
            base = _request(admission)
            cases = [
                ({**base, "project_id": "other-project"}, "outside canary scope"),
                ({**base, "task_kind": "RELEASE"}, "outside canary scope"),
                ({**base, "optional_paths": ["docs/decisions/ADR-0001.md"]}, "outside canary scope"),
                ({**base, "activated_at": "2026-10-01T16:00:00Z"}, "outside admission window"),
            ]
            for request, pattern in cases:
                with self.subTest(pattern=pattern):
                    port = RecordingSelector(_valid_result())
                    with self.assertRaisesRegex(ValidationError, pattern):
                        commit_optional_context_effect(
                            root, repo_root=ROOT, request=request, selector_port=port
                        )
                    self.assertEqual(port.calls, [])

    def test_budget_and_duplicate_activation_fail_closed_before_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _publish_admission(root, max_decisions=1)
            first = RecordingSelector(_valid_result())
            commit_optional_context_effect(
                root, repo_root=ROOT, request=_request(admission), selector_port=first
            )
            replay = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "replay"):
                commit_optional_context_effect(
                    root, repo_root=ROOT, request=_request(admission), selector_port=replay
                )
            self.assertEqual(replay.calls, [])
            second = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "budget is exhausted"):
                commit_optional_context_effect(
                    root,
                    repo_root=ROOT,
                    request=_request(admission, activation_id="activation-2"),
                    selector_port=second,
                )
            self.assertEqual(second.calls, [])

    def test_reservation_survives_crash_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _publish_admission(root, max_decisions=2)
            crashing = RecordingSelector(KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                commit_optional_context_effect(
                    root,
                    repo_root=ROOT,
                    request=_request(admission, activation_id="crash-activation"),
                    selector_port=crashing,
                )
            dashboard = limited_active_dashboard(root, repo_root=ROOT)
            self.assertEqual(dashboard["in_progress_count"], 1)
            replay = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "replay"):
                commit_optional_context_effect(
                    root,
                    repo_root=ROOT,
                    request=_request(admission, activation_id="crash-activation"),
                    selector_port=replay,
                )
            self.assertEqual(replay.calls, [])

    def test_tampered_terminal_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _publish_admission(root)
            commit_optional_context_effect(
                root,
                repo_root=ROOT,
                request=_request(admission),
                selector_port=RecordingSelector(_valid_result()),
            )
            ledger = json.loads((root / FILENAME).read_text())
            ledger["records"][0]["receipt"]["selected_candidate_ids"] = [
                ".engineering/project.yaml",
                "AGENTS.md",
                "atlas/service.py",
            ]
            (root / FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "receipt digest mismatch"):
                limited_active_dashboard(root, repo_root=ROOT)

    def test_module_has_no_concrete_provider_transport_or_retry_loop(self):
        source = (ROOT / "atlas" / "decision_plane_limited_active.py").read_text()
        for forbidden in (
            "subprocess",
            "requests",
            "urllib",
            "socket",
            "while True",
            "agent persist",
        ):
            self.assertNotIn(forbidden, source)

    def test_cli_web_mcp_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            admission = _publish_admission(root)
            commit_optional_context_effect(
                root,
                repo_root=ROOT,
                request=_request(admission),
                selector_port=RecordingSelector(_valid_result()),
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root", str(root),
                        "decision-plane", "limited-active-show",
                    ]),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["terminal_count"], 1)
            self.assertEqual(cli["binding_state"], "CURRENT")

            svc = AtlasService(root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                decision_limited_active_factory=svc.decision_limited_active_dashboard,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_decision_plane_limited_active", names)
            result = tools.call(
                "get_decision_plane_limited_active",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["result_counts"]["APPLIED_CANARY"], 1)

            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update({
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/decision-plane",
                "QUERY_STRING": "",
                "HTTP_HOST": "127.0.0.1:8788",
            })
            state = {}
            def start(status, headers):
                state.update(status=status, headers=dict(headers))
            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Limited-active optional context", body)
            self.assertIn("OPTIONAL_CONTEXT_SELECTION_ONLY", body)
            self.assertIn("LIMITED_ACTIVE", body)

            backup_data_root(root, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
