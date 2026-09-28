"""Context shadow quality-binding regressions for Atlas #76."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.cli import main
from atlas.context_optimization import normalize_context_canary_report
from atlas.context_shadow import (
    bind_shadow_quality,
    load_shadow_quality_binding,
    normalize_shadow_report,
)
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/context-optimization-input.schema.json"
HEAD = "a" * 40
RUN_SET_DIGEST = "b" * 64
PROFILE = {
    "provider": "cursor",
    "model": "composer-2.5",
    "reasoning": "standard",
    "toolset": "default",
}


def _arm(arm_id: str, *, kept: int) -> dict:
    return {
        "arm_id": arm_id,
        "system_head": HEAD,
        "run_count": 2,
        "verified_solved_count": 2,
        "original_context_bytes": 2000,
        "kept_context_bytes": kept,
        "reduction_ratio": 0.0 if kept == 2000 else 0.5,
        "provider_cost_total": "2",
        "cost_per_verified_solved_task": "1",
        "rework_total": 0,
        "input_tokens_total": 200,
        "output_tokens_total": 40,
        "cache_read_tokens_total": 600,
        "cache_write_tokens_total": 20,
        "tool_turns_total": 12,
        "retries_total": 0,
        "rereads_total": 2,
        "compactions_total": 0,
        "pr_rework_total": 0,
        "ci_rework_total": 0,
        "review_rework_total": 0,
        "human_interventions_total": 0,
    }


def _context_input() -> dict:
    return normalize_context_canary_report(
        {
            "schema_version": 2,
            "kind": "context-canary-eligibility-report",
            "decision": "ELIGIBLE",
            "system_head": HEAD,
            "profile": PROFILE,
            "repo": "datarelay-labs/datarelay-atlas",
            "task_kind": "DEVELOPMENT",
            "run_set_digest": RUN_SET_DIGEST,
            "record_count": 4,
            "arm_count": 2,
            "arms": [
                _arm("candidate", kept=1000),
                _arm("baseline", kept=2000),
            ],
        }
    )


def _shadow_report() -> dict:
    return {
        "schema_version": 2,
        "kind": "context-shadow-equivalence-report",
        "decision": "EQUIVALENT",
        "control_arm_id": "baseline",
        "system_head": HEAD,
        "repo": "datarelay-labs/datarelay-atlas",
        "task_kind": "DEVELOPMENT",
        "run_set_digest": RUN_SET_DIGEST,
        "profile": PROFILE,
        "case_count": 2,
        "arm_count": 2,
        "observation_count": 4,
        "arms": [
            {
                "arm_id": "baseline",
                "run_count": 2,
                "material_action_count": 12,
            },
            {
                "arm_id": "candidate",
                "run_count": 2,
                "material_action_count": 12,
            },
        ],
    }


def _write_json(payload: object) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


class ContextShadowQualityTests(unittest.TestCase):
    def test_binding_advances_only_shadow_quality_gate(self) -> None:
        base = _context_input()
        bound = bind_shadow_quality(base, _shadow_report())

        self.assertEqual(
            bound["gates"],
            {
                "quality_noninferiority": "SHADOW_ACTION_EQUIVALENT",
                "data_egress_eligibility": "UNKNOWN",
                "runtime_capability": "UNKNOWN",
                "active_control": "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
            },
        )
        self.assertEqual(bound["control_mode"], "OBSERVE_ONLY")
        self.assertEqual(bound["source_evidence"], base["source_evidence"])
        self.assertEqual(bound["scope"], base["scope"])
        self.assertEqual(bound["arms"], base["arms"])
        self.assertEqual(
            bound["quality_evidence"]["source_kind"],
            "engineering_system_context_shadow_v2",
        )
        self.assertEqual(
            bound["quality_evidence"]["control_arm_id"],
            "baseline",
        )
        self.assertEqual(bound["quality_evidence"]["source_schema_version"], 2)
        self.assertEqual(
            bound["quality_evidence"]["repository"],
            "datarelay-labs/datarelay-atlas",
        )
        self.assertEqual(
            bound["quality_evidence"]["run_set_digest"],
            RUN_SET_DIGEST,
        )
        self.assertEqual(bound["quality_evidence"]["observation_count"], 4)

        encoded = json.dumps(bound, sort_keys=True)
        for forbidden in (
            "winner",
            "ranking",
            "recommendation",
            "probability",
            "confidence",
            "COMPRESS",
            "CLEAR",
            "YIELD",
            "ROUTE",
            "prompt",
            "transcript",
            "tool_output",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn('"actions":', encoded)

    def test_shadow_report_is_normalized_and_bounded(self) -> None:
        normalized = normalize_shadow_report(_shadow_report())
        self.assertEqual(
            [item["arm_id"] for item in normalized["arms"]],
            ["baseline", "candidate"],
        )

        bad_action_count = deepcopy(_shadow_report())
        bad_action_count["arms"][0]["material_action_count"] = 33
        with self.assertRaises(ValidationError):
            normalize_shadow_report(bad_action_count)

        too_few_actions = deepcopy(_shadow_report())
        too_few_actions["arms"][0]["material_action_count"] = 1
        with self.assertRaises(ValidationError):
            normalize_shadow_report(too_few_actions)

    def test_shadow_identity_and_cardinality_fail_closed(self) -> None:
        cases = []

        wrong_kind = deepcopy(_shadow_report())
        wrong_kind["kind"] = "context-shadow-score"
        cases.append(wrong_kind)

        wrong_decision = deepcopy(_shadow_report())
        wrong_decision["decision"] = "SIMILAR"
        cases.append(wrong_decision)

        unknown = deepcopy(_shadow_report())
        unknown["score"] = 1
        cases.append(unknown)

        secret_profile = deepcopy(_shadow_report())
        secret_profile["profile"]["model"] = "ghp_secretlike"
        cases.append(secret_profile)

        duplicate_arm = deepcopy(_shadow_report())
        duplicate_arm["arms"][1]["arm_id"] = "baseline"
        cases.append(duplicate_arm)

        unknown_control = deepcopy(_shadow_report())
        unknown_control["control_arm_id"] = "missing"
        cases.append(unknown_control)

        wrong_runs = deepcopy(_shadow_report())
        wrong_runs["arms"][1]["run_count"] = 1
        cases.append(wrong_runs)

        wrong_observations = deepcopy(_shadow_report())
        wrong_observations["observation_count"] = 3
        cases.append(wrong_observations)

        mismatched_actions = deepcopy(_shadow_report())
        mismatched_actions["arms"][1]["material_action_count"] = 11
        cases.append(mismatched_actions)

        legacy_shadow = deepcopy(_shadow_report())
        legacy_shadow["schema_version"] = 1
        cases.append(legacy_shadow)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    normalize_shadow_report(payload)

    def test_binding_requires_exact_canary_identity(self) -> None:
        mutations = []

        head = deepcopy(_shadow_report())
        head["system_head"] = "b" * 40
        mutations.append(head)

        repo = deepcopy(_shadow_report())
        repo["repo"] = "datarelay-labs/other"
        mutations.append(repo)

        task_kind = deepcopy(_shadow_report())
        task_kind["task_kind"] = "TEST"
        mutations.append(task_kind)

        run_set = deepcopy(_shadow_report())
        run_set["run_set_digest"] = "c" * 64
        mutations.append(run_set)

        profile = deepcopy(_shadow_report())
        profile["profile"]["model"] = "other-model"
        mutations.append(profile)

        arm = deepcopy(_shadow_report())
        arm["arms"][1]["arm_id"] = "other"
        mutations.append(arm)

        run_count = deepcopy(_shadow_report())
        run_count["case_count"] = 1
        run_count["observation_count"] = 2
        for item in run_count["arms"]:
            item["run_count"] = 1
            item["material_action_count"] = 6
        mutations.append(run_count)

        for shadow in mutations:
            with self.subTest(shadow=shadow):
                with self.assertRaises(ValidationError):
                    bind_shadow_quality(_context_input(), shadow)

    def test_legacy_base_context_cannot_receive_shadow_quality_promotion(self) -> None:
        legacy = normalize_context_canary_report(
            {
                "schema_version": 1,
                "kind": "context-canary-eligibility-report",
                "decision": "ELIGIBLE",
                "system_head": HEAD,
                "profile": PROFILE,
                "repo": "datarelay-labs/datarelay-atlas",
                "task_kind": "DEVELOPMENT",
                "record_count": 4,
                "arm_count": 2,
                "arms": [
                    _arm("candidate", kept=1000),
                    _arm("baseline", kept=2000),
                ],
            }
        )
        self.assertEqual(
            legacy["source_evidence"]["source_schema_version"],
            1,
        )
        self.assertNotIn("run_set_digest", legacy["source_evidence"])
        with self.assertRaisesRegex(ValidationError, "exact run-set binding"):
            bind_shadow_quality(legacy, _shadow_report())

    def test_base_context_must_be_pristine_observation_only_input(self) -> None:
        active = _context_input()
        active["gates"]["active_control"] = "ELIGIBLE"

        already_bound = _context_input()
        already_bound["gates"][
            "quality_noninferiority"
        ] = "SHADOW_ACTION_EQUIVALENT"

        extra = _context_input()
        extra["winner"] = "candidate"

        for base in (active, already_bound, extra):
            with self.assertRaises(ValidationError):
                bind_shadow_quality(base, _shadow_report())

    def test_loader_rejects_duplicate_keys_and_oversize(self) -> None:
        context_path = _write_json(_context_input())

        duplicate = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        duplicate.write(
            '{"schema_version":1,"schema_version":1,'
            '"kind":"context-shadow-equivalence-report"}'
        )
        duplicate.close()
        with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
            load_shadow_quality_binding(context_path, Path(duplicate.name))

        oversized = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        )
        oversized.write(b"{" + (b" " * (1024 * 1024)) + b"}")
        oversized.close()
        with self.assertRaisesRegex(ValidationError, "bounded input size"):
            load_shadow_quality_binding(context_path, Path(oversized.name))

    def test_cli_binding_is_read_only_and_content_free(self) -> None:
        context_path = _write_json(_context_input())
        shadow_path = _write_json(_shadow_report())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                [
                    "usage",
                    "context-shadow-bind",
                    "--context-input",
                    str(context_path),
                    "--shadow-report",
                    str(shadow_path),
                ]
            )

        self.assertEqual(rc, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["gates"]["quality_noninferiority"],
            "SHADOW_ACTION_EQUIVALENT",
        )
        self.assertEqual(
            payload["gates"]["active_control"],
            "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
        )
        self.assertEqual(payload["control_mode"], "OBSERVE_ONLY")

    def test_schema_requires_quality_evidence_exactly_when_bound(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        base = _context_input()
        bound = bind_shadow_quality(base, _shadow_report())

        Draft202012Validator.check_schema(schema)
        validator.validate(base)
        validator.validate(bound)

        missing = deepcopy(bound)
        del missing["quality_evidence"]
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(missing)

        invented = deepcopy(base)
        invented["quality_evidence"] = bound["quality_evidence"]
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(invented)

    def test_module_has_no_provider_network_or_mutation_dependency(self) -> None:
        source = (ROOT / "atlas/context_shadow.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests",
            "httpx",
            "urllib",
            "subprocess",
            "socket",
            "gh issue edit",
            "agent persist",
            "winner",
            "ranking",
            "recommendation",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
