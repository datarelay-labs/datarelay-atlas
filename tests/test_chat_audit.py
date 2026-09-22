"""Continuous Chat Audit Supervisor PoC regressions (ADR-0007)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas.chat_audit import (
    RESUME_COMMAND,
    ChatAuditController,
    FakeBrowserRolloverProvider,
    FileCheckpointStore,
    FixedUnitExecutor,
    MemoryCheckpointStore,
    RecordingWorkPacketHandoff,
    StagehandRolloverProvider,
    stagehand_hard_dependency_present,
)
from atlas.provenance import ValidationError


HEAD_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
REPO = "datarelay-labs/datarelay-atlas"
BRANCH = "main"


class ChatAuditTests(unittest.TestCase):
    def _ctl(
        self,
        tmp: str,
        *,
        head: str = HEAD_A,
        executor: FixedUnitExecutor | None = None,
        handoff: RecordingWorkPacketHandoff | None = None,
        rollover: FakeBrowserRolloverProvider | None = None,
    ) -> ChatAuditController:
        store = FileCheckpointStore(Path(tmp) / "data")
        current = {"head": head}

        def identity():
            from atlas.chat_audit import Identity

            return Identity(repository=REPO, branch=BRANCH, head=current["head"])

        ctl = ChatAuditController(
            store,
            executor=executor or FixedUnitExecutor(),
            handoff=handoff or RecordingWorkPacketHandoff(),
            rollover=rollover or FakeBrowserRolloverProvider(),
            identity_resolver=identity,
        )
        ctl._test_head = current  # type: ignore[attr-defined]
        return ctl

    def test_01_first_audit_initializes_from_exact_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            out = ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "initialized")
            packet = out["packet"]
            self.assertEqual(packet["current_target_sha"], HEAD_A)
            self.assertIsNone(packet["last_audited_sha"])
            self.assertEqual(packet["audit_status"], "IDLE")
            self.assertIn("changed_code", packet["audit_queue"])
            self.assertTrue(packet["idempotency_run_key"])

    def test_02_second_run_audits_only_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor()
            ctl = self._ctl(tmp, executor=executor)
            # Drain all units on HEAD_A.
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            while True:
                result = ctl.run_slice()
                if result["action"] == "queue_complete":
                    break
                self.assertEqual(result["action"], "slice_complete")
            self.assertEqual(ctl.show()["last_audited_sha"], HEAD_A)
            first_calls = list(executor.calls)

            # Advance HEAD → new delta run should reset queue and execute again.
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            second = ctl.run_slice()
            self.assertEqual(second["action"], "slice_complete")
            self.assertEqual(second["unit"], "changed_code")
            packet = second["packet"]
            self.assertEqual(packet["last_audited_sha"], HEAD_A)
            self.assertEqual(packet["current_target_sha"], HEAD_B)
            self.assertEqual(packet["mode"], "delta")
            self.assertIn(
                ("changed_code", HEAD_B), executor.calls[len(first_calls) :]
            )

    def test_03_no_change_run_is_idempotent_and_cheap(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor()
            ctl = self._ctl(tmp, executor=executor)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            while ctl.run_slice()["action"] != "queue_complete":
                pass
            calls_after_pass = len(executor.calls)
            cheap = ctl.run_slice()
            self.assertEqual(cheap["action"], "cheap_no_change")
            self.assertTrue(cheap["cheap_no_change"])
            self.assertEqual(cheap["units_executed"], 0)
            self.assertEqual(len(executor.calls), calls_after_pass)
            self.assertEqual(ctl.show()["no_change_runs"], 1)

    def test_04_timeout_resumes_from_recorded_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor(timeout_units={"changed_code"})
            ctl = self._ctl(tmp, executor=executor)
            timed = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(timed["action"], "timeout_checkpointed")
            self.assertEqual(timed["packet"]["audit_status"], "IN_SLICE")
            self.assertEqual(timed["packet"]["current_unit"], "changed_code")
            self.assertEqual(timed["packet"]["session"]["state"], "TIMEOUT")

            # Clear timeout behavior and resume — same unit, no skip.
            executor.timeout_units.clear()
            resumed = ctl.run_slice()
            self.assertEqual(resumed["action"], "slice_complete")
            self.assertEqual(resumed["unit"], "changed_code")
            self.assertEqual(
                [c[0] for c in executor.calls],
                ["changed_code", "changed_code"],
            )

    def test_05_duplicate_invocation_cannot_double_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor()
            ctl = self._ctl(tmp, executor=executor)
            first = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(first["action"], "slice_complete")
            self.assertEqual(first["unit"], "changed_code")
            # Force replay of same completed unit by restoring cursor artificially.
            packet = ctl.show()
            store = FileCheckpointStore(Path(tmp) / "data")
            from atlas.chat_audit import AuditControlPacket

            raw = AuditControlPacket.from_dict(packet)
            raw.audit_status = "IDLE"
            raw.current_unit = None
            # completed_units still contains changed_code → select next unit
            store.save(raw)
            second = ctl.run_slice()
            self.assertEqual(second["unit"], "affected_contracts")
            # Explicit idempotent replay when same unit_key already recorded.
            raw = AuditControlPacket.from_dict(ctl.show())
            raw.audit_status = "IDLE"
            raw.current_unit = "changed_code"
            # Put changed_code back as current without removing completed_units
            # by selecting via IN_SLICE path with completed key:
            raw.audit_status = "IN_SLICE"
            store.save(raw)
            replay = ctl.run_slice()
            self.assertEqual(replay["action"], "idempotent_replay")
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["unit"], "changed_code")

    def test_06_stale_head_during_in_slice_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor(timeout_units={"changed_code"})
            ctl = self._ctl(tmp, executor=executor)
            ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            with self.assertRaises(ValidationError):
                ctl.run_slice()

    def test_07_truncated_evidence_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor(truncate_units={"changed_code"})
            ctl = self._ctl(tmp, executor=executor)
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "failed_closed")
            self.assertEqual(out["reason"], "truncated_or_incomplete_evidence")
            self.assertEqual(out["packet"]["audit_status"], "FAILED_CLOSED")

    def test_08_finding_produces_bounded_implementation_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = RecordingWorkPacketHandoff()
            executor = FixedUnitExecutor(
                outcomes={"changed_code": "FINDING"},
                finding_summaries={"changed_code": "missing regression"},
            )
            ctl = self._ctl(tmp, executor=executor, handoff=handoff)
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["outcome"], "FINDING")
            self.assertEqual(len(handoff.handoffs), 1)
            self.assertIn("[AI Work]", handoff.handoffs[0]["title"])
            self.assertIn("Cursor", handoff.handoffs[0]["next_action"])
            self.assertEqual(len(out["packet"]["open_findings"]), 1)

    def test_09_fresh_chat_resume_uses_only_packet_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            payload = ctl.resume_instruction_payload()
            self.assertEqual(payload["resume_command"], RESUME_COMMAND)
            self.assertFalse(payload["conversation_history_required"])
            self.assertEqual(payload["target_repository"], REPO)
            self.assertEqual(payload["current_target_sha"], HEAD_A)
            # New controller instance (fresh Chat) continues from store only.
            store = FileCheckpointStore(Path(tmp) / "data")
            fresh = ChatAuditController(
                store,
                executor=FixedUnitExecutor(),
                identity_resolver=lambda: __import__(
                    "atlas.chat_audit", fromlist=["Identity"]
                ).Identity(REPO, BRANCH, HEAD_A),
            )
            cont = fresh.run_slice()
            self.assertEqual(cont["unit"], "affected_contracts")

    def test_10_fake_rollover_does_not_alter_audit_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollover = FakeBrowserRolloverProvider()
            ctl = self._ctl(tmp, rollover=rollover)
            ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            before = ctl.show()
            ctl.mark_session("ROLLOVER_REQUIRED", notes="chat limit")
            out = ctl.perform_rollover()
            self.assertEqual(out["action"], "rollover_resumed")
            self.assertTrue(out["audit_fields_unchanged"])
            after = out["packet"]
            self.assertEqual(after["session"]["state"], "RESUMED")
            self.assertEqual(after["session"]["last_resume_command"], RESUME_COMMAND)
            self.assertEqual(after["last_audited_sha"], before["last_audited_sha"])
            self.assertEqual(
                after["current_target_sha"], before["current_target_sha"]
            )
            self.assertEqual(
                after["idempotency_run_key"], before["idempotency_run_key"]
            )
            self.assertEqual(
                after["completed_units"], before["completed_units"]
            )
            self.assertEqual(len(rollover.calls), 1)

    def test_11_stagehand_adapter_optional_and_gated(self):
        self.assertFalse(stagehand_hard_dependency_present())
        blocked = StagehandRolloverProvider(provider_approved=False)
        store = MemoryCheckpointStore()
        ctl = ChatAuditController(
            store,
            rollover=blocked,
            identity_resolver=lambda: __import__(
                "atlas.chat_audit", fromlist=["Identity"]
            ).Identity(REPO, BRANCH, HEAD_A),
        )
        ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
        ctl.mark_session("ROLLOVER_REQUIRED")
        with self.assertRaises(ValidationError):
            ctl.perform_rollover()
        # Even if "approved", PoC stub still refuses unimplemented path.
        approved = StagehandRolloverProvider(provider_approved=True)
        ctl.rollover = approved
        with self.assertRaises(ValidationError):
            ctl.perform_rollover()


if __name__ == "__main__":
    unittest.main()
