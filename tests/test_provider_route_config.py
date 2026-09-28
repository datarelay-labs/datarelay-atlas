"""Approved provider route configuration binding regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.provider_broker import plan_provider_routes
from atlas.provider_route_config import (
    bind_configured_provider_route,
    load_provider_route_set,
    validate_provider_route_set,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import _capacity, _descriptor

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"
SCHEMA = CONTRACTS / "provider-route-set.schema.json"
FIXTURE = FIXTURES / "provider-route-set.example.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _gates(**overrides: str) -> dict[str, str]:
    result = {
        "policy": "ALLOW",
        "trust": "ALLOW",
        "budget": "ALLOW",
        "usage_mode": "ALLOW",
        "blast_radius": "ALLOW",
        "wip": "ALLOW",
    }
    result.update(overrides)
    return result


def _route_set() -> dict:
    return _json(FIXTURE)


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


class ProviderRouteConfigTests(unittest.TestCase):
    def test_public_schema_fixture_and_runtime_match(self) -> None:
        schema = _json(SCHEMA)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        fixture = _route_set()

        validator.validate(fixture)
        self.assertEqual(validate_provider_route_set(fixture), fixture)

        leaked = deepcopy(fixture)
        leaked["routes"][0]["adapter"] = "ghp_secretlike"
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(leaked)
        with self.assertRaises(ValidationError):
            validate_provider_route_set(leaked)

    def test_route_and_capability_order_normalize_deterministically(self) -> None:
        one = _route_set()
        two = deepcopy(one)
        two["routes"] = list(reversed(two["routes"]))

        one["routes"][0]["allowed_capabilities"] = [
            "USAGE_INSIGHTS",
            "CODE_REVIEW",
        ]
        two["routes"][1]["allowed_capabilities"] = [
            "CODE_REVIEW",
            "USAGE_INSIGHTS",
        ]

        normalized_one = validate_provider_route_set(one)
        normalized_two = validate_provider_route_set(two)
        self.assertEqual(normalized_one, normalized_two)
        self.assertEqual(
            [item["route_id"] for item in normalized_one["routes"]],
            ["codex-primary", "openai-fallback"],
        )
        self.assertEqual(
            normalized_one["routes"][0]["allowed_capabilities"],
            ["CODE_REVIEW", "USAGE_INSIGHTS"],
        )

    def test_binding_uses_configured_identity_ranks_and_dynamic_gates(self) -> None:
        route_set = _route_set()
        candidate = bind_configured_provider_route(
            route_set,
            route_id="codex-primary",
            capability_descriptor=_descriptor("codex"),
            capacity_input=_capacity("codex"),
            gates=_gates(budget="UNKNOWN", trust="DENY"),
            required_capability="CODE_REVIEW",
        )

        self.assertEqual(candidate["route_id"], "codex-primary")
        self.assertEqual(candidate["ranks"], {
            "capability_preference": 0,
            "stewardship_preference": 10,
        })
        self.assertEqual(candidate["gates"]["budget"], "UNKNOWN")
        self.assertEqual(candidate["gates"]["trust"], "DENY")

        plan = plan_provider_routes(
            [candidate],
            required_capability="CODE_REVIEW",
        )
        self.assertIsNone(plan["selected_route_id"])
        self.assertEqual(
            plan["ineligible_routes"][0]["reasons"],
            ["TRUST_DENY", "BUDGET_UNKNOWN"],
        )

    def test_disabled_or_unconfigured_route_cannot_bind(self) -> None:
        route_set = _route_set()
        route_set["routes"][0]["enabled"] = False
        with self.assertRaisesRegex(ValidationError, "route is disabled"):
            bind_configured_provider_route(
                route_set,
                route_id="codex-primary",
                capability_descriptor=_descriptor("codex"),
                capacity_input=_capacity("codex"),
                gates=_gates(),
                required_capability="CODE_REVIEW",
            )

        with self.assertRaisesRegex(ValidationError, "is not configured"):
            bind_configured_provider_route(
                _route_set(),
                route_id="missing-route",
                capability_descriptor=_descriptor("codex"),
                capacity_input=_capacity("codex"),
                gates=_gates(),
                required_capability="CODE_REVIEW",
            )

    def test_descriptor_identity_must_match_static_configuration(self) -> None:
        route_set = _route_set()
        base = _descriptor("codex")
        mutations = (
            ("provider", "openai"),
            ("runtime", "other_runtime"),
            ("usage_mode", "api"),
            ("adapter", "OtherAdapter"),
        )
        for field, value in mutations:
            descriptor = deepcopy(base)
            descriptor[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValidationError,
                    "descriptor identity mismatch",
                ):
                    bind_configured_provider_route(
                        route_set,
                        route_id="codex-primary",
                        capability_descriptor=descriptor,
                        capacity_input=_capacity("codex"),
                        gates=_gates(),
                        required_capability="CODE_REVIEW",
                    )

    def test_required_capability_needs_config_approval_and_descriptor_support(self) -> None:
        not_approved = _route_set()
        not_approved["routes"][0]["allowed_capabilities"] = [
            "USAGE_INSIGHTS"
        ]
        with self.assertRaisesRegex(ValidationError, "is not approved"):
            bind_configured_provider_route(
                not_approved,
                route_id="codex-primary",
                capability_descriptor=_descriptor("codex"),
                capacity_input=_capacity("codex"),
                gates=_gates(),
                required_capability="CODE_REVIEW",
            )

        unsupported = _descriptor("codex")
        unsupported["capabilities"][0]["status"] = "UNSUPPORTED"
        unsupported["capabilities"][0]["result_contract"] = None
        with self.assertRaisesRegex(ValidationError, "is not supported"):
            bind_configured_provider_route(
                _route_set(),
                route_id="codex-primary",
                capability_descriptor=unsupported,
                capacity_input=_capacity("codex"),
                gates=_gates(),
                required_capability="CODE_REVIEW",
            )

    def test_descriptor_cannot_smuggle_unapproved_supported_capability(self) -> None:
        descriptor = _descriptor("codex")
        descriptor["capabilities"].append(
            {
                "name": "USAGE_INSIGHTS",
                "status": "SUPPORTED",
                "result_contract": None,
            }
        )
        with self.assertRaisesRegex(
            ValidationError,
            "unapproved supported capability",
        ):
            bind_configured_provider_route(
                _route_set(),
                route_id="codex-primary",
                capability_descriptor=descriptor,
                capacity_input=_capacity("codex"),
                gates=_gates(),
                required_capability="CODE_REVIEW",
            )

        approved = _route_set()
        approved["routes"][0]["allowed_capabilities"].append(
            "USAGE_INSIGHTS"
        )
        candidate = bind_configured_provider_route(
            approved,
            route_id="codex-primary",
            capability_descriptor=descriptor,
            capacity_input=_capacity("codex"),
            gates=_gates(),
            required_capability="CODE_REVIEW",
        )
        self.assertEqual(
            [item["name"] for item in candidate["capability_descriptor"]["capabilities"]],
            ["CODE_REVIEW", "USAGE_INSIGHTS"],
        )

    def test_capacity_provider_and_gate_contract_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "capacity provider mismatch"):
            bind_configured_provider_route(
                _route_set(),
                route_id="codex-primary",
                capability_descriptor=_descriptor("codex"),
                capacity_input=_capacity("openai"),
                gates=_gates(),
                required_capability="CODE_REVIEW",
            )

        bad_gates = _gates()
        bad_gates["new_gate"] = "ALLOW"
        with self.assertRaisesRegex(ValidationError, "gates schema"):
            bind_configured_provider_route(
                _route_set(),
                route_id="codex-primary",
                capability_descriptor=_descriptor("codex"),
                capacity_input=_capacity("codex"),
                gates=bad_gates,
                required_capability="CODE_REVIEW",
            )

        invalid_state = _gates(policy="MAYBE")
        with self.assertRaisesRegex(ValidationError, "policy gate is invalid"):
            bind_configured_provider_route(
                _route_set(),
                route_id="codex-primary",
                capability_descriptor=_descriptor("codex"),
                capacity_input=_capacity("codex"),
                gates=invalid_state,
                required_capability="CODE_REVIEW",
            )

    def test_duplicate_and_unknown_static_configuration_fail_closed(self) -> None:
        duplicate_route = _route_set()
        duplicate_route["routes"].append(
            deepcopy(duplicate_route["routes"][0])
        )
        with self.assertRaisesRegex(ValidationError, "route ids must be unique"):
            validate_provider_route_set(duplicate_route)

        duplicate_capability = _route_set()
        duplicate_capability["routes"][0]["allowed_capabilities"] = [
            "CODE_REVIEW",
            "CODE_REVIEW",
        ]
        with self.assertRaisesRegex(ValidationError, "capabilities must be unique"):
            validate_provider_route_set(duplicate_capability)

        unknown_capability = _route_set()
        unknown_capability["routes"][0]["allowed_capabilities"] = [
            "NOT_A_CAPABILITY"
        ]
        with self.assertRaisesRegex(ValidationError, "capability is unsupported"):
            validate_provider_route_set(unknown_capability)

    def test_equivalent_input_order_produces_same_candidate(self) -> None:
        route_set = _route_set()
        reversed_set = deepcopy(route_set)
        reversed_set["routes"] = list(reversed(reversed_set["routes"]))

        gates = _gates()
        reversed_gates = dict(reversed(list(gates.items())))
        first = bind_configured_provider_route(
            route_set,
            route_id="codex-primary",
            capability_descriptor=_descriptor("codex"),
            capacity_input=_capacity("codex"),
            gates=gates,
            required_capability="CODE_REVIEW",
        )
        second = bind_configured_provider_route(
            reversed_set,
            route_id="codex-primary",
            capability_descriptor=_descriptor("codex"),
            capacity_input=_capacity("codex"),
            gates=reversed_gates,
            required_capability="CODE_REVIEW",
        )
        self.assertEqual(first, second)

    def test_loader_rejects_duplicate_keys_oversize_and_nonfinite(self) -> None:
        duplicate = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        )
        duplicate.write(
            '{"schema_version":1,"schema_version":1,'
            '"kind":"approved_provider_route_set",'
            '"authority":"CONFIGURATION_ONLY","routes":[]}'
        )
        duplicate.close()
        with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
            load_provider_route_set(Path(duplicate.name))

        oversized = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".json",
            delete=False,
        )
        oversized.write(b"{" + (b" " * (1024 * 1024)) + b"}")
        oversized.close()
        with self.assertRaisesRegex(ValidationError, "bounded input size"):
            load_provider_route_set(Path(oversized.name))

        nonfinite = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        )
        nonfinite.write(
            '{"schema_version":1,"kind":"approved_provider_route_set",'
            '"authority":"CONFIGURATION_ONLY","routes":NaN}'
        )
        nonfinite.close()
        with self.assertRaisesRegex(ValidationError, "non-finite number"):
            load_provider_route_set(Path(nonfinite.name))

    def test_module_has_no_provider_transport_or_execution_dependency(self) -> None:
        source = (ROOT / "atlas/provider_route_config.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "subprocess",
            "socket",
            "requests",
            "httpx",
            "urllib",
            "openai",
            "anthropic",
            "agent persist",
            "cursor_agent",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
