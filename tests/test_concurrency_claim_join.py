from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from atlas.concurrency_claim import claim_concurrency_handoff
from atlas.concurrency_claim_join import (
    FILENAME,
    concurrency_claim_bound_join_dashboard,
    get_concurrency_claim_bound_join,
    record_concurrency_claim_bound_join,
    validate_concurrency_claim_bound_join_evidence,
    validate_concurrency_claim_bound_join_observation,
)
from atlas.concurrency_join import record_concurrency_dispatch_join
from atlas.data_protection import backup_data_root
from atlas.provenance import ValidationError
from tests.test_concurrency_claim import _request, _source
from tests.test_concurrency_join import _join_observation

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"

def _prepare(base: Path, *, outcome: str = "COMPLETE"):
    data, adapter, _bindings, runner, effect, handoffs = _source(base)
    claims = []
    for index, handoff in enumerate(handoffs, start=1):
        claims.append(
            claim_concurrency_handoff(
                data,
                _request(handoff, f"claim-join-source-{index}"),
                packet_adapter=adapter,
                git_runner=runner,
            )
        )
    join_dashboard = record_concurrency_dispatch_join(
        data,
        _join_observation(
            effect,
            join_id="claim-bound-source-join",
            outcome=outcome,
        ),
    )
    return data, effect, claims, join_dashboard["latest_join"]


def _observation(
    join: dict,
    claims: list[dict],
    *,
    claim_join_id: str = "claim-join-1",
):
    return {
        "schema_version": 1,
        "kind": "concurrency_claim_bound_join_observation",
        "claim_join_id": claim_join_id,
        "dispatch_join_digest": join["join_digest"],
        "claims": [
            {
                "node_id": claim["node_id"],
                "claim_digest": claim["claim_digest"],
            }
            for claim in claims
        ],
    }


