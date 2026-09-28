"""Context-canary governor input boundary regressions."""

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
from atlas.context_optimization import (
    MAX_INPUT_BYTES,
    load_context_canary_report,
    normalize_context_canary_report,
)
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/context-optimization-input.schema.json"
FIXTURE = ROOT / "docs/contracts/fixtures/context-optimization-input.example.json"
HEAD = "a" * 40


def _arm(
    arm_id: str,
    *,
    kept: int,
    cost: str,
    retries: int = 0,
    pr_rework: int = 0,
    ci_rework: int = 0,
    review_rework: int = 0,
) -> dict:
    return {
        "arm_id": arm_id,
        "system_head": HEAD,
        "run_count": 1,
        "verified_solved_count": 1,
        "original_context_bytes": 1000,
        "kept_context_bytes": kept,
        "reduction_ratio": round(1.0 - (kept / 1000), 6),
        "provider_cost_total": cost,
        "cost_per_verified_solved_task": cost,
        "rework_total": pr_rework + ci_rework + review_rework,
        "input_tokens_total": 100,
        "output_tokens_total": 20,
        "cache_read_tokens_total": 300,
        "cache_write_tokens_total": 10,
        "tool_turns_total": 8,
        "retries_total": retries,
        "rereads_total": 1,
        "compactions_total": 0,
        "pr_rework_total": pr_rework,
        "ci_rework_total": ci_rework,
        "review_rework_total": review_rework,
        "human_interventions_total": 0,
    }


def _report() -> dict:
    return {
        "schema_version": 1,
        "kind": "context-canary-eligibility-report",
        "decision": "ELIGIBLE",
        "system_head": HEAD,
        "profile": {
            "provider": "cursor",
            "model": "composer-2.5",
            "reasoning": "standard",
            "toolset": "default",
        },
        "repo": "datarelay-labs/datarelay-atlas",
        "task_kind": "DEVELOPMENT",
        "record_count": 2,
        "arm_count": 2,
        "arms": [
            _arm("compiler", kept=500, cost="0.75"),
            _arm("baseline", kept=1000, cost="1.25"),
        ],
    }


def _write_json(payload: object) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


