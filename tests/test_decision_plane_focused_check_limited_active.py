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
from atlas.decision_plane import (
    append_decision_observation,
    decision_canary_readiness,
)
from atlas.decision_plane_canary import publish_decision_canary_admission
from atlas.decision_plane_focused_check_limited_active import (
    FILENAME,
    commit_focused_check_effect,
    decision_canary_admission_digest,
    focused_check_limited_active_dashboard,
    validate_focused_check_effect_receipt,
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
        "decision_class": "FOCUSED_CHECK_SELECTION",
        "observed_at": observed_at,
        "candidate_ids": ["FAST", "TERM"],
        "required_candidate_ids": ["TERM"],
        "current_choice_ids": ["FAST", "TERM"],
        "model_choice_ids": ["TERM"],
        "model_provider": "jev",
        "model_name": "system-one",
        "model_profile": "bounded-v1",
        "confidence": "0.90",
        "current_outcome": "VERIFIED_SUCCESS",
        "model_outcome": "VERIFIED_SUCCESS",
        "cost_delta_milliunits": -10,
        "wall_time_delta_ms": -10,
        "frontier_call_delta": 0,
        "retry_delta": 0,
    }


def _publish_admission(
    root: Path,
    *,
    max_decisions: int = 3,
    path_prefixes: list[str] | None = None,
) -> dict:
    for idx in range(5):
        append_decision_observation(
            root,
            _replay(f"focused-{idx}", f"2026-09-30T12:0{idx}:00Z"),
        )
    digest = decision_canary_readiness(root)["evidence_digest"]
    dashboard = publish_decision_canary_admission(
        root,
        {
            "schema_version": 1,
            "kind": "decision_plane_canary_request",
            "request_id": "focused-check-canary",
            "decision_class": "FOCUSED_CHECK_SELECTION",
            "expected_evidence_digest": digest,
            "scope": {
                "project_id": "datarelay-atlas",
                "path_prefixes": path_prefixes or ["src", "docs"],
                "task_kinds": ["DEVELOPMENT"],
            },
            "max_canary_decisions": max_decisions,
            "evaluated_at": "2026-09-30T16:00:00Z",
            "expires_at": "2026-10-01T16:00:00Z",
        },
    )
    assert dashboard["binding_state"] == "CURRENT"
    assert dashboard["effective_decision"] == "CANARY_ELIGIBLE"
    return dashboard["admission"]


def _repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / ".engineering").mkdir(parents=True)
    (repo / ".engineering" / "tests.yaml").write_text(
        """version: 1
paths:
  "src/**":
    domains: [code]
  "docs/**":
    domains: [docs]
scenarios:
  - id: FAST
    name: Fast focused check
    level: unit
    domains: [code]
    triggers: [affected]
    platforms: [linux]
    command: "echo fast"
    invariants: ["fast"]
    release_gate: false
  - id: TERM
    name: Terminal required check
    level: static
    domains: [code]
    triggers: [affected]
    platforms: [linux]
    command: "echo terminal"
    invariants: ["terminal"]
    release_gate: true
""",
        encoding="utf-8",
    )
    return repo


