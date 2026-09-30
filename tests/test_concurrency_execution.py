from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path

from atlas.concurrency_admission import (
    SNAPSHOT_FILENAME,
    plan_concurrency_admission,
)
from atlas.concurrency_effect import get_concurrency_dispatch_effect_entry
from atlas.concurrency_execution import (
    concurrency_execution_dashboard,
    start_concurrency_execution,
)
from atlas.concurrency_join import record_concurrency_dispatch_join
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs" / "contracts" / "fixtures"


def _snapshot() -> dict:
    return json.loads(
        (FIXTURES / "concurrency-admission-snapshot.example.json").read_text()
    )


def _write_snapshot(root: Path, snapshot: dict) -> dict:
    (root / SNAPSHOT_FILENAME).write_text(
        json.dumps(snapshot), encoding="utf-8"
    )
    return plan_concurrency_admission(snapshot)


def _request(
    plan: dict,
    *,
    authorization_id: str = "cycle-auth-1",
) -> dict:
    return {
        "schema_version": 1,
        "kind": "concurrency_dispatch_authorization_request",
        "authorization_id": authorization_id,
        "expected_plan_digest": plan["plan_digest"],
        "evaluated_at": "2026-09-30T12:30:00Z",
    }


class BarrierPort:
    def __init__(self):
        self.barrier = threading.Barrier(2, timeout=2)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def dispatch(self, request):
        with self._lock:
            self.calls.append(deepcopy(request))
        self.barrier.wait()
        node = request["assignment"]["node_id"]
        if node.endswith("#201"):
            time.sleep(0.05)
        return {
            "result": "DISPATCHED",
            "dispatch_ref": "cycle-" + node.rsplit("#", 1)[-1],
        }


class RecordingPort:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def dispatch(self, request):
        with self._lock:
            index = len(self.calls)
            self.calls.append(deepcopy(request))
        if self.results:
            result = self.results[index]
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)
        return {
            "result": "DISPATCHED",
            "dispatch_ref": f"session-{index + 1}",
        }


def _join_observation(receipt: dict) -> dict:
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
                "outcome": "COMPLETE",
                "duration_ms": 1000 * index,
                "evidence_ref": f"cycle-evidence-{index}",
            }
        )
    return {
        "schema_version": 1,
        "kind": "concurrency_dispatch_join_observation",
        "join_id": "cycle-join-1",
        "effect_id": receipt["effect_id"],
        "effect_receipt_digest": receipt["receipt_digest"],
        "authorization_digest": receipt["authorization_digest"],
        "observed_at": "2026-09-30T13:00:00Z",
        "outcomes": outcomes,
    }


