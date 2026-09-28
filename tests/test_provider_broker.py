"""Provider Capacity Broker read-only planning regressions."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from referencing import Registry, Resource

from atlas.provider_broker import (
    plan_provider_routes,
    validate_provider_broker_plan,
    validate_provider_route_candidate,
)
from atlas.provider_capacity import (
    build_provider_capacity_input,
    validate_provider_capacity_input,
)
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _descriptor(provider: str) -> dict:
    return _json(
        FIXTURES / f"provider-capability-{provider}.example.json"
    )


FRESH_EVALUATED_AT = "2026-09-28T00:00:00Z"
FRESH_MAX_EVIDENCE_AGE_SECONDS = 0


def plan_fresh_routes(candidates: object, **kwargs: object):
    kwargs.setdefault("evaluated_at", FRESH_EVALUATED_AT)
    kwargs.setdefault(
        "max_evidence_age_seconds", FRESH_MAX_EVIDENCE_AGE_SECONDS
    )
    return plan_provider_routes(candidates, **kwargs)


def _capacity(
    provider: str,
    remaining: str | None = "100",
    *,
    window_start: str | None = "2026-09-28T00:00:00Z",
    window_end: str | None = "2026-09-28T00:00:00Z",
    event_count: int = 1,
    total_tokens: int = 1,
) -> dict:
    payload = build_provider_capacity_input(
        provider=provider,
        source_kind="synthetic_test",
        window_start=window_start,
        window_end=window_end,
        event_count=event_count,
        total_tokens=total_tokens,
    )
    if remaining is not None:
        payload["signals"]["remaining_capacity"] = {
            "status": "OBSERVED",
            "value": remaining,
            "unit": "credits",
        }
        payload["signals"]["active_inference_wip"] = {
            "status": "OBSERVED",
            "value": 0,
        }
    return validate_provider_capacity_input(payload)


def _candidate(
    provider: str,
    route_id: str,
    *,
    capability_rank: int,
    stewardship_rank: int,
    remaining: str | None = "100",
    gate_overrides: dict[str, str] | None = None,
    window_start: str | None = "2026-09-28T00:00:00Z",
    window_end: str | None = "2026-09-28T00:00:00Z",
    event_count: int = 1,
    total_tokens: int = 1,
) -> dict:
    gates = {
        "policy": "ALLOW",
        "trust": "ALLOW",
        "budget": "ALLOW",
        "usage_mode": "ALLOW",
        "blast_radius": "ALLOW",
        "wip": "ALLOW",
    }
    if gate_overrides:
        gates.update(gate_overrides)
    payload = {
        "schema_version": 1,
        "kind": "provider_route_candidate",
        "route_id": route_id,
        "capability_descriptor": _descriptor(provider),
        "capacity_input": _capacity(
            provider,
            remaining,
            window_start=window_start,
            window_end=window_end,
            event_count=event_count,
            total_tokens=total_tokens,
        ),
        "gates": gates,
        "ranks": {
            "capability_preference": capability_rank,
            "stewardship_preference": stewardship_rank,
        },
    }
    return validate_provider_route_candidate(payload)


class ProviderBrokerTests(unittest.TestCase):
    def test_public_schemas_and_fixtures_match_runtime_contract(self):
        capability_schema = _json(
            CONTRACTS / "provider-capability-descriptor.schema.json"
        )
        capacity_schema = _json(
            CONTRACTS / "provider-capacity-input.schema.json"
        )
        candidate_schema = _json(
            CONTRACTS / "provider-route-candidate.schema.json"
        )
        plan_schema = _json(
            CONTRACTS / "provider-broker-plan.schema.json"
        )
        for schema in (
            capability_schema,
            capacity_schema,
            candidate_schema,
            plan_schema,
        ):
            Draft202012Validator.check_schema(schema)

        registry = Registry()
        for schema in (capability_schema, capacity_schema, candidate_schema):
            registry = registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
        candidate = _json(
            FIXTURES / "provider-route-codex.example.json"
        )
        Draft202012Validator(
            candidate_schema, registry=registry
        ).validate(candidate)
        self.assertEqual(
            validate_provider_route_candidate(candidate),
            candidate,
        )

        plan = _json(FIXTURES / "provider-broker-plan.example.json")
        Draft202012Validator(plan_schema).validate(plan)
        self.assertEqual(validate_provider_broker_plan(plan, consumed_at=FRESH_EVALUATED_AT), plan)

        secret_candidate = deepcopy(candidate)
        secret_candidate["route_id"] = "ghp_secretlike"
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(candidate_schema, registry=registry).validate(
                secret_candidate
            )
        with self.assertRaises(ValidationError):
            validate_provider_route_candidate(secret_candidate)

        secret_plan = deepcopy(plan)
        secret_plan["eligible_routes"][0]["runtime"] = "ghp_secretlike"
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(plan_schema).validate(secret_plan)
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(secret_plan, consumed_at=FRESH_EVALUATED_AT)

    def test_primary_and_fallback_are_strategy_specific(self):
        codex = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=10,
        )
        openai = _candidate(
            "openai",
            "openai-fallback",
            capability_rank=5,
            stewardship_rank=0,
        )
        capability_first = plan_fresh_routes(
            [openai, codex],
            required_capability="CODE_REVIEW",
            strategy="CAPABILITY_FIRST",
        )
        self.assertEqual(
            capability_first["selected_route_id"],
            "codex-primary",
        )
        self.assertEqual(
            capability_first["fallback_route_ids"],
            ["openai-fallback"],
        )
        self.assertEqual(
            [item["rank"] for item in capability_first["eligible_routes"]],
            [[0, 10], [5, 0]],
        )

        stewardship = plan_fresh_routes(
            [codex, openai],
            required_capability="CODE_REVIEW",
            strategy="STEWARDSHIP",
        )
        self.assertEqual(
            stewardship["selected_route_id"],
            "openai-fallback",
        )
        self.assertEqual(
            stewardship["fallback_route_ids"],
            ["codex-primary"],
        )

    def test_input_order_does_not_change_plan(self):
        candidates = [
            _candidate(
                "codex",
                "codex-primary",
                capability_rank=0,
                stewardship_rank=10,
            ),
            _candidate(
                "openai",
                "openai-fallback",
                capability_rank=5,
                stewardship_rank=0,
            ),
        ]
        forward = plan_fresh_routes(
            candidates,
            required_capability="CODE_REVIEW",
        )
        reverse = plan_fresh_routes(
            list(reversed(candidates)),
            required_capability="CODE_REVIEW",
        )
        self.assertEqual(forward, reverse)

    def test_unknown_or_denied_gate_is_ineligible(self):
        candidate = _candidate(
            "codex",
            "codex-blocked",
            capability_rank=0,
            stewardship_rank=0,
            gate_overrides={
                "trust": "UNKNOWN",
                "budget": "DENY",
            },
        )
        plan = plan_fresh_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        self.assertIsNone(plan["selected_route_id"])
        self.assertEqual(plan["fallback_route_ids"], [])
        self.assertEqual(plan["eligible_routes"], [])
        self.assertEqual(
            plan["ineligible_routes"][0]["reasons"],
            ["TRUST_UNKNOWN", "BUDGET_DENY"],
        )

    def test_unknown_or_exhausted_remaining_capacity_is_ineligible(self):
        unknown = _candidate(
            "codex",
            "codex-unknown",
            capability_rank=0,
            stewardship_rank=0,
            remaining=None,
        )
        exhausted = _candidate(
            "openai",
            "openai-exhausted",
            capability_rank=0,
            stewardship_rank=0,
            remaining="0",
        )
        plan = plan_fresh_routes(
            [unknown, exhausted],
            required_capability="CODE_REVIEW",
        )
        reasons = {
            item["route_id"]: item["reasons"]
            for item in plan["ineligible_routes"]
        }
        self.assertEqual(
            reasons["codex-unknown"],
            ["REMAINING_CAPACITY_UNKNOWN"],
        )
        self.assertEqual(
            reasons["openai-exhausted"],
            ["REMAINING_CAPACITY_EXHAUSTED"],
        )
    def test_provider_identity_mismatch_fails_closed(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        candidate["capacity_input"] = _capacity("openai")
        with self.assertRaises(ValidationError):
            validate_provider_route_candidate(candidate)

    def test_missing_required_capability_is_ineligible(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        plan = plan_fresh_routes(
            [candidate],
            required_capability="PLAN",
        )
        self.assertEqual(
            plan["ineligible_routes"][0]["reasons"],
            ["CAPABILITY_NOT_DECLARED"],
        )

    def test_non_integer_schema_versions_fail_closed(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        for value in (True, 1.0, "1", 2):
            broken = deepcopy(candidate)
            broken["schema_version"] = value
            with self.subTest(candidate_version=value):
                with self.assertRaises(ValidationError):
                    validate_provider_route_candidate(broken)

        plan = plan_fresh_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        for value in (True, 1.0, "1", 2):
            broken = deepcopy(plan)
            broken["schema_version"] = value
            with self.subTest(plan_version=value):
                with self.assertRaises(ValidationError):
                    validate_provider_broker_plan(broken, consumed_at=FRESH_EVALUATED_AT)

    def test_duplicate_routes_and_invalid_strategy_fail_closed(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        with self.assertRaises(ValidationError):
            plan_fresh_routes(
                [candidate, deepcopy(candidate)],
                required_capability="CODE_REVIEW",
            )
        with self.assertRaises(ValidationError):
            plan_fresh_routes(
                [candidate],
                required_capability="CODE_REVIEW",
                strategy="COST_FIRST",
            )
        broken = deepcopy(candidate)
        broken["ranks"]["capability_preference"] = True
        with self.assertRaises(ValidationError):
            validate_provider_route_candidate(broken)

    def test_plan_consistency_and_authority_fail_closed(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        plan = plan_fresh_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        wrong_selected = deepcopy(plan)
        wrong_selected["selected_route_id"] = None
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(wrong_selected, consumed_at=FRESH_EVALUATED_AT)

        empty = deepcopy(plan)
        empty["selected_route_id"] = None
        empty["fallback_route_ids"] = []
        empty["eligible_routes"] = []
        empty["ineligible_routes"] = []
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(empty, consumed_at=FRESH_EVALUATED_AT)
        plan_schema = _json(CONTRACTS / "provider-broker-plan.schema.json")
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(plan_schema).validate(empty)

        wrong_authority = deepcopy(plan)
        wrong_authority["authority"] = "EXECUTION"
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(wrong_authority, consumed_at=FRESH_EVALUATED_AT)

        secret_identity = deepcopy(plan)
        secret_identity["eligible_routes"][0]["runtime"] = "ghp_secret"
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(secret_identity, consumed_at=FRESH_EVALUATED_AT)

        wrong_fallback = deepcopy(plan)
        wrong_fallback["fallback_route_ids"] = ["made-up-route"]
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(wrong_fallback, consumed_at=FRESH_EVALUATED_AT)

    def test_evidence_expiry_overflow_fails_closed(self):
        near_max = _candidate(
            "codex",
            "codex-near-max",
            capability_rank=0,
            stewardship_rank=0,
            window_start="9999-12-31T23:59:59Z",
            window_end="9999-12-31T23:59:59Z",
        )
        with self.assertRaisesRegex(
            ValidationError, "evidence expiry is out of range"
        ):
            plan_provider_routes(
                [near_max],
                required_capability="CODE_REVIEW",
                evaluated_at="9999-12-31T23:59:59Z",
                max_evidence_age_seconds=1,
            )

        plan = plan_provider_routes(
            [near_max],
            required_capability="CODE_REVIEW",
            evaluated_at="9999-12-31T23:59:59Z",
            max_evidence_age_seconds=0,
        )
        tampered = deepcopy(plan)
        tampered["max_evidence_age_seconds"] = 1
        with self.assertRaisesRegex(
            ValidationError, "evidence expiry is out of range"
        ):
            validate_provider_broker_plan(
                tampered, consumed_at="9999-12-31T23:59:59Z"
            )

    def test_output_is_content_free_and_has_no_effect_authority(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        plan = plan_fresh_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        rendered = json.dumps(plan)
        self.assertEqual(plan["authority"], "ADVISORY_ONLY")
        for forbidden in (
            "adapter",
            "source_kind",
            "total_tokens",
            "credential",
            "prompt",
            "transcript",
            "finding",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_positive_capacity_freshness_fails_closed(self):
        fresh = _candidate(
            "codex",
            "codex-boundary",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:00Z",
            window_end="2026-09-28T00:00:01Z",
        )
        stale = _candidate(
            "openai",
            "openai-stale",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-27T00:00:00Z",
            window_end="2026-09-27T00:00:00Z",
        )
        future = _candidate(
            "generic",
            "generic-future",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:02Z",
            window_end="2026-09-28T00:00:02Z",
        )
        unbound = _candidate(
            "codex",
            "codex-unbound",
            capability_rank=1,
            stewardship_rank=0,
            window_start=None,
            window_end=None,
            event_count=0,
            total_tokens=0,
        )
        plan = plan_provider_routes(
            [future, unbound, stale, fresh],
            required_capability="CODE_REVIEW",
            evaluated_at="2026-09-28T00:00:01Z",
            max_evidence_age_seconds=1,
        )
        self.assertEqual(plan["selected_route_id"], "codex-boundary")
        self.assertEqual(plan["eligible_routes"][0]["reasons"], ["ELIGIBLE"])
        reasons = {
            item["route_id"]: item["reasons"]
            for item in plan["ineligible_routes"]
        }
        self.assertEqual(reasons["openai-stale"], ["REMAINING_CAPACITY_STALE"])
        self.assertEqual(reasons["generic-future"], ["REMAINING_CAPACITY_FUTURE"])
        self.assertEqual(reasons["codex-unbound"], ["REMAINING_CAPACITY_UNBOUND"])

        exact_age = _candidate(
            "codex",
            "codex-exact-age",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:00Z",
            window_end="2026-09-28T00:00:00Z",
        )
        at_limit = plan_provider_routes(
            [exact_age],
            required_capability="CODE_REVIEW",
            evaluated_at="2026-09-28T00:00:01Z",
            max_evidence_age_seconds=1,
        )
        self.assertEqual(at_limit["selected_route_id"], "codex-exact-age")
        just_over = plan_provider_routes(
            [exact_age],
            required_capability="CODE_REVIEW",
            evaluated_at="2026-09-28T00:00:02Z",
            max_evidence_age_seconds=1,
        )
        self.assertEqual(
            just_over["ineligible_routes"][0]["reasons"],
            ["REMAINING_CAPACITY_STALE"],
        )

        unknown = _candidate(
            "codex",
            "codex-unknown",
            capability_rank=0,
            stewardship_rank=0,
            remaining=None,
            window_start="2026-09-27T00:00:00Z",
            window_end="2026-09-27T00:00:00Z",
        )
        exhausted = _candidate(
            "openai",
            "openai-exhausted",
            capability_rank=0,
            stewardship_rank=0,
            remaining="0",
            window_start="2026-09-28T00:00:02Z",
            window_end="2026-09-28T00:00:02Z",
        )
        unchanged = plan_provider_routes(
            [unknown, exhausted],
            required_capability="CODE_REVIEW",
            evaluated_at="2026-09-28T00:00:01Z",
            max_evidence_age_seconds=0,
        )
        unchanged_reasons = {
            item["route_id"]: item["reasons"]
            for item in unchanged["ineligible_routes"]
        }
        self.assertEqual(
            unchanged_reasons["codex-unknown"],
            ["REMAINING_CAPACITY_UNKNOWN"],
        )
        self.assertEqual(
            unchanged_reasons["openai-exhausted"],
            ["REMAINING_CAPACITY_EXHAUSTED"],
        )

        with self.assertRaisesRegex(ValidationError, "evaluated_at"):
            plan_provider_routes(
                [fresh],
                required_capability="CODE_REVIEW",
                evaluated_at="now",
                max_evidence_age_seconds=0,
            )
        with self.assertRaisesRegex(ValidationError, "max_evidence_age_seconds"):
            plan_provider_routes(
                [fresh],
                required_capability="CODE_REVIEW",
                evaluated_at="2026-09-28T00:00:01Z",
                max_evidence_age_seconds=True,
            )

    def test_serialized_eligible_plan_replay_fails_closed_without_wall_clock(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        plan = plan_fresh_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        self.assertEqual(plan["evaluated_at"], FRESH_EVALUATED_AT)
        self.assertEqual(plan["max_evidence_age_seconds"], 0)
        self.assertEqual(plan["evidence_fresh_until"], FRESH_EVALUATED_AT)
        self.assertEqual(
            validate_provider_broker_plan(
                plan, consumed_at=FRESH_EVALUATED_AT
            ),
            plan,
        )
        with self.assertRaisesRegex(ValidationError, "evidence is stale"):
            validate_provider_broker_plan(
                plan,
                consumed_at="2026-09-28T00:00:01Z",
            )
        with self.assertRaisesRegex(ValidationError, "evidence is stale"):
            validate_provider_broker_plan(
                plan,
                consumed_at="2026-09-27T23:59:59Z",
            )

        earlier = _candidate(
            "codex",
            "codex-earlier",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:00Z",
            window_end="2026-09-28T00:00:00Z",
        )
        later = _candidate(
            "openai",
            "openai-later",
            capability_rank=1,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:01Z",
            window_end="2026-09-28T00:00:01Z",
        )
        bounded = plan_provider_routes(
            [later, earlier],
            required_capability="CODE_REVIEW",
            evaluated_at="2026-09-28T00:00:01Z",
            max_evidence_age_seconds=1,
        )
        self.assertEqual(
            [item["route_id"] for item in bounded["eligible_routes"]],
            ["codex-earlier", "openai-later"],
        )
        self.assertEqual(bounded["evidence_fresh_until"], "2026-09-28T00:00:01Z")
        self.assertEqual(
            validate_provider_broker_plan(
                bounded,
                consumed_at="2026-09-28T00:00:01Z",
            )["selected_route_id"],
            "codex-earlier",
        )
        with self.assertRaisesRegex(ValidationError, "evidence is stale"):
            validate_provider_broker_plan(
                bounded,
                consumed_at="2026-09-28T00:00:02Z",
            )
        extended = deepcopy(bounded)
        extended["evidence_fresh_until"] = "2026-09-28T00:00:03Z"
        with self.assertRaisesRegex(ValidationError, "boundary is inconsistent"):
            validate_provider_broker_plan(
                extended,
                consumed_at="2026-09-28T00:00:01Z",
            )
        missing = deepcopy(plan)
        del missing["evidence_fresh_until"]
        with self.assertRaisesRegex(ValidationError, "schema is invalid"):
            validate_provider_broker_plan(
                missing,
                consumed_at=FRESH_EVALUATED_AT,
            )

        stale = _candidate(
            "openai",
            "openai-stale",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-27T00:00:00Z",
            window_end="2026-09-27T00:00:00Z",
        )
        ineligible = plan_provider_routes(
            [stale],
            required_capability="CODE_REVIEW",
            evaluated_at=FRESH_EVALUATED_AT,
            max_evidence_age_seconds=0,
        )
        self.assertIsNone(ineligible["selected_route_id"])
        self.assertIsNone(ineligible["evidence_fresh_until"])
        self.assertIsNone(
            validate_provider_broker_plan(
                ineligible,
                consumed_at="2026-09-29T00:00:00Z",
            )["selected_route_id"]
        )

    def test_tampered_expiry_extension_fails_against_retained_observations(self):
        earlier = _candidate(
            "codex",
            "codex-earlier",
            capability_rank=0,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:00Z",
            window_end="2026-09-28T00:00:00Z",
        )
        later = _candidate(
            "openai",
            "openai-later",
            capability_rank=1,
            stewardship_rank=0,
            window_start="2026-09-28T00:00:01Z",
            window_end="2026-09-28T00:00:01Z",
        )
        plan = plan_provider_routes(
            [later, earlier],
            required_capability="CODE_REVIEW",
            evaluated_at="2026-09-28T00:00:01Z",
            max_evidence_age_seconds=1,
        )
        self.assertEqual(
            plan["eligible_routes"][0]["observed_at"],
            "2026-09-28T00:00:00Z",
        )
        self.assertEqual(
            plan["eligible_routes"][0]["fresh_until"],
            "2026-09-28T00:00:01Z",
        )
        self.assertEqual(
            plan["eligible_routes"][1]["fresh_until"],
            "2026-09-28T00:00:02Z",
        )
        self.assertEqual(plan["evidence_fresh_until"], "2026-09-28T00:00:01Z")
        self.assertEqual(
            validate_provider_broker_plan(
                plan,
                consumed_at="2026-09-28T00:00:01Z",
            ),
            plan,
        )

        extended = deepcopy(plan)
        extended["evidence_fresh_until"] = "2026-09-28T00:00:02Z"
        with self.assertRaisesRegex(ValidationError, "boundary is inconsistent"):
            validate_provider_broker_plan(
                extended,
                consumed_at="2026-09-28T00:00:01Z",
            )

        shifted_route = deepcopy(plan)
        shifted_route["eligible_routes"][0]["fresh_until"] = "2026-09-28T00:00:02Z"
        with self.assertRaisesRegex(ValidationError, "boundary is inconsistent"):
            validate_provider_broker_plan(
                shifted_route,
                consumed_at="2026-09-28T00:00:01Z",
            )

    def test_module_has_no_execution_or_provider_transport_dependency(self):
        source = (
            ROOT / "atlas" / "provider_broker.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "httpx",
            "CodexAuditProvider",
            "BoundedResponsesAuditProvider",
            "cursor_agent",
            "datetime.now",
            "time.time",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
