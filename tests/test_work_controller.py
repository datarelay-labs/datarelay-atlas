"""Autonomous Work Controller PoC regressions (ADR-0006)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from atlas.provenance import ValidationError
from atlas.work_controller import (
    AuditResult,
    CompletionEvent,
    FixedAuditAdapter,
    RecordingCursorDispatcher,
    RecordingObserver,
    RecordingWorkPacketAdapter,
    WorkController,
    WorkControllerStore,
    WorkstreamRecord,
    build_persist_resume_command,
)


HEAD_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class WorkControllerTests(unittest.TestCase):
    def _ctl(
        self,
        tmp: str,
        *,
        verdict: str = "PASS",
        findings: str = "ok",
        max_attempts: int = 3,
    ) -> tuple[WorkController, RecordingCursorDispatcher, RecordingWorkPacketAdapter]:
        worktree = Path(tmp) / "wt"
        worktree.mkdir()
        dispatcher = RecordingCursorDispatcher()
        packets = RecordingWorkPacketAdapter()
        audit = FixedAuditAdapter(AuditResult(verdict=verdict, findings=findings))
        observer = RecordingObserver()
        ctl = WorkController(
            Path(tmp) / "data",
            audit=audit,
            work_packet=packets,
            dispatcher=dispatcher,
            observer=observer,
        )
        ctl.register_workstream(
            workstream="awc-poc",
            repository="datarelay-labs/datarelay-atlas",
            issue_number=12,
            branch="feature/autonomous-work-controller-poc",
            worktree_path=str(worktree),
            expected_head=HEAD_A,
            max_attempts=max_attempts,
        )
        return ctl, dispatcher, packets

    def _event(self, **overrides) -> dict:
        base = {
            "event_id": "evt-1",
            "workstream": "awc-poc",
            "issue_number": 12,
            "branch": "feature/autonomous-work-controller-poc",
            "head": HEAD_A,
            "attempt": 1,
            "session_id": "sess-1",
        }
        base.update(overrides)
        return base

    def test_pass_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["verdict"], "PASS")
            self.assertEqual(outcome["state"], "PASSED")
            self.assertEqual(outcome["action"], "stop")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(packets.updates, [])
            shown = ctl.show("awc-poc")
            self.assertEqual(shown["state"], "PASSED")

    def test_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            first = ctl.handle_completion(self._event())
            second = ctl.handle_completion(self._event())
            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(second["idempotent_replay"])
            self.assertEqual(second["event_id"], "evt-1")
            self.assertEqual(len(dispatcher.requests), 0)
            # Audit must not re-fire on replay.
            self.assertEqual(len(ctl.audit.calls), 1)

    def test_stale_head_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, _, _ = self._ctl(tmp)
            with self.assertRaises(ValidationError):
                ctl.handle_completion(self._event(head=HEAD_B))
            self.assertEqual(ctl.show("awc-poc")["state"], "IDLE")

    def test_branch_and_issue_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, _, _ = self._ctl(tmp)
            with self.assertRaises(ValidationError):
                ctl.handle_completion(self._event(branch="other"))
            with self.assertRaises(ValidationError):
                ctl.handle_completion(self._event(issue_number=99))

    def test_rework_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(
                tmp, verdict="REWORK", findings="fix gaps"
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["verdict"], "REWORK")
            self.assertEqual(outcome["state"], "REWORK_DISPATCHED")
            self.assertEqual(outcome["action"], "rework_dispatched")
            self.assertEqual(outcome["next_attempt"], 2)
            self.assertEqual(outcome["resume_prompt"], "/resume")
            self.assertEqual(len(dispatcher.requests), 1)
            req = dispatcher.requests[0]
            self.assertEqual(req.resume_prompt, "/resume")
            self.assertEqual(req.attempt, 2)
            self.assertEqual(
                build_persist_resume_command(req),
                outcome["dispatch_command"],
            )
            self.assertEqual(len(packets.updates), 1)
            self.assertIn("fix gaps", packets.updates[0]["findings"])
            self.assertEqual(ctl.show("awc-poc")["attempt"], 2)

    def test_retry_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(
                tmp, verdict="REWORK", findings="still broken", max_attempts=1
            )
            outcome = ctl.handle_completion(self._event(attempt=1))
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "retry_exhausted")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(packets.updates, [])

    def test_human_required_from_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, _ = self._ctl(
                tmp, verdict="HUMAN_REQUIRED", findings="needs owner"
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["action"], "stop")
            self.assertEqual(dispatcher.requests, [])

    def test_rework_then_new_head_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, _ = self._ctl(tmp, verdict="REWORK")
            ctl.handle_completion(self._event(event_id="evt-1", attempt=1))
            # Switch audit to PASS for second cycle.
            ctl.audit = FixedAuditAdapter(AuditResult(verdict="PASS", findings="fixed"))
            outcome = ctl.handle_completion(
                self._event(event_id="evt-2", attempt=2, head=HEAD_B)
            )
            self.assertEqual(outcome["state"], "PASSED")
            self.assertEqual(outcome["head"], HEAD_B)
            self.assertEqual(len(dispatcher.requests), 1)
            self.assertEqual(ctl.show("awc-poc")["expected_head"], HEAD_B)

    def test_reconcile_after_restart_mid_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            store = WorkControllerStore(data_root)
            store.put(
                WorkstreamRecord(
                    workstream="awc-poc",
                    repository="datarelay-labs/datarelay-atlas",
                    issue_number=12,
                    branch="feature/autonomous-work-controller-poc",
                    worktree_path=str(worktree.resolve()),
                    expected_head=HEAD_A,
                    state="AUDITING",
                    attempt=0,
                    max_attempts=3,
                    pending_event=self._event(),
                )
            )
            dispatcher = RecordingCursorDispatcher()
            packets = RecordingWorkPacketAdapter()
            ctl = WorkController(
                data_root,
                audit=FixedAuditAdapter(AuditResult(verdict="PASS", findings="recovered")),
                work_packet=packets,
                dispatcher=dispatcher,
            )
            outcomes = ctl.reconcile("awc-poc")
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["state"], "PASSED")
            self.assertEqual(ctl.show("awc-poc")["state"], "PASSED")
            self.assertIsNone(ctl.show("awc-poc")["pending_event"])

    def test_unsupported_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "work-controller.json"
            path.write_text(
                json.dumps({"schema_version": 99, "workstreams": {}}),
                encoding="utf-8",
            )
            store = WorkControllerStore(Path(tmp))
            with self.assertRaises(ValidationError):
                store.list_workstreams()

    def test_completion_event_requires_fields(self):
        with self.assertRaises(ValidationError):
            CompletionEvent.from_dict({"event_id": "x"})


if __name__ == "__main__":
    unittest.main()
