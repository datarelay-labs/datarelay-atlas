"""Static regressions for the review-only protected release workflow."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
PIN = "e85d6a4de7e7df2dcbcf4f5dd43269bb308de195"


class ProtectedReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load(
            (ROOT / ".github/workflows/atlas-protected-release-boundary.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        cls.release = yaml.safe_load((ROOT / ".engineering/release.yaml").read_text())

    def test_manual_trigger_only(self):
        triggers = self.workflow["on"]
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_explicit_sha_and_bounded_phase(self):
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"expected_sha", "phase"})
        self.assertEqual(inputs["expected_sha"]["required"], "true")
        self.assertEqual(inputs["phase"]["options"], ["qualify", "post-release"])

    def test_immutable_canonical_workflow_no_secrets(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(set(jobs), {"bounded-qualification"})
        job = jobs["bounded-qualification"]
        self.assertEqual(
            job["uses"],
            "datarelay-labs/engineering-system/.github/workflows/release-contract.yml@" + PIN,
        )
        self.assertNotIn("secrets", job)
        self.assertNotIn("runs-on", job)
        self.assertNotIn("steps", job)
        self.assertIn("github.ref_protected", job["if"])
        self.assertIn("refs/heads/main", job["if"])
        self.assertEqual(
            job["with"]["profile_path"], ".engineering/release.yaml"
        )
        self.assertEqual(
            self.workflow["concurrency"]["cancel-in-progress"], "false"
        )

    def test_protected_profile_only_and_no_production_mutator(self):
        self.assertEqual(self.release["execution_context"], "protected-production")
        self.assertIs(self.release["exact_head_required"], True)
        self.assertIs(self.release["preflight_required"], True)
        self.assertEqual(
            self.release["preflight_command"],
            "python3 scripts/atlas_protected_release_gate.py preflight",
        )
        self.assertEqual(
            self.release["qualification_command"],
            "python3 scripts/atlas_protected_release_gate.py qualify",
        )
        self.assertIs(self.release["operational_e2e_required"], False)
        self.assertFalse(self.release["operational_e2e_command"])
        self.assertIs(self.release["public_smoke_required"], True)
        self.assertEqual(
            self.release["public_smoke_command"],
            "python3 scripts/atlas_protected_release_gate.py public-smoke",
        )
        commands = " ".join(
            str(self.release.get(k, ""))
            for k in ("preflight_command", "qualification_command", "operational_e2e_command", "public_smoke_command")
        )
        for forbidden in ("sudo ", "ssh ", "systemctl", "chown", "chmod", "scp "):
            self.assertNotIn(forbidden, commands)

    def test_pinned_baseline_is_full_commit_sha(self):
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40}", PIN))


if __name__ == "__main__":
    unittest.main()
