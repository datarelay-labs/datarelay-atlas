"""Provider capacity attribution evidence regressions."""

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
    validate_provider_route_candidate,
)
from atlas.provider_capacity_attribution import (
    build_unknown_provider_capacity_attribution,
    validate_provider_capacity_attribution,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import (
    FRESH_EVALUATED_AT,
    _candidate,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"
SCHEMA = CONTRACTS / "provider-capacity-attribution.schema.json"
FIXTURE = FIXTURES / "provider-capacity-attribution.example.json"

def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _observed(provider: str = "codex") -> dict:
    payload = _json(FIXTURE)
    payload["provider"] = provider
    return payload


class ProviderCapacityAttributionTests(unittest.TestCase):
    def test_fixture_matches_schema_and_runtime(self) -> None:
        schema = _json(SCHEMA)
        fixture = _json(FIXTURE)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(
            validate_provider_capacity_attribution(fixture),
            fixture,
        )

    def test_unknown_builder_keeps_every_fact_unknown(self) -> None:
        payload = build_unknown_provider_capacity_attribution(
            provider="codex",
            source_kind="unverified_observation",
        )
        self.assertEqual(payload["evidence"]["authority"], "UNVERIFIED")
        self.assertIsNone(payload["evidence"]["source_ref"])
        self.assertIsNone(payload["evidence"]["source_digest"])
        self.assertIsNone(payload["evidence"]["observed_at"])
        for value in payload["facts"].values():
            self.assertEqual(value, {"status": "UNKNOWN"})

    def test_observed_fact_requires_provider_authoritative_evidence(self) -> None:
        payload = _observed()
        payload["evidence"]["authority"] = "UNVERIFIED"
        payload["evidence"]["source_ref"] = None
        payload["evidence"]["source_digest"] = None
        payload["evidence"]["observed_at"] = None

        with self.assertRaisesRegex(
            ValidationError,
            "PROVIDER_AUTHORITATIVE",
        ):
            validate_provider_capacity_attribution(payload)

        schema = _json(SCHEMA)
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_unknown_facts_cannot_carry_values(self) -> None:
        payload = build_unknown_provider_capacity_attribution(
            provider="codex",
            source_kind="unverified_observation",
        )
        payload["facts"]["charging_mode"] = {
            "status": "UNKNOWN",
            "value": "payg",
        }
        with self.assertRaises(ValidationError):
            validate_provider_capacity_attribution(payload)

        schema = _json(SCHEMA)
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(payload)

    def test_observed_values_are_generic_bounded_facts(self) -> None:
        payload = _observed()
        payload["facts"]["shared_allowance"] = {
            "status": "OBSERVED",
            "value": True,
        }
        payload["facts"]["charging_mode"] = {
            "status": "OBSERVED",
            "value": "usage_credit",
        }
        normalized = validate_provider_capacity_attribution(payload)
        self.assertTrue(normalized["facts"]["shared_allowance"]["value"])
        self.assertEqual(
            normalized["facts"]["charging_mode"]["value"],
            "usage_credit",
        )

    def test_unverified_evidence_cannot_claim_source_provenance(self) -> None:
        base = build_unknown_provider_capacity_attribution(
            provider="codex",
            source_kind="unverified_observation",
        )
        cases = (
            ("source_ref", "https://provider.example/docs/quota"),
            ("source_digest", "a" * 64),
            ("observed_at", "2026-09-30T00:00:00Z"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                payload = deepcopy(base)
                payload["evidence"][field] = value
                with self.assertRaisesRegex(
                    ValidationError,
                    "cannot claim source provenance",
                ):
                    validate_provider_capacity_attribution(payload)

    def test_authoritative_evidence_requires_verifiable_source_provenance(self) -> None:
        schema = _json(SCHEMA)
        for field, value in (
            ("source_ref", None),
            ("source_digest", None),
            ("source_digest", "not-a-digest"),
        ):
            with self.subTest(field=field, value=value):
                payload = _observed()
                payload["evidence"][field] = value
                with self.assertRaises(ValidationError):
                    validate_provider_capacity_attribution(payload)
                with self.assertRaises(JsonSchemaValidationError):
                    Draft202012Validator(schema).validate(payload)

    def test_secret_like_source_and_fact_labels_fail_closed(self) -> None:
        for mutator in (
            lambda payload: payload["evidence"].__setitem__(
                "source_ref", "ghp_secretlike"
            ),
            lambda payload: payload["facts"].__setitem__(
                "allowance_domain",
                {"status": "OBSERVED", "value": "sk-secret"},
            ),
        ):
            payload = _observed()
            mutator(payload)
            with self.assertRaises(ValidationError):
                validate_provider_capacity_attribution(payload)

    def test_optional_candidate_binding_preserves_legacy_shape(self) -> None:
        legacy = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=10,
        )
        normalized = validate_provider_route_candidate(deepcopy(legacy))
        self.assertEqual(normalized, legacy)
        self.assertNotIn("capacity_attribution", normalized)

        attributed = deepcopy(legacy)
        attributed["capacity_attribution"] = _observed()
        normalized_attributed = validate_provider_route_candidate(attributed)
        self.assertEqual(
            normalized_attributed["capacity_attribution"],
            _observed(),
        )

    def test_candidate_attribution_provider_must_match_route_provider(self) -> None:
        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=10,
        )
        candidate["capacity_attribution"] = _observed("openai")
        with self.assertRaisesRegex(
            ValidationError,
            "capacity attribution",
        ):
            validate_provider_route_candidate(candidate)

    def test_broker_plan_is_unchanged_by_attribution_evidence(self) -> None:
        legacy = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=10,
        )
        attributed = deepcopy(legacy)
        attributed["capacity_attribution"] = _observed()

        baseline = plan_provider_routes(
            [legacy],
            required_capability="CODE_REVIEW",
            evaluated_at=FRESH_EVALUATED_AT,
            max_evidence_age_seconds=0,
        )
        with_attribution = plan_provider_routes(
            [attributed],
            required_capability="CODE_REVIEW",
            evaluated_at=FRESH_EVALUATED_AT,
            max_evidence_age_seconds=0,
        )
        self.assertEqual(with_attribution, baseline)

    def test_candidate_schema_accepts_optional_attribution(self) -> None:
        capability_schema = _json(
            CONTRACTS / "provider-capability-descriptor.schema.json"
        )
        capacity_schema = _json(
            CONTRACTS / "provider-capacity-input.schema.json"
        )
        attribution_schema = _json(SCHEMA)
        candidate_schema = _json(
            CONTRACTS / "provider-route-candidate.schema.json"
        )
        registry = Registry()
        for schema in (
            capability_schema,
            capacity_schema,
            attribution_schema,
        ):
            registry = registry.with_resource(
                schema["$id"],
                Resource.from_contents(schema),
            )

        candidate = _candidate(
            "codex",
            "codex-primary",
            capability_rank=0,
            stewardship_rank=10,
        )
        candidate["capacity_attribution"] = _observed()
        Draft202012Validator(
            candidate_schema,
            registry=registry,
        ).validate(candidate)

    def test_runtime_contains_no_vendor_quota_assumptions_or_transport(self) -> None:
        source = (
            ROOT / "atlas" / "provider_capacity_attribution.py"
        ).read_text(encoding="utf-8").lower()
        for forbidden in (
            "chatgpt",
            "claude",
            "anthropic",
            "cursor",
            "subprocess",
            "requests",
            "urllib",
            "socket",
            "os.environ",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
