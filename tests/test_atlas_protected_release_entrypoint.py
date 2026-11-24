"""Synthetic exact-checkout CLI exercises, NOT protected runner authorization.

These tests prove the bounded qualification entrypoint executes the canonical
Git-checkout validation and fails closed on invalid trust context. Their fake
GitHub environment is test input only; no production or external credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "atlas_protected_release_gate.py"


class ProtectedQualificationEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.head = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
            text=True,
        ).strip()
        assert len(cls.head) == 40

    def synthetic_env(self, **overrides: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": "datarelay-labs/datarelay-atlas",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_REF_PROTECTED": "true",
                "RUNNER_ENVIRONMENT": "self-hosted",
                "ENGINEERING_RELEASE_CONTEXT": "protected-production",
                "AUTHORIZED_EXECUTION_CONTEXT": "protected-production",
                "RELEASE_PHASE": "qualify",
                "CANDIDATE_ROOT": str(ROOT),
                "EXPECTED_SHA": self.head,
            }
        )
        env.update(overrides)
        return env

    def run_cli(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), "qualify"],
            cwd=ROOT,
            env=self.synthetic_env(**overrides),
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def test_positive_synthetic_cli_is_boundary_only_not_release(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["candidate_sha"], self.head)
        self.assertEqual(evidence["boundary"], "PROTECTED_RUNNER_CONTEXT_VERIFIED")
        self.assertEqual(evidence["production_deployed"], "NOT_ASSERTED")
        self.assertEqual(evidence["authority"], "BOUNDARY_ONLY_NOT_RELEASE_AUTHORIZATION")

    def test_checkout_mismatch_fails_closed_in_real_cli(self) -> None:
        wrong_sha = "0" * 40 if self.head != "0" * 40 else "1" * 40
        result = self.run_cli(EXPECTED_SHA=wrong_sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ATLAS_PROTECTED_RELEASE_BLOCKED=CANDIDATE_HEAD_MISMATCH", result.stderr)

    def test_unprotected_dispatch_is_a_failure_not_a_skip(self) -> None:
        result = self.run_cli(GITHUB_REF_PROTECTED="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ATLAS_PROTECTED_RELEASE_BLOCKED=BOUNDARY_GITHUB_REF_PROTECTED_INVALID", result.stderr)

    def test_github_hosted_dispatch_is_rejected(self) -> None:
        result = self.run_cli(RUNNER_ENVIRONMENT="github-hosted")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ATLAS_PROTECTED_RELEASE_BLOCKED=BOUNDARY_RUNNER_ENVIRONMENT_INVALID", result.stderr)


if __name__ == "__main__":
    unittest.main()