def _resign(evidence: dict) -> None:
    body = {
        key: value
        for key, value in evidence.items()
        if key != "evidence_digest"
    }
    evidence["evidence_digest"] = hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class ConcurrencyClaimBoundJoinTests(unittest.TestCase):
    def test_public_schemas_fixtures_and_runtime_parity(self) -> None:
        observation_schema = json.loads(
            (
                CONTRACTS
                / "concurrency-claim-bound-join-observation.schema.json"
            ).read_text()
        )
        evidence_schema = json.loads(
            (
                CONTRACTS
                / "concurrency-claim-bound-join-evidence.schema.json"
            ).read_text()
        )
        observation_fixture = json.loads(
            (
                FIXTURES
                / "concurrency-claim-bound-join-observation.example.json"
            ).read_text()
        )
        evidence_fixture = json.loads(
            (
                FIXTURES
                / "concurrency-claim-bound-join-evidence.example.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(observation_schema)
        Draft202012Validator.check_schema(evidence_schema)
        Draft202012Validator(observation_schema).validate(observation_fixture)
        Draft202012Validator(evidence_schema).validate(evidence_fixture)
        self.assertEqual(
            validate_concurrency_claim_bound_join_observation(
                observation_fixture
            ),
            observation_fixture,
        )
        self.assertEqual(
            validate_concurrency_claim_bound_join_evidence(
                evidence_fixture
            ),
            evidence_fixture,
        )

    def test_boolean_counts_and_invalid_head_fail_closed(self) -> None:
        evidence_fixture = json.loads(
            (
                FIXTURES
                / "concurrency-claim-bound-join-evidence.example.json"
            ).read_text()
        )
        evidence_fixture["complete_count"] = True
        with self.assertRaises(ValidationError):
            validate_concurrency_claim_bound_join_evidence(
                evidence_fixture
            )

        evidence_fixture = json.loads(
            (
                FIXTURES
                / "concurrency-claim-bound-join-evidence.example.json"
            ).read_text()
        )
        evidence_fixture["bindings"][0]["head"] = "not-a-head"
        with self.assertRaises(ValidationError):
            validate_concurrency_claim_bound_join_evidence(
                evidence_fixture
            )

    def test_two_claimed_assignments_record_measurement_only_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, effect, claims, join = _prepare(Path(tmp))
            dashboard = record_concurrency_claim_bound_join(
                data,
                _observation(join, claims),
            )
            evidence = dashboard["latest_join"]
            self.assertEqual(evidence["result"], "PASS")
            self.assertEqual(evidence["effect_id"], effect["effect_id"])
            self.assertEqual(
                evidence["dispatch_join_digest"],
                join["join_digest"],
            )
            self.assertEqual(evidence["dispatched_count"], 2)
            self.assertEqual(len(evidence["bindings"]), 2)
            self.assertEqual(evidence["pass_authority"], "MEASUREMENT_ONLY")
            self.assertEqual(evidence["completion_authority"], "NONE")
            self.assertEqual(evidence["release_authority"], "NONE")
            self.assertEqual(evidence["merge_authority"], "NONE")
            self.assertEqual(evidence["deploy_authority"], "NONE")
            self.assertEqual(
                {row["claim_digest"] for row in evidence["bindings"]},
                {claim["claim_digest"] for claim in claims},
            )
            self.assertEqual(
                get_concurrency_claim_bound_join(
                    data,
                    evidence["evidence_digest"],
                ),
                evidence,
            )

    def test_missing_wrong_or_reused_claim_fails_before_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, _effect, claims, join = _prepare(Path(tmp))

            missing = _observation(
                join,
                claims[:1],
                claim_join_id="missing",
            )
            with self.assertRaisesRegex(ValidationError, "one claim"):
                record_concurrency_claim_bound_join(data, missing)
            self.assertFalse((data / FILENAME).exists())

            wrong = _observation(
                join,
                list(reversed(claims)),
                claim_join_id="wrong",
            )
            for row, claim in zip(wrong["claims"], claims):
                row["node_id"] = claim["node_id"]
            with self.assertRaisesRegex(ValidationError, "does not match"):
                record_concurrency_claim_bound_join(data, wrong)
            self.assertFalse((data / FILENAME).exists())

            reused = _observation(
                join,
                claims,
                claim_join_id="reused",
            )
            reused["claims"][1]["claim_digest"] = (
                reused["claims"][0]["claim_digest"]
            )
            with self.assertRaisesRegex(ValidationError, "duplicated"):
                record_concurrency_claim_bound_join(data, reused)
            self.assertFalse((data / FILENAME).exists())

    def test_source_join_and_claims_cannot_be_replayed_under_new_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, _effect, claims, join = _prepare(Path(tmp))
            record_concurrency_claim_bound_join(
                data,
                _observation(join, claims, claim_join_id="first-binding"),
            )
            with self.assertRaisesRegex(ValidationError, "replay"):
                record_concurrency_claim_bound_join(
                    data,
                    _observation(
                        join,
                        claims,
                        claim_join_id="second-binding",
                    ),
                )

    def test_claim_attribution_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, _effect, claims, join = _prepare(Path(tmp))
            observation = _observation(join, claims)
            observation["claims"][0]["node_id"] = claims[1]["node_id"]
            observation["claims"][1]["node_id"] = claims[0]["node_id"]
            with self.assertRaisesRegex(ValidationError, "does not match"):
                record_concurrency_claim_bound_join(
                    data,
                    observation,
                )
            self.assertFalse((data / FILENAME).exists())

    def test_resigned_stored_attribution_drift_fails_against_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, _effect, claims, join = _prepare(Path(tmp))
            dashboard = record_concurrency_claim_bound_join(
                data,
                _observation(join, claims),
            )
            self.assertEqual(dashboard["join_count"], 1)
            ledger = json.loads((data / FILENAME).read_text())
            ledger["joins"][0]["bindings"][0]["worker_id"] = (
                "forged-worker"
            )
            _resign(ledger["joins"][0])
            (data / FILENAME).write_text(
                json.dumps(ledger),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError,
                "stored attribution",
            ):
                concurrency_claim_bound_join_dashboard(data)

    def test_source_join_or_claim_tamper_invalidates_existing_evidence(self) -> None:
        for source in ("join", "claim"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                data, _effect, claims, join = _prepare(Path(tmp))
                record_concurrency_claim_bound_join(
                    data,
                    _observation(join, claims),
                )
                if source == "join":
                    source_path = data / "concurrency-dispatch-joins.json"
                    ledger = json.loads(source_path.read_text())
                    ledger["joins"][0]["outcomes"][0]["worker_id"] = "tampered"
                    source_path.write_text(
                        json.dumps(ledger),
                        encoding="utf-8",
                    )
                else:
                    source_path = data / "concurrency-handoff-claims.json"
                    ledger = json.loads(source_path.read_text())
                    ledger["claims"][0]["receipt"]["worker_id"] = "tampered"
                    source_path.write_text(
                        json.dumps(ledger),
                        encoding="utf-8",
                    )
                with self.assertRaises(ValidationError):
                    concurrency_claim_bound_join_dashboard(data)

    def test_non_pass_join_preserves_measurement_result_without_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, _effect, claims, join = _prepare(
                Path(tmp),
                outcome="FAILED",
            )
            dashboard = record_concurrency_claim_bound_join(
                data,
                _observation(join, claims),
            )
            evidence = dashboard["latest_join"]
            self.assertEqual(evidence["result"], "FAILED")
            self.assertEqual(evidence["failed_count"], 2)
            self.assertEqual(evidence["completion_authority"], "NONE")
            self.assertEqual(evidence["release_authority"], "NONE")

    def test_claim_bound_join_ledger_is_excluded_from_backup_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            (data / "registry.json").write_text(
                '{"schema_version":1,"projects":{}}\n',
                encoding="utf-8",
            )
            (data / FILENAME).write_text(
                (
                    '{"schema_version":1,'
                    '"kind":"concurrency_claim_bound_join_ledger",'
                    '"authority":"MEASUREMENT_ONLY","joins":[]}\n'
                ),
                encoding="utf-8",
            )
            dest = base / "backup"
            result = backup_data_root(data, dest)
            self.assertNotIn(FILENAME, json.dumps(result))
            self.assertFalse((dest / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