class ConcurrencyExecutionTests(unittest.TestCase):
    def test_execution_operation_dispatches_two_assignments_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _write_snapshot(root, _snapshot())
            port = BarrierPort()
            dashboard = start_concurrency_execution(
                root,
                cycle_id="cycle-1",
                authorization_request=_request(plan),
                effect_id="cycle-effect-1",
                effect_port=port,
            )
            self.assertEqual(len(port.calls), 2)
            self.assertEqual(dashboard["execution_count"], 1)
            latest = dashboard["latest_execution"]
            self.assertEqual(latest["stage"], "AWAITING_JOIN")
            self.assertEqual(latest["plan_digest"], plan["plan_digest"])
            self.assertEqual(latest["assignment_count"], 2)
            self.assertEqual(latest["effect_result"], "DISPATCHED")
            self.assertEqual(latest["pass_authority"], "NONE")

    def test_same_plan_replay_is_rejected_before_new_port_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _write_snapshot(root, _snapshot())
            first = RecordingPort()
            start_concurrency_execution(
                root,
                cycle_id="cycle-first",
                authorization_request=_request(plan, authorization_id="auth-first"),
                effect_id="effect-first",
                effect_port=first,
            )
            self.assertEqual(len(first.calls), 2)

            replay = RecordingPort()
            with self.assertRaisesRegex(ValidationError, "replay"):
                start_concurrency_execution(
                    root,
                    cycle_id="cycle-second",
                    authorization_request=_request(plan, authorization_id="auth-second"),
                    effect_id="effect-second",
                    effect_port=replay,
                )
            self.assertEqual(replay.calls, [])

    def test_conflicting_plan_is_rejected_with_zero_effect_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _snapshot()
            snapshot["graph"]["nodes"][1]["resources"] = [
                "component:retrieval"
            ]
            plan = _write_snapshot(root, snapshot)
            port = RecordingPort()
            with self.assertRaises(ValidationError):
                start_concurrency_execution(
                    root,
                    cycle_id="conflict-cycle",
                    authorization_request=_request(plan),
                    effect_id="conflict-effect",
                    effect_port=port,
                )
            self.assertEqual(port.calls, [])
            self.assertEqual(
                concurrency_execution_dashboard(root)["execution_count"], 0
            )

    def test_join_advances_execution_from_awaiting_to_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _write_snapshot(root, _snapshot())
            start_concurrency_execution(
                root,
                cycle_id="join-cycle",
                authorization_request=_request(plan),
                effect_id="join-effect",
                effect_port=RecordingPort(),
            )
            before = concurrency_execution_dashboard(root)
            self.assertEqual(
                before["latest_execution"]["stage"], "AWAITING_JOIN"
            )
            entry = get_concurrency_dispatch_effect_entry(root, "join-effect")
            receipt = {
                **entry["receipt"],
                "receipt_digest": entry["receipt_digest"],
            }
            record_concurrency_dispatch_join(
                root, _join_observation(receipt)
            )
            after = concurrency_execution_dashboard(root)
            self.assertEqual(after["latest_execution"]["stage"], "COMPLETE")
            self.assertEqual(after["latest_execution"]["join_result"], "PASS")
            self.assertIsNotNone(
                after["latest_execution"]["join_digest"]
            )
            self.assertEqual(after["pass_authority"], "NONE")

    def test_partial_or_human_effect_state_never_claims_complete(self):
        cases = [
            (
                [
                    {"result": "DISPATCHED", "dispatch_ref": "one"},
                    {"result": "REFUSED", "dispatch_ref": None},
                ],
                "AWAITING_JOIN",
            ),
            (
                [
                    {"result": "DISPATCHED", "dispatch_ref": "one"},
                    {"result": "HUMAN_REQUIRED", "dispatch_ref": None},
                ],
                "HUMAN_REQUIRED",
            ),
        ]
        for results, stage in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan = _write_snapshot(root, _snapshot())
                dashboard = start_concurrency_execution(
                    root,
                    cycle_id=f"cycle-{stage.lower()}",
                    authorization_request=_request(plan),
                    effect_id=f"effect-{stage.lower()}",
                    effect_port=RecordingPort(results),
                )
                self.assertEqual(
                    dashboard["latest_execution"]["stage"], stage
                )
                self.assertNotEqual(
                    dashboard["latest_execution"]["stage"], "COMPLETE"
                )


    def test_partial_dispatch_stays_partial_after_dispatched_subset_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = _write_snapshot(root, _snapshot())
            start_concurrency_execution(
                root,
                cycle_id="partial-join-cycle",
                authorization_request=_request(plan),
                effect_id="partial-join-effect",
                effect_port=RecordingPort([
                    {"result": "DISPATCHED", "dispatch_ref": "one"},
                    {"result": "REFUSED", "dispatch_ref": None},
                ]),
            )
            entry = get_concurrency_dispatch_effect_entry(
                root, "partial-join-effect"
            )
            receipt = {
                **entry["receipt"],
                "receipt_digest": entry["receipt_digest"],
            }
            record_concurrency_dispatch_join(
                root, _join_observation(receipt)
            )
            dashboard = concurrency_execution_dashboard(root)
            latest = dashboard["latest_execution"]
            self.assertEqual(latest["effect_result"], "PARTIAL")
            self.assertEqual(latest["join_result"], "PASS")
            self.assertEqual(latest["stage"], "PARTIAL")
            self.assertEqual(dashboard["stage_counts"]["PARTIAL"], 1)
            self.assertEqual(dashboard["stage_counts"]["COMPLETE"], 0)

if __name__ == "__main__":
    unittest.main()
