"""Learned-compressor local-canary evidence binding regressions for Atlas #76."""

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
from atlas.context_learned import (
    DATA_EGRESS_READY,
    RUNTIME_READY,
    _REQUIREMENT_BLOCKERS,
    _TRUST_BLOCKERS,
    bind_learned_canary_admission,
    load_learned_canary_binding,
    normalize_learned_canary_report,
)
from atlas.context_optimization import normalize_context_canary_report
from atlas.context_shadow import bind_shadow_quality
from atlas.provenance import ValidationError
from tests.test_context_shadow import _context_input, _shadow_report

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/context-optimization-input.schema.json"
SOURCE_COMMIT = "2c913302073367f2402d8bc4bb1a929a3f70a030"

_REQUIREMENT_KEYS = (
    "execution_mode",
    "endpoint_class",
    "external_egress",
    "protected_state_route",
    "deterministic_bypass",
    "exact_original_recovery_verified",
    "identifier_preservation_verified",
    "cache_behavior_verified",
    "provider_usage_capture_ready",
    "live_comparability_gate_available",
    "shadow_equivalence_gate_available",
    "trusted_runtime_evidence",
)


def _ready_report() -> dict:
    return {
        "schema_version": 1,
        "kind": "context-learned-canary-admission-report",
        "decision": "CANARY_READY",
        "setup_allowed": True,
        "canary_ready": True,
        "candidate": {
            "candidate_id": "paritok-local",
            "source_repo": "Paritok-official/paritok-4b-v1",
            "source_commit": SOURCE_COMMIT,
            "package_version": "1.3.13",
            "license": "Apache-2.0",
            "integration_mode": "LOCAL_SELF_HOST",
        },
        "requirements": {key: True for key in _REQUIREMENT_KEYS},
        "blockers": [],
    }


def _setup_report() -> dict:
    report = _ready_report()
    report["decision"] = "SETUP_ALLOWED"
    report["canary_ready"] = False
    report["requirements"]["external_egress"] = False
    report["blockers"] = ["EXTERNAL_EGRESS_NOT_DENIED"]
    return report


def _write_json(payload: object) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


def _legacy_context_input() -> dict:
    current = _context_input()
    evidence = current["source_evidence"]
    return normalize_context_canary_report(
        {
            "schema_version": 1,
            "kind": "context-canary-eligibility-report",
            "decision": "ELIGIBLE",
            "system_head": evidence["system_head"],
            "profile": current["scope"]["profile"],
            "repo": current["scope"]["repository"],
            "task_kind": current["scope"]["task_kind"],
            "record_count": evidence["record_count"],
            "arm_count": evidence["arm_count"],
            "arms": deepcopy(current["arms"]),
        }
    )


