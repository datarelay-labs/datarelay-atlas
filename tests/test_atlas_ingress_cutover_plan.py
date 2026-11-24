"""Independent negative checks for non-executable ingress-aware transaction contract."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import atlas_ingress_cutover_plan as plancheck

ROOT = Path(__file__).resolve().parents[1]


class IngressTransactionPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-review-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        paths = (
            plancheck.PLAN,
            plancheck.INGRESS_UNIT,
            plancheck.INGRESS_SOCKET,
            plancheck.MCP_UNIT,
        )
        for rel in paths:
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, target)
        self.plan = json.loads((self.root / plancheck.PLAN).read_text())

    def validate(self, current=None):
        return plancheck.validate_plan(
            deepcopy(self.plan if current is None else current), root=self.root
        )

    def test_frozen_source_contract_is_static_only(self):
        result = self.validate()
        self.assertEqual(result["static_cutover_plan"], "PASS")
        self.assertEqual(result["production_effect"], "NONE")
        self.assertEqual(result["candidate_manifest_verification"], "NOT_EXECUTED")
        self.assertEqual(result["dynamicuser_sandbox_test"], "NOT_EXECUTED")
        self.assertEqual(result["rollback_execution"], "NOT_EXECUTED")
        self.assertEqual(result["native_mcp_write_acceptance"], "NOT_TESTED")
        self.assertEqual(result["release_authority"], "NONE")

    def test_every_required_preflight_is_mandatory(self):
        for gate in self.plan["required_preflight"]:
            with self.subTest(gate=gate):
                candidate = deepcopy(self.plan)
                candidate["required_preflight"].remove(gate)
                with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
                    self.validate(candidate)

    def test_every_forward_operation_is_mandatory(self):
        for step in self.plan["forward"]:
            with self.subTest(step=step):
                candidate = deepcopy(self.plan)
                candidate["forward"].remove(step)
                with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
                    self.validate(candidate)

    def test_every_rollback_operation_is_mandatory(self):
        for step in self.plan["rollback"]:
            with self.subTest(step=step):
                candidate = deepcopy(self.plan)
                candidate["rollback"].remove(step)
                with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
                    self.validate(candidate)

    def test_code_swap_before_ingress_stop_is_rejected(self):
        candidate = deepcopy(self.plan)
        candidate["forward"].remove("SWAP_VERIFIED_CODE")
        candidate["forward"].insert(0, "SWAP_VERIFIED_CODE")
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
            self.validate(candidate)

    def test_rollback_ingress_before_atlas_start_is_rejected(self):
        candidate = deepcopy(self.plan)
        candidate["rollback"].remove("START_INGRESS_SOCKET")
        candidate["rollback"].insert(4, "START_INGRESS_SOCKET")
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
            self.validate(candidate)

    def test_rollback_public_keycloak_required_even_if_mcp_recovers(self):
        candidate = deepcopy(self.plan)
        candidate["rollback"].remove("VERIFY_PUBLIC_KEYCLOAK_SNI_AND_ISSUER")
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
            self.validate(candidate)

    def test_no_automatic_pass_after_rollback_failure(self):
        for key, value in (
            ("rollback_failure_state", "SUCCESS"),
            ("failure_policy", "CONTINUE_ON_ERROR"),
            ("execution", "EXECUTABLE"),
            ("native_mcp_write_acceptance", "PASSED"),
        ):
            with self.subTest(key=key):
                candidate = deepcopy(self.plan)
                candidate[key] = value
                with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_VALUE"):
                    self.validate(candidate)

    def test_unexpected_input_or_arbitrary_shell_is_rejected(self):
        candidate = deepcopy(self.plan)
        candidate["rollback"].append("bash -c arbitrary")
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_SEQUENCE"):
            self.validate(candidate)
        candidate = deepcopy(self.plan)
        candidate["approval_tokens"] = ["secret"]
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "PLAN_FIELDS"):
            self.validate(candidate)

    def test_required_systemd_dependency_and_dynamic_user_fail_closed(self):
        tests = (
            (plancheck.INGRESS_UNIT, "DynamicUser=yes", "DynamicUser=no"),
            (plancheck.INGRESS_UNIT, "Requires=datarelay-atlas.service", "# Requires=datarelay-atlas.service"),
            (plancheck.INGRESS_UNIT, "ProtectSystem=strict", "ProtectSystem=false"),
            (plancheck.INGRESS_UNIT, "After=datarelay-atlas.service keycloak.service", "After=keycloak.service"),
            (plancheck.INGRESS_UNIT, "Wants=keycloak.service", "# Wants=keycloak.service"),
            (plancheck.INGRESS_UNIT, "Environment=PYTHONPATH=/opt/datarelay-atlas", "Environment=PYTHONPATH=/tmp"),
            (plancheck.INGRESS_UNIT, "ExecStart=/opt/datarelay-atlas/.venv/bin/python -m atlas.sni_ingress", "ExecStart=/bin/false"),
            (plancheck.INGRESS_SOCKET, "ListenStream=0.0.0.0:443", "ListenStream=127.0.0.1:443"),
            (plancheck.MCP_UNIT, "ExecStartPre=/opt/datarelay-atlas/.venv/bin/python -m atlas ops check --prod --env-file /etc/datarelay-atlas/service.env", "ExecStartPre=/bin/true"),
        )
        for rel, old, new in tests:
            with self.subTest(rel=rel, old=old):
                target = self.root / rel
                original = target.read_text()
                self.assertIn(old, original)
                target.write_text(original.replace(old, new, 1))
                with self.assertRaisesRegex(plancheck.CutoverPlanError, "UNIT_CONTRACT_MISMATCH"):
                    self.validate()
                target.write_text(original)

    def test_missing_or_symlinked_unit_is_not_validated(self):
        target = self.root / plancheck.INGRESS_SOCKET
        original = target.read_bytes()
        target.unlink()
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "REQUIRED_FILE_UNAVAILABLE"):
            self.validate()
        target.symlink_to(ROOT / plancheck.INGRESS_SOCKET)
        with self.assertRaisesRegex(plancheck.CutoverPlanError, "REQUIRED_FILE_UNAVAILABLE"):
            self.validate()
        target.unlink()
        target.write_bytes(original)
        self.assertEqual(self.validate()["static_cutover_plan"], "PASS")

    def test_actual_cli_is_non_executable_and_syntax_bounded(self):
        result = subprocess.run(
            [sys.executable, "scripts/atlas_ingress_cutover_plan.py", "check"],
            cwd=ROOT, capture_output=True, text=True, timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["production_effect"], "NONE")
        invalid = subprocess.run(
            [sys.executable, "scripts/atlas_ingress_cutover_plan.py", "deploy"],
            cwd=ROOT, capture_output=True, text=True, timeout=8,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("usage:", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
