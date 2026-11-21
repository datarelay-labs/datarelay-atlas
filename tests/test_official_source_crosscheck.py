"""Offline source crosscheck security + factual-claim boundaries."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from atlas.official_source_crosscheck import (
    CrosscheckError, crosscheck, load_file,
)
from atlas.public_source_benchmark import read_json_file

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = read_json_file(str(ROOT / "evals/public-sources/questions-v1.json"))
FIRST = MANIFEST["questions"][0]


def sample(provider, source_url, assessment="SUPPORTS", *, reviewed=True, digest="a" * 64):
    return {
        "question_id": FIRST["id"],
        "provider": provider,
        "source_url": source_url,
        "retrieved_at": "2026-11-21T10:00:00Z",
        "source_sha256": digest,
        "assessment": assessment if reviewed else "UNVERIFIED",
        "review": {
            "reviewer_id": "offline_review_01",
            "reviewed_at": "2026-11-21T10:00:01Z",
        } if reviewed else None,
        "public_only": True,
    }


def corpus(*observations):
    return {
        "schema_version": 1, "dataset_id": MANIFEST["dataset_id"],
        "scope": "PUBLIC_ONLY", "observations": list(observations),
    }


class OfficialSourceCrosscheckTests(unittest.TestCase):
    def test_zero_data_never_claims_verified_source(self):
        result = crosscheck(MANIFEST, corpus())
        self.assertEqual(result["observations"], 0)
        self.assertEqual(result["statuses"]["NO_SOURCES"], 30)
        self.assertEqual(result["independent_content_attestation"], "NOT_ATTESTED")
        self.assertIs(result["canonical_knowledge_written"], False)

    def test_two_distinct_official_citations_are_declared_not_attested(self):
        records = corpus(
            sample("baseline", "https://cisa.gov/known-exploited-vulnerabilities-catalog"),
            sample("parallel_mcp", "https://cisa.gov/resources-tools/resources"),
        )
        report = crosscheck(MANIFEST, records)
        first = report["results"][0]
        self.assertEqual(first["status"], "DECLARED_CORROBORATION")
        self.assertEqual(first["supporting_providers"], 2)
        self.assertEqual(first["supporting_sources"], 2)
        self.assertEqual(len(first["citations"]), 2)
        self.assertEqual(report["independent_content_attestation"], "NOT_ATTESTED")
        self.assertEqual(report["trust_boundary"], "UNTRUSTED_EXTERNAL_CONTENT")

    def test_one_official_citation_is_not_adequate(self):
        r = crosscheck(MANIFEST, corpus(sample("baseline", "https://cisa.gov/source")))
        self.assertEqual(r["results"][0]["status"], "INSUFFICIENT_CORROBORATION")

    def test_same_url_across_providers_is_not_independent_source(self):
        r = crosscheck(MANIFEST, corpus(
            sample("baseline", "https://cisa.gov/source", digest="a" * 64),
            sample("parallel_mcp", "https://cisa.gov/source", digest="b" * 64),
        ))
        self.assertEqual(r["results"][0]["supporting_sources"], 1)
        self.assertEqual(r["results"][0]["status"], "INSUFFICIENT_CORROBORATION")

    def test_two_sources_one_provider_cannot_promote(self):
        r = crosscheck(MANIFEST, corpus(
            sample("baseline", "https://cisa.gov/source1"),
            sample("baseline", "https://cisa.gov/source2"),
        ))
        self.assertEqual(r["results"][0]["status"], "INSUFFICIENT_CORROBORATION")

    def test_contradiction_overrides_support_even_if_two_support(self):
        r = crosscheck(MANIFEST, corpus(
            sample("baseline", "https://cisa.gov/source1"),
            sample("parallel_mcp", "https://cisa.gov/source2"),
            sample("parallel_mcp", "https://cisa.gov/source3", "CONTRADICTS"),
        ))
        self.assertEqual(r["results"][0]["status"], "CONFLICT_REVIEW_REQUIRED")
        self.assertEqual(r["results"][0]["contradiction_count"], 1)

    def test_unreviewed_and_review_inconsistency_fail_closed(self):
        unreviewed = sample("baseline", "https://cisa.gov/source", reviewed=False)
        r = crosscheck(MANIFEST, corpus(unreviewed))
        self.assertEqual(r["results"][0]["status"], "INSUFFICIENT_CORROBORATION")
        unreviewed["assessment"] = "SUPPORTS"
        with self.assertRaises(CrosscheckError):
            crosscheck(MANIFEST, corpus(unreviewed))
        reviewed = sample("baseline", "https://cisa.gov/source")
        reviewed["assessment"] = "UNVERIFIED"
        with self.assertRaises(CrosscheckError):
            crosscheck(MANIFEST, corpus(reviewed))

    def test_false_authority_and_foreign_domain_are_rejected(self):
        for source in (
            "http://cisa.gov/source",
            "https://cisa.gov.evil.test/source",
            "https://token@cisa.gov/source",
            "https://127.0.0.1/source",
            "https://cisa.gov/path?token=x",
            "https://cisa.gov:8443/source",
            "https://cisa.gov/%2fprivate",
        ):
            with self.subTest(source=source):
                with self.assertRaises(CrosscheckError):
                    crosscheck(MANIFEST, corpus(sample("baseline", source)))

    def test_privacy_and_invalid_identity_rejected(self):
        value = sample("baseline", "https://cisa.gov/source")
        for key, item in (
            ("public_only", False), ("source_sha256", "a" * 63),
            ("question_id", "not_a_question"), ("provider", "fake"),
            ("assessment", []), ("retrieved_at", "tomorrow"),
        ):
            v = copy.deepcopy(value)
            v[key] = item
            with self.subTest(key=key), self.assertRaises(CrosscheckError):
                crosscheck(MANIFEST, corpus(v))

    def test_duplicate_citation_and_schema_mismatch_rejected(self):
        row = sample("baseline", "https://cisa.gov/source")
        with self.assertRaisesRegex(CrosscheckError, "duplicate research source"):
            crosscheck(MANIFEST, corpus(row, row))
        evidence = corpus(row)
        evidence["scope"] = "INTERNAL"
        with self.assertRaises(CrosscheckError):
            crosscheck(MANIFEST, evidence)

    def test_review_timestamp_cannot_predate_retrieval(self):
        row = sample("baseline", "https://cisa.gov/source")
        row["review"]["reviewed_at"] = "2026-11-21T09:59:59Z"
        with self.assertRaisesRegex(CrosscheckError, "predate"):
            crosscheck(MANIFEST, corpus(row))

    def test_response_digest_changes_when_evidence_changes(self):
        original = corpus(sample("baseline", "https://cisa.gov/source"))
        before = crosscheck(MANIFEST, original)["evidence_sha256"]
        original["observations"][0]["source_sha256"] = "b" * 64
        after = crosscheck(MANIFEST, original)["evidence_sha256"]
        self.assertNotEqual(before, after)

    def test_input_read_limit_and_no_duplicate_object_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text('{"scope":"PUBLIC_ONLY","scope":"PUBLIC_ONLY"}')
            with self.assertRaisesRegex(CrosscheckError, "invalid public crosscheck JSON"):
                load_file(str(path))
            path.write_bytes(b"x" * 1_048_577)
            with self.assertRaisesRegex(CrosscheckError, "exceeds"):
                load_file(str(path))

    def test_cli_defaults_no_network_and_errors_do_not_echo_payload(self):
        cli = [sys.executable, str(ROOT / "scripts/atlas_official_source_crosscheck.py")]
        r = subprocess.run(cli, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["statuses"]["NO_SOURCES"], 30)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            path.write_text(json.dumps(corpus(
                sample("baseline", "https://cisa.gov/source")
            )))
            r = subprocess.run(cli + ["--evidence", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(json.loads(r.stdout)["results"][0]["status"], "INSUFFICIENT_CORROBORATION")
            path.write_text("{SECRETPAYLOAD")
            r = subprocess.run(cli + ["--evidence", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertNotIn("SECRETPAYLOAD", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
