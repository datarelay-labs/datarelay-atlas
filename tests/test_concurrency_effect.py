from __future__ import annotations

import contextlib
import hashlib
import io
import json
from datetime import datetime, timezone
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.concurrency_admission import SNAPSHOT_FILENAME, plan_concurrency_admission
from atlas.concurrency_authorization import (
    FILENAME as AUTH_FILENAME,
    publish_concurrency_dispatch_authorization,
)
from atlas.concurrency_effect import (
    AUTHORITY,
    FILENAME,
    commit_concurrency_dispatch_effect,
    concurrency_dispatch_effect_dashboard,
    concurrency_effect_receipt_digest,
    validate_concurrency_effect_receipt,
)
from atlas.data_protection import backup_data_root
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs" / "contracts" / "fixtures"


def _snapshot() -> dict:
    return json.loads(
        (FIXTURES / "concurrency-admission-snapshot.example.json").read_text()
    )


def _prepare(root: Path) -> dict:
    snapshot = _snapshot()
    (root / SNAPSHOT_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")
    plan = plan_concurrency_admission(snapshot)
    request = {
        "schema_version": 1,
        "kind": "concurrency_dispatch_authorization_request",
        "authorization_id": "effect-auth-1",
        "expected_plan_digest": plan["plan_digest"],
        "evaluated_at": "2026-09-30T12:30:00Z",
    }
    dashboard = publish_concurrency_dispatch_authorization(root, request)
    return dashboard["authorization"]


class RecordingPort:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self._lock = threading.Lock()

    def dispatch(self, request):
        with self._lock:
            index = len(self.calls)
            self.calls.append(deepcopy(request))
            result = self.results[index]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class BarrierPort:
    def __init__(self):
        self.barrier = threading.Barrier(2, timeout=2)
        self.calls = []
        self._lock = threading.Lock()

    def dispatch(self, request):
        node_id = request["assignment"]["node_id"]
        with self._lock:
            self.calls.append(node_id)
        self.barrier.wait()
        if node_id.endswith("#201"):
            time.sleep(0.05)
        return {
            "result": "DISPATCHED",
            "dispatch_ref": "parallel-" + node_id.rsplit("#", 1)[-1],
        }


class ConcurrencyEffectTests(unittest.TestCase):
    def test_dangling_effect_ledger_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / FILENAME).symlink_to(root / "missing-effect-ledger.json")
            with self.assertRaisesRegex(ValidationError, "ledger path is unsafe"):
                concurrency_dispatch_effect_dashboard(root)

    def test_duplicate_effect_ledger_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = (
                '{"schema_version":1,"kind":"concurrency_dispatch_effect_ledger",'
                '"authority":"DISPATCH_EFFECT_RECEIPT_ONLY",'
                '"effects":[],"effects":[]}'
            )
            (root / FILENAME).write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON keys"):
                concurrency_dispatch_effect_dashboard(root)

    def setUp(self) -> None:
        patcher = patch(
            "atlas.concurrency_effect._trusted_effect_time",
            return_value=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_receipt_schema_fixture(self):
        schema = json.loads(
            (ROOT / "docs/contracts/concurrency-dispatch-effect-receipt.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "concurrency-dispatch-effect-receipt.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)

    def test_all_dispatched_calls_each_assignment_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = RecordingPort(
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-node-1"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-node-2"},
                ]
            )
            receipt = commit_concurrency_dispatch_effect(
                root,
                effect_id="effect-1",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            self.assertEqual(receipt["result"], "DISPATCHED")
            self.assertEqual(receipt["dispatched_count"], 2)
            self.assertEqual(len(port.calls), 2)
            self.assertEqual(
                [call["assignment"]["node_id"] for call in port.calls],
                [item["node_id"] for item in auth["assignments"]],
            )
            self.assertEqual(len({call["replay_key"] for call in port.calls}), 2)
            self.assertTrue(all(call["authorization_digest"] == auth["authorization_digest"] for call in port.calls))
            dashboard = concurrency_dispatch_effect_dashboard(root)
            self.assertEqual(dashboard["terminal_count"], 1)
            self.assertEqual(dashboard["in_progress_count"], 0)
            self.assertEqual(dashboard["join_authority"], "NONE")
            self.assertEqual(dashboard["pass_authority"], "NONE")

    def test_dispatch_calls_overlap_and_receipts_preserve_authorization_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = BarrierPort()
            receipt = commit_concurrency_dispatch_effect(
                root,
                effect_id="parallel-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            self.assertEqual(receipt["result"], "DISPATCHED")
            self.assertEqual(len(port.calls), 2)
            self.assertEqual(set(port.calls), {item["node_id"] for item in auth["assignments"]})
            self.assertEqual(
                [item["node_id"] for item in receipt["receipts"]],
                [item["node_id"] for item in auth["assignments"]],
            )
            self.assertEqual(
                [item["dispatch_ref"] for item in receipt["receipts"]],
                ["parallel-201", "parallel-202"],
            )

    def test_partial_human_and_error_results_are_bounded_without_retry(self):
        cases = [
            (
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "REFUSED", "dispatch_ref": None},
                ],
                "PARTIAL",
                (1, 1, 0, 0),
            ),
            (
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "HUMAN_REQUIRED", "dispatch_ref": None},
                ],
                "HUMAN_REQUIRED",
                (1, 0, 1, 0),
            ),
            (
                [
                    RuntimeError("boom"),
                    {"unexpected": True},
                ],
                "FAILED",
                (0, 0, 0, 2),
            ),
        ]
        for results, expected, counts in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                auth = _prepare(root)
                port = RecordingPort(results)
                receipt = commit_concurrency_dispatch_effect(
                    root,
                    effect_id=f"effect-{expected.lower()}",
                    expected_authorization_digest=auth["authorization_digest"],
                    effect_port=port,
                )
                self.assertEqual(receipt["result"], expected)
                self.assertEqual(len(port.calls), 2)
                self.assertEqual(
                    (
                        receipt["dispatched_count"],
                        receipt["refused_count"],
                        receipt["human_required_count"],
                        receipt["error_count"],
                    ),
                    counts,
                )

    def test_stale_or_wrong_authorization_causes_zero_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = RecordingPort([])
            with self.assertRaisesRegex(ValidationError, "digest mismatch"):
                commit_concurrency_dispatch_effect(
                    root,
                    effect_id="wrong-digest",
                    expected_authorization_digest="0" * 64,
                    effect_port=port,
                )
            self.assertEqual(port.calls, [])

            snapshot = json.loads((root / SNAPSHOT_FILENAME).read_text())
            snapshot["execution_slots"][0]["route_id"] = "changed-route"
            (root / SNAPSHOT_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "not current"):
                commit_concurrency_dispatch_effect(
                    root,
                    effect_id="stale-auth",
                    expected_authorization_digest=auth["authorization_digest"],
                    effect_port=port,
                )
            self.assertEqual(port.calls, [])

    def test_stale_effect_time_causes_zero_calls_and_zero_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = RecordingPort([])
            with patch(
                "atlas.concurrency_effect._trusted_effect_time",
                return_value=datetime(2026, 9, 30, 13, 30, 1, tzinfo=timezone.utc),
            ), self.assertRaisesRegex(ValidationError, "stale"):
                commit_concurrency_dispatch_effect(
                    root,
                    effect_id="stale-at-effect-time",
                    expected_authorization_digest=auth["authorization_digest"],
                    effect_port=port,
                )
            self.assertEqual(port.calls, [])
            self.assertFalse((root / FILENAME).exists())

    def test_duplicate_effect_or_authorization_replay_is_blocked_before_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            first = RecordingPort(
                [
                    {"result": "DISPATCHED", "dispatch_ref": "s1"},
                    {"result": "DISPATCHED", "dispatch_ref": "s2"},
                ]
            )
            commit_concurrency_dispatch_effect(
                root,
                effect_id="same-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=first,
            )
            replay = RecordingPort([])
            for effect_id in ("same-effect", "different-effect"):
                with self.subTest(effect_id=effect_id), self.assertRaisesRegex(
                    ValidationError, "replay"
                ):
                    commit_concurrency_dispatch_effect(
                        root,
                        effect_id=effect_id,
                        expected_authorization_digest=auth["authorization_digest"],
                        effect_port=replay,
                    )
            self.assertEqual(replay.calls, [])

    def test_in_progress_reservation_blocks_crash_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            ledger = {
                "schema_version": 1,
                "kind": "concurrency_dispatch_effect_ledger",
                "authority": AUTHORITY,
                "effects": [
                    {
                        "effect_id": "crash-reservation",
                        "authorization_digest": auth["authorization_digest"],
                        "authorization_id": auth["authorization_id"],
                        "state": "IN_PROGRESS",
                        "assignment_count": auth["assignment_count"],
                        "receipt": None,
                        "receipt_digest": None,
                    }
                ],
            }
            (root / FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            port = RecordingPort([])
            with self.assertRaisesRegex(ValidationError, "replay"):
                commit_concurrency_dispatch_effect(
                    root,
                    effect_id="new-effect",
                    expected_authorization_digest=auth["authorization_digest"],
                    effect_port=port,
                )
            self.assertEqual(port.calls, [])
            dashboard = concurrency_dispatch_effect_dashboard(root)
            self.assertEqual(dashboard["in_progress_count"], 1)

    def test_tampered_terminal_ledger_and_secret_like_dispatch_ref_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = RecordingPort(
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ]
            )
            commit_concurrency_dispatch_effect(
                root,
                effect_id="tamper-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            ledger = json.loads((root / FILENAME).read_text())
            ledger["effects"][0]["receipt"]["dispatched_count"] = 1
            (root / FILENAME).write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaises(ValidationError):
                concurrency_dispatch_effect_dashboard(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = RecordingPort(
                [
                    {"result": "DISPATCHED", "dispatch_ref": "sk-secret"},
                    {"result": "REFUSED", "dispatch_ref": None},
                ]
            )
            receipt = commit_concurrency_dispatch_effect(
                root,
                effect_id="secret-ref",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            self.assertEqual(receipt["error_count"], 1)
            self.assertEqual(receipt["result"], "FAILED")

    def test_receipt_digest_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = _prepare(root)
            port = RecordingPort(
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ]
            )
            result = commit_concurrency_dispatch_effect(
                root,
                effect_id="digest-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            receipt = {key: value for key, value in result.items() if key != "receipt_digest"}
            validated = validate_concurrency_effect_receipt(
                receipt,
                expected_receipt_digest=result["receipt_digest"],
                expected_authorization_digest=auth["authorization_digest"],
            )
            self.assertEqual(validated, receipt)
            self.assertEqual(
                concurrency_effect_receipt_digest(receipt),
                result["receipt_digest"],
            )

    def test_cli_web_mcp_read_surfaces_and_backup_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            auth = _prepare(root)
            port = RecordingPort(
                [
                    {"result": "DISPATCHED", "dispatch_ref": "session-one"},
                    {"result": "DISPATCHED", "dispatch_ref": "session-two"},
                ]
            )
            commit_concurrency_dispatch_effect(
                root,
                effect_id="surface-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(["--data-root", str(root), "concurrency", "effects-show"]),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["terminal_count"], 1)

            svc = AtlasService(root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                concurrency_effects_factory=svc.concurrency_dispatch_effect_dashboard,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_concurrency_dispatch_effects", names)
            result = tools.call(
                "get_concurrency_dispatch_effects",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["terminal_count"], 1)

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
            self.assertIn("Dispatch effect receipts", body)
            self.assertIn("DISPATCH_EFFECT_RECEIPT_ONLY", body)
            self.assertIn("PASS authority", body)

            backup_data_root(root, base / "backup")
            self.assertTrue((base / "backup" / FILENAME).is_file())

    def test_module_has_no_concrete_transport_or_retry_loop(self):
        source = (ROOT / "atlas" / "concurrency_effect.py").read_text()
        for forbidden in (
            "subprocess",
            "requests",
            "urllib",
            "socket",
            "agent persist",
            "PtyPersist",
            "while True",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
