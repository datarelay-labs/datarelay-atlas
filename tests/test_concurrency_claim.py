from __future__ import annotations

import json
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from jsonschema import Draft202012Validator

from atlas.concurrency_claim import (
    FILENAME as CLAIM_FILENAME,
    claim_concurrency_handoff,
    get_concurrency_handoff_claim,
    validate_concurrency_handoff_claim_receipt,
    validate_concurrency_handoff_claim_request,
)
from atlas.concurrency_effect import (
    FILENAME as EFFECT_FILENAME,
    commit_concurrency_dispatch_effect,
)
from atlas.concurrency_handoff import (
    FILENAME as HANDOFF_FILENAME,
    ConcurrencyWorkPacketHandoffPort,
    get_concurrency_handoff_authorization,
)
from atlas.data_protection import backup_data_root
from atlas.provenance import ValidationError
from tests.test_concurrency_handoff import (
    MemoryPacketAdapter,
    _git_runner,
    _prepare,
    _registry,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _source(base: Path):
    data = base / "data"
    data.mkdir()
    auth = _prepare(data)
    registry, bindings = _registry(base, auth)
    adapter = MemoryPacketAdapter(
        auth["assignments"],
        {node: row["workstream"] for node, row in registry.items()},
    )
    runner = _git_runner(bindings)
    port = ConcurrencyWorkPacketHandoffPort(
        data_root=data,
        packet_adapter=adapter,
        target_registry=registry,
        git_runner=runner,
    )
    effect = commit_concurrency_dispatch_effect(
        data,
        effect_id="claim-source-effect",
        expected_authorization_digest=auth["authorization_digest"],
        effect_port=port,
    )
    handoffs = [
        get_concurrency_handoff_authorization(data, item["dispatch_ref"])
        for item in effect["receipts"]
    ]
    return data, adapter, bindings, runner, effect, handoffs


def _request(handoff: dict, claim_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "concurrency_handoff_claim_request",
        "claim_id": claim_id,
        "handoff_digest": handoff["handoff_digest"],
        "effect_id": handoff["effect_id"],
        "node_id": handoff["node_id"],
        "repository": handoff["repository"],
        "issue_number": handoff["issue_number"],
        "branch": handoff["branch"],
        "head": handoff["head"],
        "worktree_path": handoff["worktree_path"],
        "adapter_id": handoff["adapter_id"],
        "provider": handoff["provider"],
        "runtime": handoff["runtime"],
        "route_id": handoff["route_id"],
    }


class ConcurrencyClaimTests(unittest.TestCase):
    def test_boolean_ledger_schema_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {
                "schema_version": True,
                "kind": "concurrency_handoff_claim_ledger",
                "claims": [],
            }
            (root / CLAIM_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "schema"):
                get_concurrency_handoff_claim(root, "0" * 64)



    def test_dangling_claim_ledger_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / CLAIM_FILENAME).symlink_to(data / "missing-claim-ledger.json")
            with self.assertRaisesRegex(ValidationError, "ledger path is unsafe"):
                get_concurrency_handoff_claim(data, "0" * 64)

    def test_duplicate_claim_ledger_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            raw = (
                '{"schema_version":1,"kind":"concurrency_handoff_claim_ledger",'
                '"claims":[],"claims":[]}'
            )
            (data / CLAIM_FILENAME).write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON keys"):
                get_concurrency_handoff_claim(data, "0" * 64)

    def setUp(self) -> None:
        patcher = patch(
            "atlas.concurrency_effect._trusted_effect_time",
            return_value=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_request_receipt_schema_fixtures_and_runtime_parity(self) -> None:
        request_schema = json.loads(
            (CONTRACTS / "concurrency-handoff-claim-request.schema.json").read_text()
        )
        receipt_schema = json.loads(
            (CONTRACTS / "concurrency-handoff-claim-receipt.schema.json").read_text()
        )
        request_fixture = json.loads(
            (FIXTURES / "concurrency-handoff-claim-request.example.json").read_text()
        )
        receipt_fixture = json.loads(
            (FIXTURES / "concurrency-handoff-claim-receipt.example.json").read_text()
        )
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(receipt_schema)
        Draft202012Validator(request_schema).validate(request_fixture)
        Draft202012Validator(receipt_schema).validate(receipt_fixture)
        self.assertEqual(
            validate_concurrency_handoff_claim_request(request_fixture),
            request_fixture,
        )
        self.assertEqual(
            validate_concurrency_handoff_claim_receipt(receipt_fixture),
            receipt_fixture,
        )

    def test_boolean_schema_version_and_non_digest_replay_key_fail_closed(self) -> None:
        request_fixture = json.loads(
            (FIXTURES / "concurrency-handoff-claim-request.example.json").read_text()
        )
        request_fixture["schema_version"] = True
        with self.assertRaises(ValidationError):
            validate_concurrency_handoff_claim_request(request_fixture)

        receipt_fixture = json.loads(
            (FIXTURES / "concurrency-handoff-claim-receipt.example.json").read_text()
        )
        receipt_fixture["schema_version"] = True
        with self.assertRaises(ValidationError):
            validate_concurrency_handoff_claim_receipt(receipt_fixture)

        receipt_fixture = json.loads(
            (FIXTURES / "concurrency-handoff-claim-receipt.example.json").read_text()
        )
        receipt_fixture["replay_key"] = "not-a-digest"
        with self.assertRaises(ValidationError):
            validate_concurrency_handoff_claim_receipt(receipt_fixture)

    def test_two_distinct_handoffs_claim_once_with_distinct_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, adapter, _bindings, runner, _effect, handoffs = _source(Path(tmp))
            claims = []
            for index, handoff in enumerate(handoffs, start=1):
                claim = claim_concurrency_handoff(
                    data,
                    _request(handoff, f"claim-{index}"),
                    packet_adapter=adapter,
                    git_runner=runner,
                )
                claims.append(claim)
                self.assertEqual(claim["result"], "CLAIMED")
                self.assertEqual(claim["handoff_digest"], handoff["handoff_digest"])
                self.assertEqual(claim["effect_id"], handoff["effect_id"])
                self.assertEqual(claim["node_id"], handoff["node_id"])
                self.assertEqual(claim["worktree_path"], handoff["worktree_path"])
                self.assertEqual(claim["adapter_id"], "CHATGPT_EXTERNAL_HANDOFF")
                self.assertEqual(claim["provider"], "openai")
                self.assertEqual(claim["runtime"], "chat_ssh")
                self.assertFalse(claim["spawned"])
                self.assertEqual(claim["completion_authority"], "NONE")
                self.assertEqual(claim["pass_authority"], "NONE")
                self.assertEqual(claim["release_authority"], "NONE")
            self.assertEqual(
                len({item["claim_digest"] for item in claims}),
                2,
            )
            for claim in claims:
                self.assertEqual(
                    get_concurrency_handoff_claim(data, claim["claim_digest"]),
                    claim,
                )

    def test_duplicate_handoff_or_conflicting_claim_id_is_replay_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, adapter, _bindings, runner, _effect, handoffs = _source(Path(tmp))
            first = claim_concurrency_handoff(
                data,
                _request(handoffs[0], "claim-replay"),
                packet_adapter=adapter,
                git_runner=runner,
            )
            with self.assertRaisesRegex(ValidationError, "replay"):
                claim_concurrency_handoff(
                    data,
                    _request(handoffs[0], "claim-other-id"),
                    packet_adapter=adapter,
                    git_runner=runner,
                )
            with self.assertRaisesRegex(ValidationError, "replay"):
                claim_concurrency_handoff(
                    data,
                    _request(handoffs[1], "claim-replay"),
                    packet_adapter=adapter,
                    git_runner=runner,
                )
            ledger = json.loads((data / CLAIM_FILENAME).read_text())
            self.assertEqual(len(ledger["claims"]), 1)
            self.assertEqual(
                ledger["claims"][0]["claim_digest"],
                first["claim_digest"],
            )

    def test_packet_worktree_and_request_drift_produce_no_claim(self) -> None:
        cases = ("packet", "worktree", "head", "adapter", "runtime")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                data, adapter, bindings, runner, _effect, handoffs = _source(Path(tmp))
                handoff = handoffs[0]
                request = _request(handoff, f"claim-drift-{case}")
                if case == "packet":
                    packet = adapter.packets[int(handoff["issue_number"])]
                    packet["status"] = "PAUSED"
                    packet["queue_state"] = "QUEUED"
                elif case == "worktree":
                    bindings[str(Path(handoff["worktree_path"]).resolve())]["head"] = "f" * 40
                elif case == "head":
                    request["head"] = "f" * 40
                elif case == "adapter":
                    request["adapter_id"] = "OTHER_ADAPTER"
                else:
                    request["runtime"] = "codex"
                with self.assertRaises(ValidationError):
                    claim_concurrency_handoff(
                        data,
                        request,
                        packet_adapter=adapter,
                        git_runner=runner,
                    )
                self.assertFalse((data / CLAIM_FILENAME).exists())

    def test_post_reservation_drift_leaves_replay_blocking_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, adapter, _bindings, runner, _effect, handoffs = _source(Path(tmp))
            handoff = handoffs[0]
            request = _request(handoff, "claim-crash-window")
            original = adapter.read_readiness_packet_fact
            calls = {"count": 0}

            def drifting(repository: str, issue_number: int) -> dict:
                calls["count"] += 1
                fact = original(repository, issue_number)
                if calls["count"] >= 2:
                    fact = dict(fact)
                    fact["packet_status"] = "PAUSED"
                    fact["queue_state"] = "QUEUED"
                return fact

            adapter.read_readiness_packet_fact = drifting
            with self.assertRaisesRegex(ValidationError, "lifecycle drifted"):
                claim_concurrency_handoff(
                    data,
                    request,
                    packet_adapter=adapter,
                    git_runner=runner,
                )
            ledger = json.loads((data / CLAIM_FILENAME).read_text())
            self.assertEqual(len(ledger["claims"]), 1)
            self.assertEqual(ledger["claims"][0]["state"], "IN_PROGRESS")
            self.assertIsNone(ledger["claims"][0]["receipt"])

            adapter.read_readiness_packet_fact = original
            with self.assertRaisesRegex(ValidationError, "replay"):
                claim_concurrency_handoff(
                    data,
                    request,
                    packet_adapter=adapter,
                    git_runner=runner,
                )

    def test_source_handoff_or_effect_tamper_fails_closed(self) -> None:
        for source in ("handoff", "effect"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                data, adapter, _bindings, runner, _effect, handoffs = _source(Path(tmp))
                request = _request(handoffs[0], f"claim-tamper-{source}")
                if source == "handoff":
                    ledger = json.loads((data / HANDOFF_FILENAME).read_text())
                    ledger["handoffs"][0]["runtime"] = "tampered"
                    (data / HANDOFF_FILENAME).write_text(
                        json.dumps(ledger),
                        encoding="utf-8",
                    )
                else:
                    ledger = json.loads((data / EFFECT_FILENAME).read_text())
                    ledger["effects"][0]["receipt"]["receipts"][0]["result"] = "REFUSED"
                    (data / EFFECT_FILENAME).write_text(
                        json.dumps(ledger),
                        encoding="utf-8",
                    )
                with self.assertRaises(ValidationError):
                    claim_concurrency_handoff(
                        data,
                        request,
                        packet_adapter=adapter,
                        git_runner=runner,
                    )
                self.assertFalse((data / CLAIM_FILENAME).exists())

    def test_claim_ledger_is_preserved_for_replay_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            (data / "registry.json").write_text(
                '{"schema_version":1,"projects":{}}\n',
                encoding="utf-8",
            )
            (data / CLAIM_FILENAME).write_text(
                '{"schema_version":1,"kind":"concurrency_handoff_claim_ledger","claims":[]}\n',
                encoding="utf-8",
            )
            dest = base / "backup"
            result = backup_data_root(data, dest)
            self.assertEqual(result["status"], "ok")
            self.assertTrue((dest / CLAIM_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