def _request(
    admission: dict,
    *,
    activation_id: str = "focused-activation-1",
    changed_paths: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "kind": "decision_plane_focused_check_activation_request",
        "activation_id": activation_id,
        "expected_admission_digest": decision_canary_admission_digest(admission),
        "project_id": "datarelay-atlas",
        "task_kind": "DEVELOPMENT",
        "changed_paths": changed_paths or ["src/module.py"],
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


def _valid_result(selected: list[str] | None = None) -> dict:
    return {
        "selected_candidate_ids": selected or ["TERM"],
        "provider": "jev",
        "model": "system-one",
        "profile": "bounded-v1",
        "decision_ref": "decision:focused-check:001",
    }


class FocusedCheckLimitedActiveTests(unittest.TestCase):
    def test_valid_selector_may_narrow_nonterminal_but_preserves_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data)
            port = RecordingSelector(_valid_result(["TERM"]))
            receipt = commit_focused_check_effect(
                data,
                repo_root=repo,
                request=_request(admission),
                selector_port=port,
            )
            self.assertEqual(receipt["result"], "APPLIED_CANARY")
            self.assertEqual(receipt["candidate_ids"], ["FAST", "TERM"])
            self.assertEqual(receipt["terminal_required_ids"], ["TERM"])
            self.assertEqual(receipt["selected_candidate_ids"], ["TERM"])
            self.assertEqual(receipt["authority"], "FOCUSED_CHECK_SELECTION_ONLY")
            self.assertEqual(len(port.calls), 1)
            call = port.calls[0]
            self.assertEqual(call["terminal_required_ids"], ["TERM"])
            self.assertEqual(
                {item["id"] for item in call["candidates"]},
                {"FAST", "TERM"},
            )
            serialized = json.dumps(call).lower()
            self.assertNotIn("echo fast", serialized)
            self.assertNotIn("echo terminal", serialized)
            self.assertNotIn('"command"', serialized)
            body = {
                key: value
                for key, value in receipt.items()
                if key != "receipt_digest"
            }
            self.assertEqual(
                validate_focused_check_effect_receipt(
                    body,
                    expected_receipt_digest=receipt["receipt_digest"],
                ),
                body,
            )

    def test_missing_terminal_invalid_and_error_fall_back_to_all_affected(self):
        cases = [
            (_valid_result(["FAST"]), "CHOICE_MISSING_TERMINAL_REQUIRED"),
            (_valid_result(["TERM", "OTHER"]), "CHOICE_OUTSIDE_CANDIDATES"),
            ({**_valid_result(), "selected_candidate_ids": []}, "CHOICE_EMPTY"),
            (RuntimeError("boom"), "SELECTOR_ERROR"),
        ]
        for idx, (raw, reason) in enumerate(cases):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                data = base / "data"
                data.mkdir()
                repo = _repo(base)
                admission = _publish_admission(data)
                port = RecordingSelector(raw)
                receipt = commit_focused_check_effect(
                    data,
                    repo_root=repo,
                    request=_request(
                        admission,
                        activation_id=f"fallback-{idx}",
                    ),
                    selector_port=port,
                )
                self.assertEqual(receipt["result"], "FALLBACK")
                self.assertEqual(receipt["fallback_reason"], reason)
                self.assertEqual(
                    receipt["selected_candidate_ids"],
                    ["FAST", "TERM"],
                )
                self.assertEqual(receipt["terminal_required_ids"], ["TERM"])
                self.assertIsNone(receipt["selector_attribution"])
                self.assertEqual(len(port.calls), 1)

    def test_no_affected_candidate_is_no_effect_without_selector_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data)
            port = RecordingSelector(_valid_result())
            receipt = commit_focused_check_effect(
                data,
                repo_root=repo,
                request=_request(
                    admission,
                    activation_id="no-effect",
                    changed_paths=["docs/readme.md"],
                ),
                selector_port=port,
            )
            self.assertEqual(receipt["result"], "NO_EFFECT")
            self.assertEqual(receipt["candidate_ids"], [])
            self.assertEqual(receipt["terminal_required_ids"], [])
            self.assertEqual(receipt["selected_candidate_ids"], [])
            self.assertEqual(receipt["fallback_reason"], "NO_AFFECTED_CHECKS")
            self.assertEqual(port.calls, [])

    def test_stale_scope_and_window_fail_before_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data)
            append_decision_observation(
                data,
                _replay("newer", "2026-09-30T18:00:00Z"),
            )
            port = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "not current"):
                commit_focused_check_effect(
                    data,
                    repo_root=repo,
                    request=_request(admission),
                    selector_port=port,
                )
            self.assertEqual(port.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data)
            cases = [
                (
                    {**_request(admission), "project_id": "other-project"},
                    "outside canary scope",
                ),
                (
                    {**_request(admission), "task_kind": "RELEASE"},
                    "outside canary scope",
                ),
                (
                    {
                        **_request(admission),
                        "changed_paths": ["outside/file.py"],
                    },
                    "outside canary scope",
                ),
                (
                    {
                        **_request(admission),
                        "activated_at": "2026-10-01T16:00:00Z",
                    },
                    "outside admission window",
                ),
            ]
            for request, pattern in cases:
                port = RecordingSelector(_valid_result())
                with self.subTest(pattern=pattern), self.assertRaisesRegex(
                    ValidationError, pattern
                ):
                    commit_focused_check_effect(
                        data,
                        repo_root=repo,
                        request=request,
                        selector_port=port,
                    )
                self.assertEqual(port.calls, [])

    def test_duplicate_activation_and_budget_fail_before_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data, max_decisions=1)
            first = RecordingSelector(_valid_result())
            commit_focused_check_effect(
                data,
                repo_root=repo,
                request=_request(admission),
                selector_port=first,
            )
            replay = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "replay"):
                commit_focused_check_effect(
                    data,
                    repo_root=repo,
                    request=_request(admission),
                    selector_port=replay,
                )
            self.assertEqual(replay.calls, [])

            second = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "budget is exhausted"):
                commit_focused_check_effect(
                    data,
                    repo_root=repo,
                    request=_request(
                        admission,
                        activation_id="focused-activation-2",
                    ),
                    selector_port=second,
                )
            self.assertEqual(second.calls, [])

    def test_crash_reservation_blocks_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data, max_decisions=2)
            crashing = RecordingSelector(KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                commit_focused_check_effect(
                    data,
                    repo_root=repo,
                    request=_request(
                        admission,
                        activation_id="crash-activation",
                    ),
                    selector_port=crashing,
                )
            dashboard = focused_check_limited_active_dashboard(
                data,
                repo_root=repo,
            )
            self.assertEqual(dashboard["in_progress_count"], 1)
            replay = RecordingSelector(_valid_result())
            with self.assertRaisesRegex(ValidationError, "replay"):
                commit_focused_check_effect(
                    data,
                    repo_root=repo,
                    request=_request(
                        admission,
                        activation_id="crash-activation",
                    ),
                    selector_port=replay,
                )
            self.assertEqual(replay.calls, [])

    def test_tampered_terminal_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            repo = _repo(base)
            admission = _publish_admission(data)
            commit_focused_check_effect(
                data,
                repo_root=repo,
                request=_request(admission),
                selector_port=RecordingSelector(_valid_result()),
            )
            ledger = json.loads((data / FILENAME).read_text())
            ledger["records"][0]["receipt"]["selected_candidate_ids"] = [
                "FAST",
                "TERM",
            ]
            (data / FILENAME).write_text(
                json.dumps(ledger),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError,
                "receipt digest mismatch",
            ):
                focused_check_limited_active_dashboard(
                    data,
                    repo_root=repo,
                )

    def test_public_schema_fixture_and_runtime_validation(self):
        schema = json.loads(
            (
                CONTRACTS
                / "decision-plane-focused-check-effect-receipt.schema.json"
            ).read_text()
        )
        fixture = json.loads(
            (
                FIXTURES
                / "decision-plane-focused-check-effect-receipt.example.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        expected_digest = hashlib.sha256(
            json.dumps(
                fixture,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            validate_focused_check_effect_receipt(
                fixture,
                expected_receipt_digest=expected_digest,
            ),
            fixture,
        )

    def test_cli_web_mcp_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            admission = _publish_admission(
                data,
                path_prefixes=["atlas"],
            )
            selected = [
                "ATLAS-STATIC-001",
                "ATLAS-CONTRACT-001",
                "ATLAS-UNIT-001",
                "ATLAS-INDEPENDENCE-001",
            ]
            receipt = commit_focused_check_effect(
                data,
                repo_root=ROOT,
                request=_request(
                    admission,
                    activation_id="surface-focused",
                    changed_paths=["atlas/service.py"],
                ),
                selector_port=RecordingSelector(
                    _valid_result(selected)
                ),
            )
            self.assertEqual(receipt["result"], "APPLIED_CANARY")
            self.assertEqual(
                set(receipt["terminal_required_ids"]),
                set(receipt["selected_candidate_ids"]),
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root",
                        str(data),
                        "decision-plane",
                        "focused-check-limited-active-show",
                    ]),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["terminal_count"], 1)
            self.assertEqual(cli["binding_state"], "CURRENT")
            self.assertEqual(
                cli["authority"],
                "FOCUSED_CHECK_SELECTION_ONLY",
            )

            svc = AtlasService(data)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                decision_focused_check_limited_active_factory=(
                    svc.decision_focused_check_limited_active_dashboard
                ),
            )
            names = {
                item["name"]
                for item in tools.list_tools(default_read_scopes())
            }
            self.assertIn(
                "get_decision_plane_focused_check_limited_active",
                names,
            )
            result = tools.call(
                "get_decision_plane_focused_check_limited_active",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(
                result.data["result_counts"]["APPLIED_CANARY"],
                1,
            )

            app = create_app(data)
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
            self.assertIn("Limited-active focused checks", body)
            self.assertIn("FOCUSED_CHECK_SELECTION_ONLY", body)
            self.assertIn("terminal/release-required", body)

            backup_data_root(data, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())

    def test_module_has_no_transport_test_execution_or_retry_loop(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "atlas"
            / "decision_plane_focused_check_limited_active.py"
        ).read_text()
        for forbidden in (
            "subprocess",
            "requests",
            "urllib",
            "socket",
            "while True",
            "agent persist",
            "shell=True",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
