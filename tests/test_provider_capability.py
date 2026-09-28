"""Provider capability descriptor and CODE_REVIEW normalization regressions."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.codex_audit import CodexAuditProvider
from atlas.final_audit import BoundedResponsesAuditProvider
from atlas.provider_capability import (
    CODE_REVIEW_RESULT_CONTRACT,
    codex_cli_capability_descriptor,
    descriptor_for_adapter,
    generic_audit_capability_descriptor,
    normalize_code_review_result,
    openai_responses_capability_descriptor,
    strip_provider_attribution,
    validate_provider_capability_descriptor,
    validate_provider_capability_result,
)
from atlas.provenance import ValidationError
from atlas.work_controller import AuditResult

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR_SCHEMA = ROOT / "docs/contracts/provider-capability-descriptor.schema.json"
RESULT_SCHEMA = ROOT / "docs/contracts/provider-capability-result.schema.json"
CODEX_FIXTURE = ROOT / "docs/contracts/fixtures/provider-capability-codex.example.json"
OPENAI_FIXTURE = ROOT / "docs/contracts/fixtures/provider-capability-openai.example.json"
GENERIC_FIXTURE = ROOT / "docs/contracts/fixtures/provider-capability-generic.example.json"
RESULT_FIXTURE = ROOT / "docs/contracts/fixtures/provider-capability-result.example.json"
HEAD = "a" * 40
EVIDENCE_REF = "audit:issue-123:exact-head"


class ProviderCapabilityTests(unittest.TestCase):
    def test_real_and_generic_descriptors_share_code_review_contract(self) -> None:
        descriptors = (
            codex_cli_capability_descriptor(),
            openai_responses_capability_descriptor(),
            generic_audit_capability_descriptor(),
        )
        identities = {
            (item["provider"], item["runtime"], item["usage_mode"], item["adapter"])
            for item in descriptors
        }
        self.assertEqual(len(identities), 3)
        for descriptor in descriptors:
            self.assertEqual(descriptor["authority"], "EVIDENCE_ONLY")
            self.assertEqual(
                descriptor["capabilities"],
                [
                    {
                        "name": "CODE_REVIEW",
                        "status": "SUPPORTED",
                        "result_contract": CODE_REVIEW_RESULT_CONTRACT,
                    }
                ],
            )

    def test_known_adapter_mapping_is_deterministic(self) -> None:
        self.assertTrue(callable(getattr(CodexAuditProvider, "audit", None)))
        self.assertTrue(
            callable(getattr(BoundedResponsesAuditProvider, "audit", None))
        )
        self.assertEqual(
            descriptor_for_adapter(CodexAuditProvider.__name__),
            codex_cli_capability_descriptor(),
        )
        self.assertEqual(
            descriptor_for_adapter(BoundedResponsesAuditProvider.__name__),
            openai_responses_capability_descriptor(),
        )
        self.assertEqual(
            descriptor_for_adapter("AuditPort"),
            generic_audit_capability_descriptor(),
        )
        self.assertEqual(
            descriptor_for_adapter("FixedAuditAdapter"),
            generic_audit_capability_descriptor(),
        )
        with self.assertRaises(ValidationError):
            descriptor_for_adapter("UnknownProvider")

    def test_same_audit_result_has_same_cross_provider_semantics(self) -> None:
        result = AuditResult(
            verdict="REWORK",
            findings="raw provider finding must not be retained",
        )
        codex = normalize_code_review_result(
            codex_cli_capability_descriptor(),
            result,
            subject_head=HEAD,
            evidence_ref=EVIDENCE_REF,
        )
        openai = normalize_code_review_result(
            openai_responses_capability_descriptor(),
            result,
            subject_head=HEAD,
            evidence_ref=EVIDENCE_REF,
        )
        self.assertEqual(strip_provider_attribution(codex), strip_provider_attribution(openai))
        self.assertNotEqual(codex["provider"], openai["provider"])
        self.assertNotIn("findings", json.dumps(codex))
        self.assertNotIn("raw provider finding", json.dumps(codex))

    def test_unsupported_code_review_cannot_normalize(self) -> None:
        descriptor = codex_cli_capability_descriptor()
        descriptor["capabilities"][0]["status"] = "UNSUPPORTED"
        descriptor["capabilities"][0]["result_contract"] = None
        validated = validate_provider_capability_descriptor(descriptor)
        with self.assertRaises(ValidationError):
            normalize_code_review_result(
                validated,
                AuditResult(verdict="PASS"),
                subject_head=HEAD,
                evidence_ref=EVIDENCE_REF,
            )

    def test_descriptor_rejects_unknown_duplicate_or_incompatible_capabilities(self) -> None:
        unknown = codex_cli_capability_descriptor()
        unknown["capabilities"][0]["name"] = "MAGIC"
        duplicate = codex_cli_capability_descriptor()
        duplicate["capabilities"].append(deepcopy(duplicate["capabilities"][0]))
        bad_status = codex_cli_capability_descriptor()
        bad_status["capabilities"][0]["status"] = "ENABLED"
        wrong_contract = codex_cli_capability_descriptor()
        wrong_contract["capabilities"][0]["result_contract"] = "other_v1"
        non_review_contract = codex_cli_capability_descriptor()
        non_review_contract["capabilities"] = [
            {"name": "PLAN", "status": "SUPPORTED", "result_contract": "audit_result_v1"}
        ]
        for payload in (
            unknown,
            duplicate,
            bad_status,
            wrong_contract,
            non_review_contract,
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_provider_capability_descriptor(payload)

    def test_descriptor_rejects_non_integer_version_and_identity_leaks(self) -> None:
        cases = []
        for value in (True, 1.0, "1", 2):
            payload = codex_cli_capability_descriptor()
            payload["schema_version"] = value
            cases.append(payload)
        for field, value in (
            ("provider", "ghp_secretlike"),
            ("runtime", "/tmp/runtime"),
            ("usage_mode", "sk-secret"),
            ("adapter", "path/to/adapter"),
        ):
            payload = codex_cli_capability_descriptor()
            payload[field] = value
            cases.append(payload)
        extra = codex_cli_capability_descriptor()
        extra["endpoint"] = "https://provider.example"
        cases.append(extra)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_provider_capability_descriptor(payload)

    def test_result_validation_is_content_free_and_fail_closed(self) -> None:
        valid = normalize_code_review_result(
            codex_cli_capability_descriptor(),
            AuditResult(verdict="HUMAN_REQUIRED", findings="sensitive detail"),
            subject_head=HEAD,
            evidence_ref=EVIDENCE_REF,
        )
        self.assertEqual(validate_provider_capability_result(valid), valid)

        cases = []
        bad_outcome = deepcopy(valid)
        bad_outcome["outcome"] = "BLOCK"
        cases.append(bad_outcome)
        bad_head = deepcopy(valid)
        bad_head["evidence"]["subject_head"] = "abc"
        cases.append(bad_head)
        bad_ref = deepcopy(valid)
        bad_ref["evidence"]["evidence_ref"] = "/tmp/evidence"
        cases.append(bad_ref)
        secret_ref = deepcopy(valid)
        secret_ref["evidence"]["evidence_ref"] = "ghp_secretlike"
        cases.append(secret_ref)
        raw_findings = deepcopy(valid)
        raw_findings["findings"] = "must never survive normalization"
        cases.append(raw_findings)
        float_version = deepcopy(valid)
        float_version["schema_version"] = 1.0
        cases.append(float_version)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_provider_capability_result(payload)

    def test_public_schemas_match_runtime_contract_and_fixtures(self) -> None:
        descriptor_schema = json.loads(DESCRIPTOR_SCHEMA.read_text(encoding="utf-8"))
        result_schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(descriptor_schema)
        Draft202012Validator.check_schema(result_schema)
        descriptor_validator = Draft202012Validator(descriptor_schema)
        result_validator = Draft202012Validator(result_schema)

        codex_fixture = json.loads(CODEX_FIXTURE.read_text(encoding="utf-8"))
        openai_fixture = json.loads(OPENAI_FIXTURE.read_text(encoding="utf-8"))
        generic_fixture = json.loads(GENERIC_FIXTURE.read_text(encoding="utf-8"))
        result_fixture = json.loads(RESULT_FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(codex_fixture, codex_cli_capability_descriptor())
        self.assertEqual(openai_fixture, openai_responses_capability_descriptor())
        self.assertEqual(generic_fixture, generic_audit_capability_descriptor())
        descriptor_validator.validate(codex_fixture)
        descriptor_validator.validate(openai_fixture)
        descriptor_validator.validate(generic_fixture)
        result_validator.validate(result_fixture)
        self.assertEqual(validate_provider_capability_result(result_fixture), result_fixture)

        invalid_contract = codex_cli_capability_descriptor()
        invalid_contract["capabilities"] = [
            {"name": "PLAN", "status": "SUPPORTED", "result_contract": "audit_result_v1"}
        ]
        with self.assertRaises(JsonSchemaValidationError):
            descriptor_validator.validate(invalid_contract)

        duplicate_name = codex_cli_capability_descriptor()
        duplicate_name["capabilities"].append(
            {"name": "CODE_REVIEW", "status": "UNKNOWN", "result_contract": None}
        )
        with self.assertRaises(JsonSchemaValidationError):
            descriptor_validator.validate(duplicate_name)

        secret_identity = codex_cli_capability_descriptor()
        secret_identity["adapter"] = "adapter-ghp_secretlike"
        with self.assertRaises(JsonSchemaValidationError):
            descriptor_validator.validate(secret_identity)

    def test_module_has_no_execution_or_selection_dependency(self) -> None:
        source = (ROOT / "atlas/provider_capability.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests",
            "httpx",
            "urllib",
            "subprocess",
            "socket",
            "OPENAI_API_KEY",
            "codex exec",
            "agent persist",
            "winner",
            "ranking",
            "recommendation",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
