"""Provider Capacity Broker failover-transition regressions."""

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
from referencing import Registry, Resource

from atlas.cli import main
from atlas.provider_transition import (
    load_provider_transition_candidates,
    plan_provider_transition,
    validate_provider_transition_plan,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import CONTRACTS, _candidate, _json

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = CONTRACTS / "provider-transition-plan.schema.json"
BROKER_SCHEMA = CONTRACTS / "provider-broker-plan.schema.json"


def _candidates() -> list[dict]:
    return [
        _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=5,
        ),
        _candidate(
            "codex",
            "codex-secondary",
            capability_rank=2,
            stewardship_rank=2,
        ),
        _candidate(
            "openai",
            "openai-fallback",
            capability_rank=5,
            stewardship_rank=0,
        ),
    ]


def _write_json(payload: object) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


class ProviderTransitionTests(unittest.TestCase):
    def test_primary_failure_recommends_fresh_eligible_fallback(self) -> None:
        result = plan_provider_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
            max_attempts=3,
        )

        self.assertEqual(result["decision"], "TRANSITION_RECOMMENDED")
        self.assertEqual(result["decision_reason"], "ELIGIBLE_FALLBACK")
        self.assertEqual(result["authority"], "ADVISORY_ONLY")
        self.assertEqual(result["from_route_id"], "codex-primary")
        self.assertEqual(result["to_route_id"], "codex-secondary")
        self.assertEqual(result["failed_route_ids"], ["codex-primary"])
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(
            result["remaining_plan"]["selected_route_id"],
            "codex-secondary",
        )
        self.assertEqual(
            result["remaining_plan"]["fallback_route_ids"],
            ["openai-fallback"],
        )

    def test_second_failure_uses_prior_failure_and_replans(self) -> None:
        result = plan_provider_transition(
            list(reversed(_candidates())),
            required_capability="CODE_REVIEW",
            current_route_id="codex-secondary",
            failure_reason="HEALTH_UNAVAILABLE",
            prior_failed_route_ids=["codex-primary"],
            max_attempts=3,
        )

        self.assertEqual(result["decision"], "TRANSITION_RECOMMENDED")
        self.assertEqual(result["to_route_id"], "openai-fallback")
        self.assertEqual(
            result["failed_route_ids"],
            ["codex-primary", "codex-secondary"],
        )
        self.assertEqual(result["attempt"], 2)

    def test_attempt_ceiling_requires_human_even_with_fallback(self) -> None:
        result = plan_provider_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-secondary",
            failure_reason="RATE_LIMITED",
            prior_failed_route_ids=["codex-primary"],
            max_attempts=2,
        )

        self.assertEqual(result["decision"], "HUMAN_REQUIRED")
        self.assertEqual(result["decision_reason"], "ATTEMPT_LIMIT_REACHED")
        self.assertIsNone(result["to_route_id"])
        self.assertEqual(
            result["remaining_plan"]["selected_route_id"],
            "openai-fallback",
        )

        over_limit = plan_provider_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-secondary",
            failure_reason="RATE_LIMITED",
            prior_failed_route_ids=["codex-primary"],
            max_attempts=1,
        )
        self.assertEqual(over_limit["decision"], "HUMAN_REQUIRED")
        self.assertEqual(
            over_limit["decision_reason"],
            "ATTEMPT_LIMIT_REACHED",
        )
        self.assertEqual(over_limit["attempt"], 2)
        self.assertEqual(over_limit["max_attempts"], 1)

    def test_no_remaining_route_requires_human(self) -> None:
        candidates = [_candidates()[0]]
        result = plan_provider_transition(
            candidates,
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="RUNTIME_UNAVAILABLE",
            max_attempts=3,
        )

        self.assertEqual(result["decision"], "HUMAN_REQUIRED")
        self.assertEqual(result["decision_reason"], "NO_REMAINING_ROUTES")
        self.assertIsNone(result["remaining_plan"])
        self.assertIsNone(result["to_route_id"])

    def test_remaining_but_ineligible_fallback_requires_human(self) -> None:
        primary = _candidates()[0]
        blocked = _candidate(
            "openai",
            "openai-fallback",
            capability_rank=5,
            stewardship_rank=0,
            remaining=None,
        )
        result = plan_provider_transition(
            [blocked, primary],
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
            max_attempts=3,
        )

        self.assertEqual(result["decision"], "HUMAN_REQUIRED")
        self.assertEqual(result["decision_reason"], "NO_ELIGIBLE_FALLBACK")
        self.assertIsNone(result["to_route_id"])
        self.assertIsNone(result["remaining_plan"]["selected_route_id"])
        self.assertEqual(
            result["remaining_plan"]["ineligible_routes"][0]["reasons"],
            ["REMAINING_CAPACITY_UNKNOWN"],
        )

    def test_current_route_must_be_selected_and_not_already_failed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "selected eligible route"):
            plan_provider_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-secondary",
                failure_reason="RATE_LIMITED",
            )

        with self.assertRaisesRegex(ValidationError, "already failed"):
            plan_provider_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-primary",
                failure_reason="RATE_LIMITED",
                prior_failed_route_ids=["codex-primary"],
            )

    def test_prior_failed_order_and_candidate_order_do_not_change_output(self) -> None:
        candidates = _candidates()
        forward = plan_provider_transition(
            candidates,
            required_capability="CODE_REVIEW",
            current_route_id="openai-fallback",
            failure_reason="OPERATOR_REQUEST",
            prior_failed_route_ids=["codex-secondary", "codex-primary"],
            max_attempts=4,
        )
        reverse = plan_provider_transition(
            list(reversed(candidates)),
            required_capability="CODE_REVIEW",
            current_route_id="openai-fallback",
            failure_reason="OPERATOR_REQUEST",
            prior_failed_route_ids=["codex-primary", "codex-secondary"],
            max_attempts=4,
        )
        self.assertEqual(forward, reverse)

    def test_invalid_transition_inputs_fail_closed(self) -> None:
        cases = [
            {"failure_reason": "SILENT_FAILOVER"},
            {"required_capability": "NOT_A_CAPABILITY"},
            {"max_attempts": True},
            {"max_attempts": 0},
        ]
        for overrides in cases:
            kwargs = {
                "required_capability": "CODE_REVIEW",
                "current_route_id": "codex-primary",
                "failure_reason": "RATE_LIMITED",
                "max_attempts": 3,
            }
            kwargs.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    plan_provider_transition(_candidates(), **kwargs)

        with self.assertRaises(ValidationError):
            plan_provider_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-primary",
                failure_reason="RATE_LIMITED",
                prior_failed_route_ids=["codex-secondary", "codex-secondary"],
            )

        with self.assertRaises(ValidationError):
            plan_provider_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="missing-route",
                failure_reason="RATE_LIMITED",
            )

    def test_schema_and_runtime_contract_match(self) -> None:
        schema = _json(SCHEMA)
        broker_schema = _json(BROKER_SCHEMA)
        Draft202012Validator.check_schema(schema)
        registry = Registry().with_resource(
            broker_schema["$id"],
            Resource.from_contents(broker_schema),
        )
        validator = Draft202012Validator(schema, registry=registry)

        recommended = plan_provider_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
        )
        human = plan_provider_transition(
            [_candidates()[0]],
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="RUNTIME_UNAVAILABLE",
        )
        validator.validate(recommended)
        validator.validate(human)
        self.assertEqual(
            validate_provider_transition_plan(recommended),
            recommended,
        )
        self.assertEqual(validate_provider_transition_plan(human), human)

        contradictory = deepcopy(recommended)
        contradictory["decision"] = "HUMAN_REQUIRED"
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(contradictory)
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(contradictory)

        wrong_to = deepcopy(recommended)
        wrong_to["to_route_id"] = "openai-fallback"
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(wrong_to)

        secret_failed = deepcopy(recommended)
        secret_failed["failed_route_ids"] = ["ghp_secretlike"]
        secret_failed["from_route_id"] = "ghp_secretlike"
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(secret_failed)
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(secret_failed)

    def test_cli_is_read_only_and_content_free(self) -> None:
        path = _write_json(_candidates())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                [
                    "usage",
                    "provider-transition-plan",
                    "--candidates",
                    str(path),
                    "--required-capability",
                    "CODE_REVIEW",
                    "--current-route",
                    "codex-primary",
                    "--failure-reason",
                    "QUOTA_EXHAUSTED",
                    "--max-attempts",
                    "3",
                ]
            )

        self.assertEqual(rc, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["decision"], "TRANSITION_RECOMMENDED")
        self.assertEqual(payload["to_route_id"], "codex-secondary")
        rendered = stdout.getvalue()
        for forbidden in (
            "credential",
            "prompt",
            "transcript",
            "source_text",
            "tool_output",
            "EXECUTE",
            "AUTO_FAILOVER",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_loader_rejects_duplicate_keys_and_oversize(self) -> None:
        duplicate = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        )
        duplicate.write(
            '[{"schema_version":1,"schema_version":1,'
            '"kind":"provider_route_candidate"}]'
        )
        duplicate.close()
        with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
            load_provider_transition_candidates(Path(duplicate.name))

        oversized = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".json",
            delete=False,
        )
        oversized.write(b"[" + (b" " * (1024 * 1024)) + b"]")
        oversized.close()
        with self.assertRaisesRegex(ValidationError, "bounded input size"):
            load_provider_transition_candidates(Path(oversized.name))

    def test_module_has_no_execution_or_provider_transport_dependency(self) -> None:
        source = (ROOT / "atlas/provider_transition.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "httpx",
            "agent persist",
            "cursor_agent",
            "CodexAuditProvider",
            "BoundedResponsesAuditProvider",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