class ContextLearnedCanaryTests(unittest.TestCase):
    def test_canary_ready_advances_only_local_canary_gates(self) -> None:
        base = _context_input()
        bound = bind_learned_canary_admission(base, _ready_report())

        self.assertEqual(
            bound["gates"],
            {
                "quality_noninferiority": "UNKNOWN",
                "data_egress_eligibility": DATA_EGRESS_READY,
                "runtime_capability": RUNTIME_READY,
                "active_control": "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
            },
        )
        self.assertEqual(bound["control_mode"], "OBSERVE_ONLY")
        self.assertEqual(bound["source_evidence"], base["source_evidence"])
        self.assertEqual(bound["scope"], base["scope"])
        self.assertEqual(bound["arms"], base["arms"])
        evidence = bound["learned_canary_evidence"]
        self.assertEqual(
            evidence["source_kind"],
            "engineering_system_context_learned_canary_v1",
        )
        self.assertEqual(evidence["source_schema_version"], 1)
        self.assertEqual(evidence["decision"], "CANARY_READY")
        self.assertTrue(evidence["requirements"]["trusted_runtime_evidence"])
        self.assertEqual(evidence["candidate"]["source_commit"], SOURCE_COMMIT)

        encoded = json.dumps(bound, sort_keys=True)
        for forbidden in (
            '"winner"',
            '"ranking"',
            '"recommendation"',
            '"COMPRESS"',
            '"CLEAR"',
            '"YIELD"',
            '"ROUTE"',
            '"prompt"',
            '"transcript"',
            '"tool_output"',
            '"credential"',
        ):
            self.assertNotIn(forbidden, encoded)

    def test_canary_ready_preserves_exact_shadow_quality_binding(self) -> None:
        quality_bound = bind_shadow_quality(_context_input(), _shadow_report())
        bound = bind_learned_canary_admission(quality_bound, _ready_report())

        self.assertEqual(
            bound["gates"]["quality_noninferiority"],
            "SHADOW_ACTION_EQUIVALENT",
        )
        self.assertEqual(
            bound["quality_evidence"],
            quality_bound["quality_evidence"],
        )
        self.assertEqual(
            bound["gates"]["data_egress_eligibility"],
            DATA_EGRESS_READY,
        )
        self.assertEqual(bound["gates"]["runtime_capability"], RUNTIME_READY)
        self.assertEqual(
            bound["gates"]["active_control"],
            "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
        )
        self.assertEqual(bound["control_mode"], "OBSERVE_ONLY")

    def test_setup_allowed_attaches_evidence_without_runtime_promotion(self) -> None:
        bound = bind_learned_canary_admission(_context_input(), _setup_report())

        self.assertEqual(bound["gates"]["data_egress_eligibility"], "UNKNOWN")
        self.assertEqual(bound["gates"]["runtime_capability"], "UNKNOWN")
        self.assertEqual(
            bound["gates"]["active_control"],
            "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
        )
        evidence = bound["learned_canary_evidence"]
        self.assertEqual(evidence["decision"], "SETUP_ALLOWED")
        self.assertFalse(evidence["canary_ready"])
        self.assertEqual(
            evidence["blockers"],
            ["EXTERNAL_EGRESS_NOT_DENIED"],
        )

    def test_legacy_comparability_input_remains_bindable(self) -> None:
        legacy = _legacy_context_input()
        bound = bind_learned_canary_admission(legacy, _ready_report())

        self.assertEqual(
            legacy["source_evidence"]["source_schema_version"],
            1,
        )
        self.assertNotIn("run_set_digest", legacy["source_evidence"])
        self.assertEqual(bound["gates"]["quality_noninferiority"], "UNKNOWN")
        self.assertEqual(
            bound["gates"]["data_egress_eligibility"],
            DATA_EGRESS_READY,
        )
        self.assertEqual(bound["gates"]["runtime_capability"], RUNTIME_READY)

    def test_report_identity_and_internal_consistency_fail_closed(self) -> None:
        cases: list[dict] = []

        for value in (True, 1.0, "1", 2):
            wrong_version = _ready_report()
            wrong_version["schema_version"] = value
            cases.append(wrong_version)

        wrong_kind = _ready_report()
        wrong_kind["kind"] = "context-learned-score"
        cases.append(wrong_kind)

        unknown = _ready_report()
        unknown["score"] = 1
        cases.append(unknown)

        setup_false = _ready_report()
        setup_false["setup_allowed"] = False
        cases.append(setup_false)

        ready_false = _ready_report()
        ready_false["canary_ready"] = False
        cases.append(ready_false)

        missing_requirement = _ready_report()
        del missing_requirement["requirements"]["cache_behavior_verified"]
        cases.append(missing_requirement)

        bad_candidate = _ready_report()
        bad_candidate["candidate"]["source_commit"] = "not-a-sha"
        cases.append(bad_candidate)

        secret_candidate = _ready_report()
        secret_candidate["candidate"]["candidate_id"] = "ghp_secretlike"
        cases.append(secret_candidate)

        unknown_blocker = _setup_report()
        unknown_blocker["blockers"] = ["NOT_A_REAL_BLOCKER"]
        cases.append(unknown_blocker)

        wrong_blocker = _setup_report()
        wrong_blocker["blockers"] = ["ENDPOINT_NOT_LOOPBACK"]
        cases.append(wrong_blocker)

        duplicate_blocker = _setup_report()
        duplicate_blocker["blockers"] = [
            "EXTERNAL_EGRESS_NOT_DENIED",
            "EXTERNAL_EGRESS_NOT_DENIED",
        ]
        cases.append(duplicate_blocker)

        trust_false = _ready_report()
        trust_false["decision"] = "SETUP_ALLOWED"
        trust_false["canary_ready"] = False
        trust_false["requirements"]["trusted_runtime_evidence"] = False
        trust_false["blockers"] = ["TRUST_BOUNDARY_REQUIRED"]
        normalized = normalize_learned_canary_report(trust_false)
        self.assertEqual(
            normalized["blockers"],
            ["TRUST_BOUNDARY_REQUIRED"],
        )

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    normalize_learned_canary_report(payload)

    def test_blocker_order_and_requirement_mapping_are_strict(self) -> None:
        report = _ready_report()
        report["decision"] = "SETUP_ALLOWED"
        report["canary_ready"] = False
        report["requirements"]["endpoint_class"] = False
        report["requirements"]["external_egress"] = False
        report["requirements"]["trusted_runtime_evidence"] = False
        report["blockers"] = [
            "ENDPOINT_NOT_LOOPBACK",
            "EXTERNAL_EGRESS_NOT_DENIED",
            "TRUST_EVIDENCE_MISSING",
        ]
        normalized = normalize_learned_canary_report(report)
        self.assertEqual(normalized["blockers"], report["blockers"])

        reordered = deepcopy(report)
        reordered["blockers"][0], reordered["blockers"][1] = (
            reordered["blockers"][1],
            reordered["blockers"][0],
        )
        with self.assertRaises(ValidationError):
            normalize_learned_canary_report(reordered)

    def test_upstream_blocker_vocabulary_matrix_is_exact(self) -> None:
        for field, code in _REQUIREMENT_BLOCKERS:
            report = _ready_report()
            report["decision"] = "SETUP_ALLOWED"
            report["canary_ready"] = False
            report["requirements"][field] = False
            report["blockers"] = [code]
            self.assertEqual(
                normalize_learned_canary_report(report)["blockers"],
                [code],
            )

        for code in sorted(_TRUST_BLOCKERS):
            report = _ready_report()
            report["decision"] = "SETUP_ALLOWED"
            report["canary_ready"] = False
            report["requirements"]["trusted_runtime_evidence"] = False
            report["blockers"] = [code]
            self.assertEqual(
                normalize_learned_canary_report(report)["blockers"],
                [code],
            )

    def test_base_or_shadow_input_must_be_canonical(self) -> None:
        active = _context_input()
        active["gates"]["active_control"] = "ELIGIBLE"

        already_bound = bind_learned_canary_admission(
            _context_input(),
            _setup_report(),
        )

        forged_quality = bind_shadow_quality(_context_input(), _shadow_report())
        forged_quality["quality_evidence"]["run_set_digest"] = "c" * 64

        contradictory_quality = bind_shadow_quality(
            _context_input(),
            _shadow_report(),
        )
        contradictory_quality["gates"]["quality_noninferiority"] = "UNKNOWN"

        for base in (
            active,
            already_bound,
            forged_quality,
            contradictory_quality,
        ):
            with self.subTest(base=base):
                with self.assertRaises(ValidationError):
                    bind_learned_canary_admission(base, _ready_report())

    def test_loader_rejects_duplicate_keys_and_oversize(self) -> None:
        context_path = _write_json(_context_input())

        duplicate = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        duplicate.write(
            '{"schema_version":1,"schema_version":1,'
            '"kind":"context-learned-canary-admission-report"}'
        )
        duplicate.close()
        with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
            load_learned_canary_binding(context_path, Path(duplicate.name))

        oversized = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        )
        oversized.write(b"{" + (b" " * (1024 * 1024)) + b"}")
        oversized.close()
        with self.assertRaisesRegex(ValidationError, "bounded input size"):
            load_learned_canary_binding(context_path, Path(oversized.name))

    def test_cli_binding_is_read_only_and_content_free(self) -> None:
        context_path = _write_json(
            bind_shadow_quality(_context_input(), _shadow_report())
        )
        admission_path = _write_json(_ready_report())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                [
                    "usage",
                    "context-learned-bind",
                    "--context-input",
                    str(context_path),
                    "--admission-report",
                    str(admission_path),
                ]
            )

        self.assertEqual(rc, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["gates"]["quality_noninferiority"],
            "SHADOW_ACTION_EQUIVALENT",
        )
        self.assertEqual(
            payload["gates"]["data_egress_eligibility"],
            DATA_EGRESS_READY,
        )
        self.assertEqual(payload["gates"]["runtime_capability"], RUNTIME_READY)
        self.assertEqual(
            payload["gates"]["active_control"],
            "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
        )
        self.assertEqual(payload["control_mode"], "OBSERVE_ONLY")

    def test_schema_covers_base_shadow_setup_and_ready_states(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)

        base = _context_input()
        shadow = bind_shadow_quality(base, _shadow_report())
        setup = bind_learned_canary_admission(shadow, _setup_report())
        ready = bind_learned_canary_admission(shadow, _ready_report())

        for payload in (base, shadow, setup, ready):
            validator.validate(payload)

        missing = deepcopy(ready)
        del missing["learned_canary_evidence"]
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(missing)

        contradictory = deepcopy(setup)
        contradictory["gates"]["runtime_capability"] = RUNTIME_READY
        contradictory["gates"]["data_egress_eligibility"] = DATA_EGRESS_READY
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(contradictory)

        unbound = deepcopy(base)
        unbound["gates"]["runtime_capability"] = RUNTIME_READY
        unbound["gates"]["data_egress_eligibility"] = DATA_EGRESS_READY
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(unbound)

        for field, value in (
            ("candidate_id", "ghp_secretlike"),
            ("source_repo", "owner/ghp_secretlike"),
            ("package_version", "ghp_secretlike"),
            ("license", "AKIASECRETLIKE"),
        ):
            leaked = deepcopy(ready)
            leaked["learned_canary_evidence"]["candidate"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(JsonSchemaValidationError):
                    validator.validate(leaked)

    def test_module_has_no_provider_network_or_mutation_dependency(self) -> None:
        source = (ROOT / "atlas/context_learned.py").read_text(encoding="utf-8")
        for forbidden in (
            "requests",
            "httpx",
            "urllib",
            "subprocess",
            "socket",
            "gh issue edit",
            "agent persist",
            "openai",
            "anthropic",
            "winner",
            "ranking",
            "recommendation",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
