"""Synthetic-only metadata efficacy comparator validation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from atlas.task_value_benchmark import TaskValueError, evaluate, load_study

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
REPO = "datarelay-labs/datarelay-atlas"


def record(pair_no: int, condition: str, *, review=True):
    return {
        "pair_id": f"pair_{pair_no:03d}",
        "condition": condition,
        "repository": REPO,
        "start_head": HEAD,
        "work_class": "BUGFIX",
        "task_completed": True,
        "verified_outcome": "PASS",
        "review": {
            "reviewer_id": "reviewer_test_001",
            "verified_at": "2026-11-21T00:00:00Z",
            "evidence_url": f"https://github.com/{REPO}/pull/{pair_no + (100 if condition == 'ATLAS' else 0)}",
        } if review else None,
        "preparation_seconds": 120 if condition == "CONTROL" else 80,
        "context_bytes": 10000 if condition == "CONTROL" else 7500,
        "reread_count": 3 if condition == "CONTROL" else 1,
        "owner_reexplanations": 1 if condition == "CONTROL" else 0,
        "stale_reference_count": 0,
        "first_pass_defects": 0,
        "used_atlas_reference": condition == "ATLAS",
        "public_metadata_only": True,
    }


def study(n=20, *, reviewed=True):
    return {
        "schema_version": 1,
        "study_id": "synthetic_task_value_tests",
        "scope": "METADATA_ONLY",
        "observations": [
            record(i, cond, review=reviewed)
            for i in range(1, n + 1)
            for cond in ("CONTROL", "ATLAS")
        ],
    }


class TaskValueBenchmarkTests(unittest.TestCase):
    def test_no_data_never_claims_benefit(self):
        s = study(0)
        result = evaluate(s)
        self.assertEqual(result["state"], "NO_DATA")
        self.assertEqual(result["productivity_benefit"], "NOT_ESTABLISHED")
        self.assertIsNone(result["preparation_reduction_percent"])

    def test_small_real_cohort_is_insufficient(self):
        r = evaluate(study(3))
        self.assertEqual((r["observed_pairs"], r["state"]), (3, "INSUFFICIENT"))
        self.assertIsNone(r["declared_candidate_threshold_met"])

    def test_20_complete_pairs_are_declared_not_attested(self):
        r = evaluate(study(20))
        self.assertEqual(r["state"], "COMPLETE_DECLARED_NOT_ATTESTED")
        self.assertEqual(r["preparation_reduction_percent"], 33.33)
        self.assertEqual(r["context_reduction_percent"], 25.0)
        self.assertTrue(r["declared_quality_noninferiority"])
        self.assertTrue(r["declared_candidate_threshold_met"])
        self.assertEqual(r["productivity_benefit"], "NOT_ESTABLISHED")
        self.assertEqual(r["independent_review_attestation"], "NOT_ATTESTED")
        self.assertNotIn("reviewer_test_001", json.dumps(r))
        self.assertNotIn("/pull/", json.dumps(r))

    def test_missing_review_never_reports_savings(self):
        s = study()
        s["observations"][0]["review"] = None
        r = evaluate(s)
        self.assertEqual(r["state"], "INCOMPLETE")
        self.assertIsNone(r["preparation_reduction_percent"])

    def test_missing_arm_and_duplicate_pair_reject_or_incomplete(self):
        s = study()
        s["observations"].pop()
        self.assertEqual(evaluate(s)["state"], "INCOMPLETE")
        s = study()
        s["observations"].append(copy.deepcopy(s["observations"][0]))
        with self.assertRaisesRegex(TaskValueError, "duplicate task"):
            evaluate(s)

    def test_mismatched_head_or_repo_is_not_comparable(self):
        for field, wrong in (("start_head", "b" * 40),
                             ("repository", "datarelay-labs/datarelay-link"),
                             ("work_class", "FEATURE")):
            s = study()
            s["observations"][1][field] = wrong
            if field == "repository":
                s["observations"][1]["review"]["evidence_url"] = (
                    "https://github.com/datarelay-labs/datarelay-link/pull/101"
                )
            with self.subTest(field=field), self.assertRaisesRegex(
                TaskValueError, "pair identity mismatch"
            ):
                evaluate(s)

    def test_control_cannot_claim_atlas_usage(self):
        s = study()
        s["observations"][0]["used_atlas_reference"] = True
        with self.assertRaisesRegex(TaskValueError, "control condition"):
            evaluate(s)

    def test_outcome_guard_and_quality_regression_deny_candidate(self):
        s = study()
        s["observations"][1]["verified_outcome"] = "FAIL"
        r = evaluate(s)
        self.assertFalse(r["declared_quality_noninferiority"])
        self.assertFalse(r["declared_candidate_threshold_met"])
        s["observations"][1]["task_completed"] = False
        s["observations"][1]["verified_outcome"] = "PASS"
        with self.assertRaisesRegex(TaskValueError, "incomplete task"):
            evaluate(s)

    def test_stale_reference_regression_denies_candidate(self):
        s = study()
        s["observations"][1]["stale_reference_count"] = 1
        r = evaluate(s)
        self.assertFalse(r["declared_candidate_threshold_met"])

    def test_provenance_url_must_be_exact_matching_repository(self):
        for uri in (
            "https://github.com/datarelay-labs/other/pull/1",
            "https://github.com.evil.net/datarelay-labs/datarelay-atlas/pull/1",
            "http://github.com/datarelay-labs/datarelay-atlas/pull/1",
            "https://github.com/datarelay-labs/datarelay-atlas/pull/1?token=abc",
        ):
            s = study()
            s["observations"][0]["review"]["evidence_url"] = uri
            with self.subTest(uri=uri), self.assertRaises(TaskValueError):
                evaluate(s)

    def test_sensitive_payload_and_invalid_numerics_rejected(self):
        for field, value in (
            ("context_bytes", -1), ("preparation_seconds", True),
            ("reread_count", 1001), ("public_metadata_only", "yes"),
            ("pair_id", "invalid id"), ("verified_outcome", []),
        ):
            s = study()
            s["observations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(TaskValueError):
                evaluate(s)
        s = study()
        s["observations"][0]["raw_prompt"] = "private"
        with self.assertRaises(TaskValueError):
            evaluate(s)

    def test_nominal_atlas_condition_without_any_atlas_use_is_not_eligible(self):
        s = study()
        for r in s["observations"]:
            r["used_atlas_reference"] = False
        result = evaluate(s)
        self.assertFalse(result["declared_candidate_threshold_met"])

    def test_hash_binds_exact_metadata_and_no_url_leak(self):
        s = study()
        a = evaluate(s)
        s["observations"][0]["reread_count"] += 1
        b = evaluate(s)
        self.assertNotEqual(a["study_sha256"], b["study_sha256"])
        self.assertNotIn("github.com", json.dumps(b))

    def test_loader_rejects_duplicate_json_keys_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "study.json"
            p.write_text('{"scope":"METADATA_ONLY","scope":"METADATA_ONLY"}')
            with self.assertRaises(TaskValueError):
                load_study(str(p))
            p.write_bytes(b" " * (1024 * 1024 + 1))
            with self.assertRaisesRegex(TaskValueError, "too large"):
                load_study(str(p))

    def test_cli_empty_and_real_file(self):
        cli = [sys.executable, str(ROOT / "scripts" / "atlas_task_value_eval.py")]
        p = subprocess.run(cli, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(p.stdout)["state"], "NO_DATA")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "study.json"
            f.write_text(json.dumps(study(3)))
            p = subprocess.run(cli + ["--study", str(f)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout)["state"], "INSUFFICIENT")
            f.write_text("{oops}")
            p = subprocess.run(cli + ["--study", str(f)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
