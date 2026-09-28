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


def _capacity(provider: str, remaining: str | None = "100") -> dict:
    payload = build_provider_capacity_input(
        provider=provider,
        source_kind="synthetic_test",
        window_start="2026-09-28T00:00:00Z",
        window_end="2026-09-28T00:00:00Z",
        event_count=1,
        total_tokens=1,
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
        "capacity_input": _capacity(provider, remaining),
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
        self.assertEqual(validate_provider_broker_plan(plan), plan)

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
            validate_provider_broker_plan(secret_plan)

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
        capability_first = plan_provider_routes(
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

        stewardship = plan_provider_routes(
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
        forward = plan_provider_routes(
            candidates,
            required_capability="CODE_REVIEW",
        )
        reverse = plan_provider_routes(
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
        plan = plan_provider_routes(
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
        plan = plan_provider_routes(
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
        plan = plan_provider_routes(
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

        plan = plan_provider_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        for value in (True, 1.0, "1", 2):
            broken = deepcopy(plan)
            broken["schema_version"] = value
            with self.subTest(plan_version=value):
                with self.assertRaises(ValidationError):
                    validate_provider_broker_plan(broken)

    def test_duplicate_routes_and_invalid_strategy_fail_closed(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        with self.assertRaises(ValidationError):
            plan_provider_routes(
                [candidate, deepcopy(candidate)],
                required_capability="CODE_REVIEW",
            )
        with self.assertRaises(ValidationError):
            plan_provider_routes(
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
        plan = plan_provider_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        wrong_selected = deepcopy(plan)
        wrong_selected["selected_route_id"] = None
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(wrong_selected)

        empty = deepcopy(plan)
        empty["selected_route_id"] = None
        empty["fallback_route_ids"] = []
        empty["eligible_routes"] = []
        empty["ineligible_routes"] = []
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(empty)
        plan_schema = _json(CONTRACTS / "provider-broker-plan.schema.json")
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(plan_schema).validate(empty)

        wrong_authority = deepcopy(plan)
        wrong_authority["authority"] = "EXECUTION"
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(wrong_authority)

        secret_identity = deepcopy(plan)
        secret_identity["eligible_routes"][0]["runtime"] = "ghp_secret"
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(secret_identity)

        wrong_fallback = deepcopy(plan)
        wrong_fallback["fallback_route_ids"] = ["made-up-route"]
        with self.assertRaises(ValidationError):
            validate_provider_broker_plan(wrong_fallback)
    def test_output_is_content_free_and_has_no_effect_authority(self):
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=0,
        )
        plan = plan_provider_routes(
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
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
