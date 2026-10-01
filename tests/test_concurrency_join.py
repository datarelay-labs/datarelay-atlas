from __future__ import annotations

import contextlib
import hashlib
import io
import json
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.concurrency_admission import SNAPSHOT_FILENAME, plan_concurrency_admission
from atlas.concurrency_authorization import publish_concurrency_dispatch_authorization
from atlas.concurrency_effect import FILENAME as EFFECT_FILENAME, commit_concurrency_dispatch_effect
from atlas.concurrency_join import (
    FILENAME,
    concurrency_dispatch_join_dashboard,
    record_concurrency_dispatch_join,
    validate_concurrency_dispatch_join,
)
from atlas.data_protection import backup_data_root
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _snapshot() -> dict:
    return json.loads(
        (FIXTURES / "concurrency-admission-snapshot.example.json").read_text()
    )


class RecordingPort:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def dispatch(self, request):
        self.calls.append(deepcopy(request))
        result = self.results[len(self.calls) - 1]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def _prepare_effect(root: Path, results, *, effect_id: str = "join-effect") -> dict:
    snapshot = _snapshot()
    (root / SNAPSHOT_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")
    plan = plan_concurrency_admission(snapshot)
    auth = publish_concurrency_dispatch_authorization(
        root,
        {
            "schema_version": 1,
            "kind": "concurrency_dispatch_authorization_request",
            "authorization_id": f"{effect_id}-auth",
            "expected_plan_digest": plan["plan_digest"],
            "evaluated_at": "2026-09-30T12:30:00Z",
        },
    )["authorization"]
    return commit_concurrency_dispatch_effect(
        root,
        effect_id=effect_id,
        expected_authorization_digest=auth["authorization_digest"],
        effect_port=RecordingPort(results),
    )


def _join_observation(receipt: dict, *, join_id: str = "join-1", outcome: str = "COMPLETE") -> dict:
    outcomes = []
    for index, item in enumerate(receipt["receipts"], start=1):
        if item["result"] != "DISPATCHED":
            continue
        outcomes.append(
            {
                "node_id": item["node_id"],
                "slot_id": item["slot_id"],
                "worker_id": item["worker_id"],
                "provider": item["provider"],
                "runtime": item["runtime"],
                "route_id": item["route_id"],
                "head": item["head"],
                "outcome": outcome,
                "duration_ms": 1000 * index,
                "evidence_ref": f"evidence-{index}",
            }
        )
    return {
        "schema_version": 1,
        "kind": "concurrency_dispatch_join_observation",
        "join_id": join_id,
        "effect_id": receipt["effect_id"],
        "effect_receipt_digest": receipt["receipt_digest"],
        "authorization_digest": receipt["authorization_digest"],
        "observed_at": "2026-09-30T13:00:00Z",
        "outcomes": outcomes,
    }


def _resign(join: dict) -> dict:
    body = {key: value for key, value in join.items() if key != "join_digest"}
    join["join_digest"] = hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return join


class ConcurrencyJoinTests(unittest.TestCase):
    def test_boolean_ledger_schema_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {
                "schema_version": True,
                "kind": "concurrency_dispatch_join_ledger",
                "authority": "MEASUREMENT_ONLY",
                "joins": [],
            }
            (root / FILENAME).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "schema"):
                concurrency_dispatch_join_dashboard(root)



    def test_duplicate_join_collection_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = (
                '{"schema_version":1,'
                '"kind":"concurrency_dispatch_join_ledger",'
                '"authority":"MEASUREMENT_ONLY",'
                '"joins":[{}],"joins":[]}'
            )
            (root / FILENAME).write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON keys"):
                concurrency_dispatch_join_dashboard(root)

    def setUp(self) -> None:
        patcher = patch(
            "atlas.concurrency_effect._trusted_effect_time",
            return_value=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_schema_fixture_and_runtime_parity(self):
        schema = json.loads(
            (CONTRACTS / "concurrency-dispatch-join.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "concurrency-dispatch-join.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_concurrency_dispatch_join(fixture), fixture)

    def test_all_dispatched_join_is_measurement_only_pass_and_replay_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = _prepare_effect(
                root,
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ],
            )
            observation = _join_observation(receipt)
            dashboard = record_concurrency_dispatch_join(root, observation)
            self.assertEqual(dashboard["join_count"], 1)
            self.assertEqual(dashboard["latest_join"]["result"], "PASS")
            self.assertEqual(dashboard["latest_join"]["pass_authority"], "MEASUREMENT_ONLY")
            self.assertEqual(dashboard["latest_join"]["dispatched_count"], 2)
            self.assertEqual(dashboard["latest_join"]["dispatch_omission_count"], 0)
            with self.assertRaisesRegex(ValidationError, "replay"):
                record_concurrency_dispatch_join(root, observation)

    def test_partial_effect_keeps_non_dispatched_assignment_as_explicit_omission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = _prepare_effect(
                root,
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "REFUSED", "dispatch_ref": None},
                ],
                effect_id="partial-effect",
            )
            dashboard = record_concurrency_dispatch_join(
                root, _join_observation(receipt, join_id="partial-join")
            )
            join = dashboard["latest_join"]
            self.assertEqual(join["effect_result"], "PARTIAL")
            self.assertEqual(join["result"], "PASS")
            self.assertEqual(join["dispatched_count"], 1)
            self.assertEqual(join["dispatch_omission_count"], 1)
            self.assertEqual(join["dispatch_omissions"][0]["dispatch_result"], "REFUSED")

    def test_unsuccessful_effect_results_cannot_become_join_evidence(self):
        cases = [
            (
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "HUMAN_REQUIRED", "dispatch_ref": None},
                ],
                "HUMAN_REQUIRED",
            ),
            (
                [
                    {"result": "REFUSED", "dispatch_ref": None},
                    {"result": "REFUSED", "dispatch_ref": None},
                ],
                "FAILED",
            ),
        ]
        for results, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                receipt = _prepare_effect(root, results, effect_id=f"effect-{expected.lower()}")
                self.assertEqual(receipt["result"], expected)
                with self.assertRaisesRegex(ValidationError, "unsuccessful dispatch effect"):
                    record_concurrency_dispatch_join(
                        root,
                        _join_observation(receipt, join_id=f"join-{expected.lower()}"),
                    )

    def test_resigned_tampered_join_fails_closed_against_original_dispatch_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = _prepare_effect(
                root,
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ],
                effect_id="tamper-effect",
            )
            record_concurrency_dispatch_join(
                root, _join_observation(receipt, join_id="tamper-join")
            )
            ledger = json.loads((root / FILENAME).read_text())
            ledger["joins"][0]["outcomes"][0]["worker_id"] = "forged-worker"
            _resign(ledger["joins"][0])
            (root / FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "stored attribution"):
                concurrency_dispatch_join_dashboard(root)

    def test_missing_or_tampered_dispatch_receipt_invalidates_existing_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = _prepare_effect(
                root,
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ],
                effect_id="source-effect",
            )
            record_concurrency_dispatch_join(
                root, _join_observation(receipt, join_id="source-join")
            )
            effects = json.loads((root / EFFECT_FILENAME).read_text())
            effects["effects"][0]["receipt"]["receipts"][0]["worker_id"] = "tampered-worker"
            (root / EFFECT_FILENAME).write_text(json.dumps(effects), encoding="utf-8")
            with self.assertRaises(ValidationError):
                concurrency_dispatch_join_dashboard(root)

    def test_cli_web_mcp_read_surfaces_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            receipt = _prepare_effect(
                root,
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ],
                effect_id="surface-effect",
            )
            record_concurrency_dispatch_join(
                root, _join_observation(receipt, join_id="surface-join")
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(["--data-root", str(root), "concurrency", "joins-show"]),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["join_count"], 1)
            self.assertEqual(cli["pass_authority"], "MEASUREMENT_ONLY")

            svc = AtlasService(root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                concurrency_joins_factory=svc.concurrency_dispatch_join_dashboard,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_concurrency_dispatch_joins", names)
            result = tools.call(
                "get_concurrency_dispatch_joins",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["join_count"], 1)

            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/concurrency",
                    "QUERY_STRING": "",
                    "HTTP_HOST": "127.0.0.1:8788",
                }
            )
            state = {}

            def start(status, headers):
                state.update(status=status, headers=dict(headers))

            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Dispatch-bound join evidence", body)
            self.assertIn("MEASUREMENT_ONLY", body)

            backup_data_root(root, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
