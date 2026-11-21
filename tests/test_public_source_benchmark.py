"""Offline official-source evaluation regression; observations are synthetic test data."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from atlas.public_source_benchmark import (
    BenchmarkError, evaluate, manifest_questions, read_json_file, read_observations,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evals" / "public-sources" / "questions-v1.json"


def row(question, provider, *, reviewed=True):
    host = question["official_hosts"][0]
    return {
        "question_id": question["id"], "provider": provider,
        "status": "SUCCESS", "accessed_at": "2026-11-21T09:00:00Z",
        "latency_ms": 100 if provider == "baseline" else 70,
        "cost_usd": "0.003" if provider == "baseline" else "0.001",
        "result_url": "https://" + host + "/official-page",
        "verdict": "AGREES" if reviewed else "UNVERIFIED",
        "review": {
            "reviewer_id": "independent-review-01",
            "reviewed_at": "2026-11-21T09:05:00Z",
            "primary_url": "https://" + host + "/official-page"
        } if reviewed else None,
        "fallback_used": False, "public_only": True,
    }


class PublicSourceBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = read_json_file(str(MANIFEST))
        cls.questions = cls.manifest["questions"]

    def full(self):
        return [row(q, p) for p in self.manifest["providers"] for q in self.questions]

    def test_manifest_is_public_and_exactly_30(self):
        self.assertEqual(len(manifest_questions(self.manifest)), 30)
        self.assertEqual(self.manifest["providers"], ["baseline", "parallel_mcp"])

    def test_no_data_never_passes(self):
        result = evaluate(self.manifest, [])
        self.assertEqual(result["state"], "NO_DATA")
        self.assertIsNone(result["providers"]["baseline"]["declared_agreement_rate"])
        self.assertEqual(result["benchmark_pass"], "NOT_ESTABLISHED")

    def test_incomplete_does_not_fabricate_effectiveness(self):
        r = evaluate(self.manifest, [row(self.questions[0], "baseline")])
        self.assertEqual(r["state"], "INCOMPLETE")
        self.assertIsNone(r["providers"]["baseline"]["reported_cost_usd"])
        self.assertIsNone(r["providers"]["baseline"]["median_latency_ms"])

    def test_full_synthetic_is_declared_only(self):
        r = evaluate(self.manifest, self.full())
        self.assertEqual((r["state"], r["observed"]), ("COMPLETE_REVIEW_DECLARED", 60))
        self.assertEqual(r["providers"]["baseline"]["median_latency_ms"], 100)
        self.assertEqual(r["providers"]["parallel_mcp"]["median_latency_ms"], 70)
        self.assertEqual(r["providers"]["baseline"]["reported_cost_usd"], "0.090")
        self.assertEqual(r["providers"]["parallel_mcp"]["reported_cost_usd"], "0.030")
        self.assertEqual(r["primary_source_agreement_attestation"], "NOT_ATTESTED")
        self.assertEqual(r["benchmark_pass"], "NOT_ESTABLISHED")
        self.assertNotIn("official-page", json.dumps(r))

    def test_partial_review_prevents_declared_rate(self):
        records = self.full()
        records[0] = row(self.questions[0], "baseline", reviewed=False)
        r = evaluate(self.manifest, records)
        self.assertEqual(r["state"], "COMPLETE_UNVERIFIED")
        self.assertEqual(r["providers"]["baseline"]["unverified"], 1)
        self.assertIsNone(r["providers"]["parallel_mcp"]["declared_agreement_rate"])

    def test_nonofficial_url_counted_not_promoted(self):
        records = self.full()
        records[0]["result_url"] = "https://cisa.gov.evil.org/unverified"
        r = evaluate(self.manifest, records)
        self.assertEqual(r["providers"]["baseline"]["official_source_hits"], 29)

    def test_rate_limit_and_omission(self):
        records = self.full()
        records[0].update({
            "status": "RATE_LIMIT", "result_url": None,
            "verdict": "OMITTED", "fallback_used": True,
        })
        r = evaluate(self.manifest, records)["providers"]["baseline"]
        self.assertEqual((r["successes"], r["rate_limits"], r["fallback_count"]), (29, 1, 1))
        self.assertEqual(r["declared_omissions"], 1)
        self.assertEqual(r["declared_agreement_rate"], 0.9667)

    def test_duplicate_and_unknown_identity_rejected(self):
        records = self.full()
        with self.assertRaisesRegex(BenchmarkError, "duplicate"):
            evaluate(self.manifest, records + [records[0]])
        records[0]["question_id"] = "wrong-id"
        with self.assertRaisesRegex(BenchmarkError, "unknown question"):
            evaluate(self.manifest, records)

    def test_public_only_and_private_prompt_gate(self):
        records = self.full()
        records[0]["public_only"] = False
        with self.assertRaises(BenchmarkError):
            evaluate(self.manifest, records)
        bad = copy.deepcopy(self.manifest)
        bad["questions"][0]["question"] = "Can you use password=private-value to reach the requested page?"
        with self.assertRaises(BenchmarkError):
            manifest_questions(bad)

    def test_bad_source_urls_are_rejected(self):
        urls = (
            "http://cisa.gov/foo",
            "https://token@cisa.gov/foo", "https://cisa.gov/foo?token=private",
            "https://127.0.0.1/foo", "https://localhost/foo",
            "https://cisa.gov:8443/foo", "https://cisa.gov/foo#fragment",
            "https://cisa.gov/%2fetc",
        )
        for url in urls:
            with self.subTest(url=url):
                records = self.full()
                records[0]["result_url"] = url
                with self.assertRaises(BenchmarkError):
                    evaluate(self.manifest, records)

    def test_primary_source_reviewer_host_is_exact(self):
        records = self.full()
        records[0]["review"]["primary_url"] = "https://example.org/nonofficial"
        with self.assertRaisesRegex(BenchmarkError, "official host"):
            evaluate(self.manifest, records)

    def test_failed_request_cannot_claim_agreement_or_url(self):
        for verdict, url in (("AGREES", None), ("OMITTED", "https://cisa.gov/path")):
            records = self.full()
            records[0].update({"status": "ERROR", "verdict": verdict, "result_url": url})
            with self.assertRaises(BenchmarkError):
                evaluate(self.manifest, records)

    def test_invalid_structures_and_types_fail_closed(self):
        for change in (
            {"questions": []}, {"providers": ["baseline", "baseline"]},
            {"scope": "INTERNAL"}, {"schema_version": 2}
        ):
            data = copy.deepcopy(self.manifest)
            data.update(change)
            with self.assertRaises(BenchmarkError):
                manifest_questions(data)
        for field, invalid in (
            ("status", []), ("verdict", {}),
            ("latency_ms", True), ("cost_usd", "NaN"),
            ("public_only", "true"), ("accessed_at", "2026-22-21T09:00:00Z")
        ):
            with self.subTest(field=field):
                rec = self.full()[0]
                rec[field] = invalid
                with self.assertRaises(BenchmarkError):
                    evaluate(self.manifest, [rec])

    def test_digest_binds_exact_dataset_and_observations_without_contents(self):
        records = self.full()
        first = evaluate(self.manifest, records)
        reversed_order = evaluate(self.manifest, list(reversed(records)))
        self.assertEqual(first["manifest_sha256"], reversed_order["manifest_sha256"])
        self.assertEqual(first["observation_set_sha256"], reversed_order["observation_set_sha256"])
        modified = copy.deepcopy(records)
        modified[0]["latency_ms"] = 101
        self.assertNotEqual(
            first["observation_set_sha256"],
            evaluate(self.manifest, modified)["observation_set_sha256"]
        )
        changed_manifest = copy.deepcopy(self.manifest)
        changed_manifest["questions"][0]["question"] += " Explain the scope."
        self.assertNotEqual(
            first["manifest_sha256"],
            evaluate(changed_manifest, [])["manifest_sha256"]
        )

    def test_jsonl_and_cli_no_data(self):
        cmd = [sys.executable, str(ROOT / "scripts" / "atlas_official_source_eval.py")]
        p = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(p.stdout)["state"], "NO_DATA")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "evidence.jsonl"
            f.write_text(json.dumps(self.full()[0]) + "\n")
            p = subprocess.run(cmd + ["--observations", str(f)],
                               capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(p.stdout)["state"], "INCOMPLETE")
            f.write_text("{broken}\n")
            with self.assertRaises(BenchmarkError):
                read_observations(str(f))
            p = subprocess.run(cmd + ["--observations", str(f)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
