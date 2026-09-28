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
from atlas.provider_broker import plan_provider_routes
from atlas.provider_transition import (
    load_provider_transition_candidates,
    plan_provider_transition as _plan_provider_transition,
    validate_provider_transition_plan as _validate_provider_transition_plan,
)
from tests.test_provider_broker import FRESH_EVALUATED_AT, FRESH_MAX_EVIDENCE_AGE_SECONDS
from atlas.provenance import ValidationError
from tests.test_provider_broker import CONTRACTS, _candidate, _json

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = CONTRACTS / "provider-transition-plan.schema.json"
BROKER_SCHEMA = CONTRACTS / "provider-broker-plan.schema.json"


def validate_provider_transition_plan(
    payload: object,
    *,
    consumed_at: str,
    expected_max_evidence_age_seconds: int = FRESH_MAX_EVIDENCE_AGE_SECONDS,
):
    return _validate_provider_transition_plan(
        payload,
        consumed_at=consumed_at,
        expected_max_evidence_age_seconds=expected_max_evidence_age_seconds,
    )


def _plan_transition(*args: object, **kwargs: object):
    kwargs.setdefault("evaluated_at", FRESH_EVALUATED_AT)
    kwargs.setdefault(
        "max_evidence_age_seconds", FRESH_MAX_EVIDENCE_AGE_SECONDS
    )
    return _plan_provider_transition(*args, **kwargs)


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
        result = _plan_transition(
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
        self.assertEqual(result["prior_failed_route_ids"], [])
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
        result = _plan_transition(
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
            result["prior_failed_route_ids"],
            ["codex-primary"],
        )
        self.assertEqual(
            result["failed_route_ids"],
            ["codex-primary", "codex-secondary"],
        )
        self.assertEqual(result["attempt"], 2)

    def test_attempt_ceiling_requires_human_even_with_fallback(self) -> None:
        result = _plan_transition(
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

        with self.assertRaisesRegex(
            ValidationError,
            "prior failures already reached max_attempts",
        ):
            _plan_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-secondary",
                failure_reason="RATE_LIMITED",
                prior_failed_route_ids=["codex-primary"],
                max_attempts=1,
            )

    def test_no_remaining_route_requires_human(self) -> None:
        candidates = [_candidates()[0]]
        result = _plan_transition(
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
        result = _plan_transition(
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

    def test_stale_or_future_fallback_is_not_recommended(self) -> None:
        primary = _candidates()[0]
        stale = _candidate(
            "openai",
            "openai-stale",
            capability_rank=1,
            stewardship_rank=0,
            window_start="2026-09-27T00:00:00Z",
            window_end="2026-09-27T00:00:00Z",
        )
        future = _candidate(
            "generic",
            "generic-future",
            capability_rank=1,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:01Z",
            window_end="2026-09-28T00:00:01Z",
        )
        fresh = _candidate(
            "codex",
            "codex-fresh",
            capability_rank=5,
            stewardship_rank=0,
        )
        result = _plan_transition(
            [future, stale, fresh, primary],
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
            evaluated_at=FRESH_EVALUATED_AT,
            max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
        )

        self.assertEqual(result["decision"], "TRANSITION_RECOMMENDED")
        self.assertEqual(result["to_route_id"], "codex-fresh")
        reasons = {
            item["route_id"]: item["reasons"]
            for item in result["remaining_plan"]["ineligible_routes"]
        }
        self.assertEqual(reasons["openai-stale"], ["REMAINING_CAPACITY_STALE"])
        self.assertEqual(reasons["generic-future"], ["REMAINING_CAPACITY_FUTURE"])

    def test_serialized_transition_replay_fails_closed_after_expiry(self) -> None:
        result = _plan_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
        )
        self.assertEqual(result["decision"], "TRANSITION_RECOMMENDED")
        self.assertEqual(
            result["remaining_plan"]["evidence_fresh_until"],
            FRESH_EVALUATED_AT,
        )
        self.assertEqual(
            validate_provider_transition_plan(
                result,
                consumed_at=FRESH_EVALUATED_AT,
            ),
            result,
        )
        with self.assertRaisesRegex(ValidationError, "evidence is stale"):
            validate_provider_transition_plan(
                result,
                consumed_at="2026-09-28T00:00:01Z",
            )

    def test_serialized_transition_cannot_widen_trusted_max_age(self) -> None:
        result = _plan_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
        )
        widened = deepcopy(result)
        remaining = widened["remaining_plan"]
        remaining["max_evidence_age_seconds"] = 3600
        remaining["evidence_fresh_until"] = "2026-09-28T01:00:00Z"
        for route in remaining["eligible_routes"]:
            route["fresh_until"] = "2026-09-28T01:00:00Z"

        with self.assertRaisesRegex(
            ValidationError, "max evidence age does not match trusted policy"
        ):
            validate_provider_transition_plan(
                widened,
                consumed_at="2026-09-28T00:01:00Z",
                expected_max_evidence_age_seconds=0,
            )

    def test_current_route_must_be_selected_and_not_already_failed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "selected eligible route"):
            _plan_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-secondary",
                failure_reason="RATE_LIMITED",
            )

        with self.assertRaisesRegex(ValidationError, "already failed"):
            _plan_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-primary",
                failure_reason="RATE_LIMITED",
                prior_failed_route_ids=["codex-primary"],
            )

    def test_prior_failed_order_and_candidate_order_do_not_change_output(self) -> None:
        candidates = _candidates()
        forward = _plan_transition(
            candidates,
            required_capability="CODE_REVIEW",
            current_route_id="openai-fallback",
            failure_reason="OPERATOR_REQUEST",
            prior_failed_route_ids=["codex-secondary", "codex-primary"],
            max_attempts=4,
        )
        reverse = _plan_transition(
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
                    _plan_transition(_candidates(), **kwargs)

        with self.assertRaises(ValidationError):
            _plan_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="codex-primary",
                failure_reason="RATE_LIMITED",
                prior_failed_route_ids=["codex-secondary", "codex-secondary"],
            )

        with self.assertRaises(ValidationError):
            _plan_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="missing-route",
                failure_reason="RATE_LIMITED",
            )

        with self.assertRaisesRegex(
            ValidationError,
            "prior failures already reached max_attempts",
        ):
            _plan_transition(
                _candidates(),
                required_capability="CODE_REVIEW",
                current_route_id="openai-fallback",
                failure_reason="RATE_LIMITED",
                prior_failed_route_ids=["codex-primary", "codex-secondary"],
                max_attempts=2,
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

        recommended = _plan_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="QUOTA_EXHAUSTED",
        )
        human = _plan_transition(
            [_candidates()[0]],
            required_capability="CODE_REVIEW",
            current_route_id="codex-primary",
            failure_reason="RUNTIME_UNAVAILABLE",
        )
        validator.validate(recommended)
        validator.validate(human)
        self.assertEqual(
            validate_provider_transition_plan(recommended, consumed_at=FRESH_EVALUATED_AT),
            recommended,
        )
        self.assertEqual(validate_provider_transition_plan(human, consumed_at=FRESH_EVALUATED_AT), human)

        contradictory = deepcopy(recommended)
        contradictory["decision"] = "HUMAN_REQUIRED"
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(contradictory)
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(contradictory, consumed_at=FRESH_EVALUATED_AT)

        wrong_to = deepcopy(recommended)
        wrong_to["to_route_id"] = "openai-fallback"
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(wrong_to, consumed_at=FRESH_EVALUATED_AT)

        wrong_strategy = deepcopy(recommended)
        wrong_strategy["remaining_plan"]["strategy"] = "STEWARDSHIP"
        with self.assertRaisesRegex(ValidationError, "strategy is inconsistent"):
            validate_provider_transition_plan(wrong_strategy, consumed_at=FRESH_EVALUATED_AT)

        wrong_capability = deepcopy(recommended)
        wrong_capability["remaining_plan"]["required_capability"] = "PLAN"
        with self.assertRaisesRegex(ValidationError, "capability is inconsistent"):
            validate_provider_transition_plan(wrong_capability, consumed_at=FRESH_EVALUATED_AT)

        secret_failed = deepcopy(recommended)
        secret_failed["failed_route_ids"] = ["ghp_secretlike"]
        secret_failed["from_route_id"] = "ghp_secretlike"
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(secret_failed)
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(secret_failed, consumed_at=FRESH_EVALUATED_AT)

        wrong_history = deepcopy(recommended)
        wrong_history["prior_failed_route_ids"] = ["codex-secondary"]
        with self.assertRaises(ValidationError):
            validate_provider_transition_plan(wrong_history, consumed_at=FRESH_EVALUATED_AT)

        stale_primary = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=5,
            remaining=None,
        )
        stale_remaining = plan_provider_routes(
            [stale_primary, _candidates()[1], _candidates()[2]],
            required_capability="CODE_REVIEW",
            evaluated_at=FRESH_EVALUATED_AT,
            max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
        )
        self.assertEqual(
            stale_remaining["selected_route_id"],
            "codex-secondary",
        )
        leaked_failed = deepcopy(recommended)
        leaked_failed["remaining_plan"] = stale_remaining
        with self.assertRaisesRegex(ValidationError, "contains a failed route"):
            validate_provider_transition_plan(leaked_failed, consumed_at=FRESH_EVALUATED_AT)

        over_limit = _plan_transition(
            _candidates(),
            required_capability="CODE_REVIEW",
            current_route_id="codex-secondary",
            failure_reason="RATE_LIMITED",
            prior_failed_route_ids=["codex-primary"],
            max_attempts=2,
        )
        over_limit["max_attempts"] = 1
        with self.assertRaisesRegex(ValidationError, "exceeds max_attempts"):
            validate_provider_transition_plan(over_limit, consumed_at=FRESH_EVALUATED_AT)

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
                    "--evaluated-at",
                    "2026-09-28T00:00:00Z",
                    "--max-evidence-age-seconds",
                    "0",
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
            "datetime.now",
            "time.time",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