class ContextOptimizationInputTests(unittest.TestCase):
    def test_normalizes_comparable_evidence_without_granting_control(self) -> None:
        payload = normalize_context_canary_report(_report())

        self.assertEqual(payload["kind"], "context_optimization_input")
        self.assertEqual(payload["control_mode"], "OBSERVE_ONLY")
        self.assertEqual(
            payload["source_evidence"]["comparability"],
            "ELIGIBLE",
        )
        self.assertEqual(
            payload["gates"],
            {
                "quality_noninferiority": "UNKNOWN",
                "data_egress_eligibility": "UNKNOWN",
                "runtime_capability": "UNKNOWN",
                "active_control": "NOT_ELIGIBLE",
            },
        )
        self.assertEqual(
            [item["arm_id"] for item in payload["arms"]],
            ["baseline", "compiler"],
        )
        encoded = json.dumps(payload)
        for forbidden in (
            "winner",
            "ranking",
            "score",
            "recommendation",
            "COMPRESS",
            "CLEAR",
            "YIELD",
            "ROUTE",
            "prompt",
            "transcript",
            "tool_output",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_output_fixture_matches_schema_and_runtime_shape(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        generated = normalize_context_canary_report(_report())
        Draft202012Validator(schema).validate(generated)
        self.assertEqual(generated, fixture)

    def test_source_identity_profile_and_unknown_fields_fail_closed(self) -> None:
        cases: list[dict] = []

        wrong_kind = deepcopy(_report())
        wrong_kind["kind"] = "context-optimization-score-report"
        cases.append(wrong_kind)

        wrong_decision = deepcopy(_report())
        wrong_decision["decision"] = "BLOCK"
        cases.append(wrong_decision)

        unknown = deepcopy(_report())
        unknown["prompt"] = "secret"
        cases.append(unknown)

        bad_profile = deepcopy(_report())
        bad_profile["profile"]["model"] = "/home/private/model"
        cases.append(bad_profile)

        credential_profile = deepcopy(_report())
        credential_profile["profile"]["model"] = "ghp_secretlike"
        cases.append(credential_profile)

        bad_task = deepcopy(_report())
        bad_task["task_kind"] = "UNKNOWN_TASK"
        cases.append(bad_task)

        for payload in cases:
            with self.subTest(payload=payload.get("kind")):
                with self.assertRaises(ValidationError):
                    normalize_context_canary_report(payload)

    def test_arm_identity_counts_and_head_fail_closed(self) -> None:
        duplicate = deepcopy(_report())
        duplicate["arms"][1]["arm_id"] = "compiler"

        wrong_head = deepcopy(_report())
        wrong_head["arms"][0]["system_head"] = "b" * 40

        wrong_arm_count = deepcopy(_report())
        wrong_arm_count["arm_count"] = 3

        wrong_record_count = deepcopy(_report())
        wrong_record_count["record_count"] = 99

        incomplete = deepcopy(_report())
        incomplete["arms"][0]["verified_solved_count"] = 0

        for payload in (
            duplicate,
            wrong_head,
            wrong_arm_count,
            wrong_record_count,
            incomplete,
        ):
            with self.assertRaises(ValidationError):
                normalize_context_canary_report(payload)

    def test_context_economics_and_rework_consistency_fail_closed(self) -> None:
        kept_over_original = deepcopy(_report())
        kept_over_original["arms"][0]["kept_context_bytes"] = 1001

        wrong_reduction = deepcopy(_report())
        wrong_reduction["arms"][0]["reduction_ratio"] = 0.7

        wrong_cost = deepcopy(_report())
        wrong_cost["arms"][0]["cost_per_verified_solved_task"] = "0.74"

        invented_cost = deepcopy(_report())
        invented_cost["arms"][0]["provider_cost_total"] = "UNKNOWN"

        oversized_cost = deepcopy(_report())
        oversized_cost["arms"][0]["provider_cost_total"] = "9" * 129

        wrong_rework = deepcopy(_report())
        wrong_rework["arms"][0]["rework_total"] = 1

        negative_count = deepcopy(_report())
        negative_count["arms"][0]["input_tokens_total"] = -1

        for payload in (
            kept_over_original,
            wrong_reduction,
            wrong_cost,
            invented_cost,
            oversized_cost,
            wrong_rework,
            negative_count,
        ):
            with self.assertRaises(ValidationError):
                normalize_context_canary_report(payload)

    def test_loader_rejects_duplicate_keys_invalid_json_and_oversize(self) -> None:
        duplicate = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        duplicate.write(
            '{"schema_version":1,"schema_version":1,'
            '"kind":"context-canary-eligibility-report"}'
        )
        duplicate.close()
        with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
            load_context_canary_report(Path(duplicate.name))

        invalid = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        )
        invalid.write(b"\xff")
        invalid.close()
        with self.assertRaises(ValidationError):
            load_context_canary_report(Path(invalid.name))

        oversized = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        )
        oversized.write(b"{" + (b" " * MAX_INPUT_BYTES) + b"}")
        oversized.close()
        with self.assertRaisesRegex(ValidationError, "bounded input size"):
            load_context_canary_report(Path(oversized.name))

    def test_cli_is_read_only_normalizer(self) -> None:
        source = _write_json(_report())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                [
                    "usage",
                    "context-canary-input",
                    "--input",
                    str(source),
                ]
            )
        self.assertEqual(rc, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["kind"], "context_optimization_input")
        self.assertEqual(payload["control_mode"], "OBSERVE_ONLY")
        self.assertEqual(
            payload["gates"]["active_control"],
            "NOT_ELIGIBLE",
        )

    def test_output_schema_rejects_active_control_or_extra_rank(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        payload = normalize_context_canary_report(_report())

        active = deepcopy(payload)
        active["gates"]["active_control"] = "ELIGIBLE"
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(active)

        ranked = deepcopy(payload)
        ranked["winner"] = "compiler"
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(ranked)


if __name__ == "__main__":
    unittest.main()
