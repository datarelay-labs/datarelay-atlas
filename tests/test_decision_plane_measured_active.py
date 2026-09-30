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
    commit_focused_check_effect,
)
from atlas.decision_plane_limited_active import (
    FILENAME as OPTIONAL_EFFECT_FILENAME,
    commit_optional_context_effect,
)
from atlas.decision_plane_measured_active import (
    FILENAME,
    decision_class_policy,
    measured_active_dashboard,
    record_measured_active_observation,
    validate_measured_active_record,
)
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _replay(
    decision_class: str,
    record_id: str,
    observed_at: str,
) -> dict:
    if decision_class == "OPTIONAL_CONTEXT_SELECTION":
        candidates = ["AGENTS.md", "atlas/service.py"]
        required = ["AGENTS.md"]
    else:
        candidates = ["ATLAS-STATIC-001", "ATLAS-UNIT-001"]
        required = ["ATLAS-STATIC-001"]
    return {
        "schema_version": 1,
        "kind": "decision_plane_observation",
        "record_id": record_id,
        "mode": "REPLAY",
        "decision_class": decision_class,
        "observed_at": observed_at,
        "candidate_ids": candidates,
        "required_candidate_ids": required,
        "current_choice_ids": candidates,
        "model_choice_ids": required,
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


def _admission(
    root: Path,
    decision_class: str,
    *,
    max_decisions: int = 8,
) -> dict:
    for idx in range(5):
        append_decision_observation(
            root,
            _replay(
                decision_class,
                f"{decision_class.lower()}-{idx}",
                f"2026-09-30T12:0{idx}:00Z",
            ),
        )
    evidence = decision_canary_readiness(root)["evidence_digest"]
    result = publish_decision_canary_admission(
        root,
        {
            "schema_version": 1,
            "kind": "decision_plane_canary_request",
            "request_id": f"measured-{decision_class.lower()}",
            "decision_class": decision_class,
            "expected_evidence_digest": evidence,
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
    self_check = result
    assert self_check["binding_state"] == "CURRENT"
    assert self_check["effective_decision"] == "CANARY_ELIGIBLE"
    return self_check["admission"]


class EchoSelector:
    def __init__(self, *, invalid: bool = False):
        self.invalid = invalid
        self.calls = []

    def select(self, request):
        self.calls.append(deepcopy(request))
        selected = (
            []
            if self.invalid
            else list(request["candidate_ids"])
        )
        return {
            "selected_candidate_ids": selected,
            "provider": "jev",
            "model": "system-one",
            "profile": "bounded-v1",
            "decision_ref": "decision:measured:001",
        }


def _optional_effect(
    root: Path,
    admission: dict,
    activation_id: str,
    selector: EchoSelector,
) -> dict:
    from atlas.decision_plane_limited_active import (
        decision_canary_admission_digest,
    )

    return commit_optional_context_effect(
        root,
        repo_root=ROOT,
        request={
            "schema_version": 1,
            "kind": "decision_plane_optional_context_activation_request",
            "activation_id": activation_id,
            "expected_admission_digest": (
                decision_canary_admission_digest(admission)
            ),
            "project_id": "datarelay-atlas",
            "task_kind": "DEVELOPMENT",
            "optional_paths": ["atlas/service.py"],
            "activated_at": "2026-09-30T17:00:00Z",
        },
        selector_port=selector,
    )


def _focused_effect(
    root: Path,
    admission: dict,
    activation_id: str,
    selector: EchoSelector,
) -> dict:
    from atlas.decision_plane_focused_check_limited_active import (
        decision_canary_admission_digest,
    )

    return commit_focused_check_effect(
        root,
        repo_root=ROOT,
        request={
            "schema_version": 1,
            "kind": "decision_plane_focused_check_activation_request",
            "activation_id": activation_id,
            "expected_admission_digest": (
                decision_canary_admission_digest(admission)
            ),
            "project_id": "datarelay-atlas",
            "task_kind": "DEVELOPMENT",
            "changed_paths": ["atlas/service.py"],
            "activated_at": "2026-09-30T17:00:00Z",
        },
        selector_port=selector,
    )


def _measurement(
    receipt: dict,
    measurement_id: str,
    *,
    baseline: str = "VERIFIED_SUCCESS",
    active: str = "VERIFIED_SUCCESS",
    cost_delta: int = -10,
    wall_delta: int = -10,
) -> dict:
    return {
        "schema_version": 1,
        "kind": "decision_plane_measured_active_observation",
        "measurement_id": measurement_id,
        "decision_class": receipt["decision_class"],
        "source_activation_id": receipt["activation_id"],
        "expected_receipt_digest": receipt["receipt_digest"],
        "observed_at": "2026-09-30T18:00:00Z",
        "baseline_outcome": baseline,
        "active_outcome": active,
        "cost_delta_milliunits": cost_delta,
        "wall_time_delta_ms": wall_delta,
        "frontier_call_delta": 0,
        "retry_delta": 0,
    }


class DecisionPlaneMeasuredActiveTests(unittest.TestCase):
    def test_quality_regression_rolls_back_next_optional_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _admission(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
                max_decisions=3,
            )
            first_selector = EchoSelector()
            first = _optional_effect(
                root,
                admission,
                "optional-first",
                first_selector,
            )
            self.assertEqual(first["result"], "APPLIED_CANARY")
            record_measured_active_observation(
                root,
                _measurement(
                    first,
                    "measure-regression",
                    active="VERIFIED_FAILURE",
                ),
            )
            policy = decision_class_policy(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
            )
            self.assertEqual(
                policy["effective_state"],
                "ROLLBACK_TO_CURRENT",
            )
            self.assertEqual(policy["quality_regression_count"], 1)

            second_selector = EchoSelector()
            second = _optional_effect(
                root,
                admission,
                "optional-after-rollback",
                second_selector,
            )
            self.assertEqual(second_selector.calls, [])
            self.assertEqual(second["result"], "FALLBACK")
            self.assertEqual(
                second["fallback_reason"],
                "MEASURED_ROLLBACK_ACTIVE",
            )
            self.assertEqual(
                second["selected_candidate_ids"],
                second["candidate_ids"],
            )

    def test_five_nonregressing_applied_measurements_are_expansion_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _admission(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
                max_decisions=6,
            )
            for idx in range(5):
                receipt = _optional_effect(
                    root,
                    admission,
                    f"optional-good-{idx}",
                    EchoSelector(),
                )
                result = record_measured_active_observation(
                    root,
                    _measurement(
                        receipt,
                        f"measure-good-{idx}",
                        cost_delta=-10,
                        wall_delta=-5,
                    ),
                )
                self.assertEqual(result["state"], "PUBLISHED")
            policy = decision_class_policy(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
            )
            self.assertEqual(
                policy["effective_state"],
                "MEASURED_EXPANSION_ELIGIBLE",
            )
            self.assertEqual(policy["applied_measurement_count"], 5)
            self.assertEqual(policy["quality_regression_count"], 0)
            self.assertLess(policy["cost_delta_milliunits"], 0)
            self.assertEqual(policy["expansion_authority"], "NONE")
            self.assertEqual(policy["pass_authority"], "NONE")

    def test_focused_check_applied_receipt_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _admission(
                root,
                "FOCUSED_CHECK_SELECTION",
            )
            receipt = _focused_effect(
                root,
                admission,
                "focused-measured",
                EchoSelector(),
            )
            published = record_measured_active_observation(
                root,
                _measurement(receipt, "measure-focused"),
            )
            record = published["record"]
            self.assertEqual(
                record["decision_class"],
                "FOCUSED_CHECK_SELECTION",
            )
            self.assertEqual(record["source_result"], "APPLIED_CANARY")
            self.assertEqual(
                record["selector_attribution"]["provider"],
                "jev",
            )

    def test_duplicate_is_noop_and_source_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _admission(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
            )
            receipt = _optional_effect(
                root,
                admission,
                "optional-tamper",
                EchoSelector(),
            )
            observation = _measurement(
                receipt,
                "measure-tamper",
            )
            first = record_measured_active_observation(
                root,
                observation,
            )
            self.assertEqual(first["state"], "PUBLISHED")
            duplicate = record_measured_active_observation(
                root,
                observation,
            )
            self.assertEqual(duplicate["state"], "DUPLICATE_NOOP")

            effects = json.loads(
                (root / OPTIONAL_EFFECT_FILENAME).read_text()
            )
            effects["records"][0]["receipt"][
                "selected_candidate_ids"
            ] = ["AGENTS.md"]
            (root / OPTIONAL_EFFECT_FILENAME).write_text(
                json.dumps(effects),
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                measured_active_dashboard(root)

    def test_fallback_measurement_does_not_count_as_expansion_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = _admission(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
            )
            receipt = _optional_effect(
                root,
                admission,
                "optional-fallback",
                EchoSelector(invalid=True),
            )
            self.assertEqual(receipt["result"], "FALLBACK")
            record_measured_active_observation(
                root,
                _measurement(
                    receipt,
                    "measure-fallback",
                ),
            )
            policy = decision_class_policy(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
            )
            self.assertEqual(policy["applied_measurement_count"], 0)
            self.assertEqual(
                policy["effective_state"],
                "LIMITED_ACTIVE_CONTINUE",
            )


    def test_public_schema_fixture_and_runtime_validation(self):
        schema = json.loads(
            (
                CONTRACTS
                / "decision-plane-measured-active-record.schema.json"
            ).read_text()
        )
        fixture = json.loads(
            (
                FIXTURES
                / "decision-plane-measured-active-record.example.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        expected_digest = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in fixture.items()
                    if key != "measurement_digest"
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            fixture["measurement_digest"],
            expected_digest,
        )
        self.assertEqual(
            validate_measured_active_record(fixture),
            fixture,
        )

    def test_cli_web_mcp_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            admission = _admission(
                root,
                "OPTIONAL_CONTEXT_SELECTION",
            )
            receipt = _optional_effect(
                root,
                admission,
                "optional-surface",
                EchoSelector(),
            )
            observation = _measurement(
                receipt,
                "measure-surface",
            )
            observation_path = base / "measurement.json"
            observation_path.write_text(
                json.dumps(observation),
                encoding="utf-8",
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root",
                        str(root),
                        "decision-plane",
                        "measured-active-record",
                        "--observation",
                        str(observation_path),
                    ]),
                    0,
                )
            published = json.loads(out.getvalue())
            self.assertEqual(published["state"], "PUBLISHED")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root",
                        str(root),
                        "decision-plane",
                        "measured-active-show",
                    ]),
                    0,
                )
            shown = json.loads(out.getvalue())
            self.assertEqual(shown["measurement_count"], 1)
            self.assertEqual(
                shown["authority"],
                "MEASURED_ACTIVE_EVIDENCE_ONLY",
            )
            self.assertEqual(shown["expansion_authority"], "NONE")

            svc = AtlasService(root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                decision_measured_active_factory=(
                    svc.decision_measured_active_dashboard
                ),
            )
            names = {
                item["name"]
                for item in tools.list_tools(default_read_scopes())
            }
            self.assertIn(
                "get_decision_plane_measured_active",
                names,
            )
            result = tools.call(
                "get_decision_plane_measured_active",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(
                result.data["measurement_count"],
                1,
            )

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
            self.assertIn("Measured active / rollback", body)
            self.assertIn("MEASURED_ACTIVE_EVIDENCE_ONLY", body)
            self.assertIn("Expansion authority", body)

            backup_data_root(root, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())

if __name__ == "__main__":
    unittest.main()
