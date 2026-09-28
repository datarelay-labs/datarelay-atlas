"""Canonical provider-route catalog regressions."""

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
from atlas.provider_capability import (
    codex_cli_capability_descriptor,
    openai_responses_capability_descriptor,
)
from atlas.provider_routes import (
    configured_route_descriptor,
    load_provider_route_catalog,
    materialize_provider_route_candidate,
    validate_provider_route_catalog,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import _capacity

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs/contracts"
FIXTURES = CONTRACTS / "fixtures"
CATALOG_SCHEMA = CONTRACTS / "provider-route-catalog.schema.json"
CATALOG_FIXTURE = FIXTURES / "provider-route-catalog.example.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog() -> dict:
    return _json(CATALOG_FIXTURE)


def _gates() -> dict[str, str]:
    return {
        "policy": "ALLOW",
        "trust": "ALLOW",
        "budget": "ALLOW",
        "usage_mode": "ALLOW",
        "blast_radius": "ALLOW",
        "wip": "ALLOW",
    }


def _ranks(capability: int = 0, stewardship: int = 0) -> dict[str, int]:
    return {
        "capability_preference": capability,
        "stewardship_preference": stewardship,
    }


class ProviderRouteCatalogTests(unittest.TestCase):
    def test_public_schema_fixture_and_runtime_match(self) -> None:
        schema = _json(CATALOG_SCHEMA)
        fixture = _catalog()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_provider_route_catalog(fixture), fixture)

        reversed_fixture = deepcopy(fixture)
        reversed_fixture["routes"].reverse()
        self.assertEqual(
            validate_provider_route_catalog(reversed_fixture),
            fixture,
        )

    def test_only_registered_real_adapters_are_configurable(self) -> None:
        for adapter in ("AuditPort", "FixedAuditAdapter", "UnknownProvider"):
            payload = _catalog()
            payload["routes"][0]["adapter"] = adapter
            with self.subTest(adapter=adapter):
                with self.assertRaises(JsonSchemaValidationError):
                    Draft202012Validator(_json(CATALOG_SCHEMA)).validate(
                        payload
                    )
                with self.assertRaises(ValidationError):
                    validate_provider_route_catalog(payload)

    def test_catalog_rejects_identity_duplicates_secrets_and_extra_claims(self) -> None:
        cases: list[dict] = []

        duplicate = _catalog()
        duplicate["routes"][1]["route_id"] = "codex-review"
        cases.append(duplicate)

        duplicate_adapter = _catalog()
        duplicate_adapter["routes"][1]["adapter"] = "CodexAuditProvider"
        cases.append(duplicate_adapter)

        secret = _catalog()
        secret["routes"][0]["route_id"] = "ghp_secretlike"
        cases.append(secret)

        bad_state = _catalog()
        bad_state["routes"][0]["state"] = "APPROVED"
        cases.append(bad_state)

        forged_provider = _catalog()
        forged_provider["routes"][0]["provider"] = "codex"
        cases.append(forged_provider)

        endpoint = _catalog()
        endpoint["routes"][0]["endpoint"] = "https://provider.example"
        cases.append(endpoint)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_provider_route_catalog(payload)

    def test_enabled_route_resolves_canonical_descriptor(self) -> None:
        catalog = _catalog()
        self.assertEqual(
            configured_route_descriptor(catalog, route_id="codex-review"),
            codex_cli_capability_descriptor(),
        )
        self.assertEqual(
            configured_route_descriptor(catalog, route_id="openai-review"),
            openai_responses_capability_descriptor(),
        )

    def test_disabled_or_unknown_route_cannot_materialize(self) -> None:
        catalog = _catalog()
        catalog["routes"][0]["state"] = "DISABLED"
        with self.assertRaisesRegex(ValidationError, "route is disabled"):
            materialize_provider_route_candidate(
                catalog,
                route_id="codex-review",
                capacity_input=_capacity("codex"),
                gates=_gates(),
                ranks=_ranks(),
            )

        with self.assertRaisesRegex(ValidationError, "not configured"):
            configured_route_descriptor(
                _catalog(),
                route_id="missing-route",
            )

    def test_materialized_candidate_uses_derived_descriptor_only(self) -> None:
        candidate = materialize_provider_route_candidate(
            _catalog(),
            route_id="codex-review",
            capacity_input=_capacity("codex"),
            gates=_gates(),
            ranks=_ranks(capability=1, stewardship=2),
        )
        self.assertEqual(candidate["route_id"], "codex-review")
        self.assertEqual(
            candidate["capability_descriptor"],
            codex_cli_capability_descriptor(),
        )
        self.assertEqual(candidate["ranks"]["capability_preference"], 1)
        self.assertEqual(candidate["ranks"]["stewardship_preference"], 2)

        capability_schema = _json(
            CONTRACTS / "provider-capability-descriptor.schema.json"
        )
        capacity_schema = _json(
            CONTRACTS / "provider-capacity-input.schema.json"
        )
        candidate_schema = _json(
            CONTRACTS / "provider-route-candidate.schema.json"
        )
        registry = Registry()
        for schema in (capability_schema, capacity_schema):
            registry = registry.with_resource(
                schema["$id"],
                Resource.from_contents(schema),
            )
        Draft202012Validator(
            candidate_schema,
            registry=registry,
        ).validate(candidate)

    def test_capacity_provider_must_match_derived_provider(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "provider identity does not match",
        ):
            materialize_provider_route_candidate(
                _catalog(),
                route_id="codex-review",
                capacity_input=_capacity("openai"),
                gates=_gates(),
                ranks=_ranks(),
            )

    def test_dynamic_gates_and_ranks_are_not_catalog_state(self) -> None:
        catalog = _catalog()
        encoded = json.dumps(catalog, sort_keys=True)
        for forbidden in (
            '"policy"',
            '"trust"',
            '"budget"',
            '"usage_mode"',
            '"blast_radius"',
            '"wip"',
            '"ranks"',
            '"capabilities"',
            '"provider"',
            '"runtime"',
            '"endpoint"',
            '"model"',
            '"credential"',
        ):
            self.assertNotIn(forbidden, encoded)

    def test_loader_and_cli_are_read_only(self) -> None:
        source = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        )
        json.dump(_catalog(), source)
        source.close()
        self.assertEqual(
            load_provider_route_catalog(Path(source.name)),
            _catalog(),
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                [
                    "usage",
                    "provider-route-catalog",
                    "--input",
                    source.name,
                ]
            )
        self.assertEqual(rc, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), _catalog())

    def test_loader_rejects_duplicate_keys_and_oversize(self) -> None:
        duplicate = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        )
        duplicate.write(
            '{"schema_version":1,"schema_version":1,'
            '"kind":"provider_route_catalog","authority":"CONFIGURATION_ONLY",'
            '"routes":[]}'
        )
        duplicate.close()
        with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
            load_provider_route_catalog(Path(duplicate.name))

        oversized = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".json",
            delete=False,
        )
        oversized.write(b"{" + (b" " * (1024 * 1024)) + b"}")
        oversized.close()
        with self.assertRaisesRegex(ValidationError, "bounded input size"):
            load_provider_route_catalog(Path(oversized.name))

    def test_module_has_no_execution_transport_or_secret_dependency(self) -> None:
        source = (ROOT / "atlas/provider_routes.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests",
            "httpx",
            "urllib",
            "subprocess",
            "socket",
            "OPENAI_API_KEY",
            "codex exec",
            "agent persist",
            "Bearer ",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
