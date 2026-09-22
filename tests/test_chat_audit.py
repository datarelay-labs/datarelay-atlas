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
            expected_next = ctl.show()["next_action"]
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
            self.assertEqual(after["next_action"], expected_next)
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

    def test_12_head_advance_does_not_promote_incomplete_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertIsNone(ctl.show()["last_audited_sha"])
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            out = ctl.run_slice()
            self.assertEqual(out["unit"], "changed_code")
            packet = out["packet"]
            self.assertIsNone(packet["last_audited_sha"])
            self.assertEqual(packet["current_target_sha"], HEAD_B)

    def test_13_missing_audit_queue_rejected(self):
        from atlas.chat_audit import AuditControlPacket

        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    "schema_version": 1,
                    "target_repository": REPO,
                    "target_branch": BRANCH,
                    "current_target_sha": HEAD_A,
                    "idempotency_run_key": "abc",
                }
            )
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    "schema_version": 1,
                    "target_repository": REPO,
                    "target_branch": BRANCH,
                    "current_target_sha": HEAD_A,
                    "audit_queue": [],
                    "idempotency_run_key": "abc",
                }
            )
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    "schema_version": 1,
                    "target_repository": REPO,
                    "target_branch": BRANCH,
                    "current_target_sha": HEAD_A,
                    "audit_queue": ["changed_code"],
                    "idempotency_run_key": "abc",
                }
            )

    def test_14_unsupported_outcome_and_empty_finding_fail_closed(self):
        class WeirdExecutor:
            def __init__(self, outcome: str, findings=None):
                self.outcome = outcome
                self.findings = findings or []
                self.calls = []

            def execute(self, packet, unit, audit_request):
                from atlas.chat_audit import AuditEvidence, AuditFinding, SliceResult

                self.calls.append(unit)
                findings = [
                    AuditFinding.from_dict(f) if isinstance(f, dict) else f
                    for f in self.findings
                ]
                return SliceResult(
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    outcome=self.outcome,
                    findings=findings,
                    audit_request=audit_request,
                    evidence=AuditEvidence(
                        status="COMPLETE",
                        unit=unit,
                        target_sha=packet.current_target_sha,
                        notes="looks complete",
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp, executor=WeirdExecutor("REJECTED"))
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "failed_closed")
            self.assertEqual(out["reason"], "unsupported_executor_outcome")

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp, executor=WeirdExecutor("FINDING", findings=[]))
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "failed_closed")
            self.assertEqual(out["reason"], "finding_without_payload")

    def test_15_active_slice_claim_blocks_duplicate_invocation(self):
        import threading

        from atlas.chat_audit import AuditEvidence, SliceResult

        started = threading.Event()
        release = threading.Event()

        class BlockingExecutor:
            def __init__(self):
                self.calls = []

            def execute(self, packet, unit, audit_request):
                self.calls.append(unit)
                started.set()
                release.wait(timeout=5)
                return SliceResult(
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    outcome="PASS",
                    audit_request=audit_request,
                    evidence=AuditEvidence(
                        status="COMPLETE",
                        unit=unit,
                        target_sha=packet.current_target_sha,
                        notes="ok",
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            executor = BlockingExecutor()
            ctl = self._ctl(tmp, executor=executor)
            errors: list[BaseException] = []

            def worker():
                try:
                    ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t = threading.Thread(target=worker)
            t.start()
            self.assertTrue(started.wait(timeout=5))
            with self.assertRaises(ValidationError):
                ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            release.set()
            t.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertEqual(len(executor.calls), 1)

    def test_16_default_evidence_executor_does_not_auto_pass(self):
        from atlas.chat_audit import ExternalEvidenceUnitExecutor

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp, executor=ExternalEvidenceUnitExecutor())
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "awaiting_evidence")
            self.assertEqual(out["packet"]["audit_status"], "AWAITING_EVIDENCE")
            self.assertNotEqual(out["packet"]["audit_status"], "PASSED")

    def test_17_evidence_requires_explicit_unit_and_target(self):
        from atlas.chat_audit import ExternalEvidenceUnitExecutor

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(
                tmp,
                executor=ExternalEvidenceUnitExecutor(
                    {"status": "COMPLETE", "notes": "ok"}
                ),
            )
            with self.assertRaises(ValidationError):
                ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)

    def test_18_expired_executing_claim_is_reclaimable(self):
        import time

        from atlas.chat_audit import AuditControlPacket

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            packet = AuditControlPacket.from_dict(ctl.show())
            packet.audit_status = "IN_SLICE"
            packet.current_unit = "changed_code"
            packet.slice_claim = {
                "claim_id": "old",
                "unit": "changed_code",
                "run_key": packet.idempotency_run_key,
                "target_sha": HEAD_A,
                "state": "executing",
                "claimed_at": time.time() - 10_000,
                "lease_seconds": 900,
            }
            FileCheckpointStore(Path(tmp) / "data").save(packet)
            out = ctl.run_slice()
            self.assertEqual(out["action"], "slice_complete")
            self.assertEqual(out["unit"], "changed_code")

    def test_19_empty_completed_unit_entries_cannot_finalize_pass(self):
        from atlas.chat_audit import AuditControlPacket, build_audit_queue

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            packet = AuditControlPacket.from_dict(ctl.show())
            forged = {
                f"{packet.idempotency_run_key}:{unit}:{HEAD_A}": {}
                for unit in build_audit_queue()
            }
            with self.assertRaises(ValidationError):
                AuditControlPacket.from_dict(
                    {**packet.to_dict(), "completed_units": forged}
                )

    def test_20_head_advance_clears_stale_open_findings(self):
        handoff = RecordingWorkPacketHandoff()
        executor = FixedUnitExecutor(
            outcomes={"changed_code": "FINDING"},
            finding_summaries={"changed_code": "bug"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp, executor=executor, handoff=handoff)
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["outcome"], "FINDING")
            self.assertEqual(len(ctl.show()["open_findings"]), 1)
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            ctl.executor = FixedUnitExecutor()
            nxt = ctl.run_slice()
            self.assertEqual(nxt["unit"], "changed_code")
            self.assertEqual(nxt["packet"]["open_findings"], [])
            # Drain remaining units on HEAD_B and ensure PASSED is reachable.
            while True:
                result = ctl.run_slice()
                if result["action"] == "queue_complete":
                    break
            self.assertEqual(ctl.show()["audit_status"], "PASSED")
            self.assertEqual(ctl.show()["last_audited_sha"], HEAD_B)

    def test_21_finalize_derives_findings_from_completed_units(self):
        from atlas.chat_audit import (
            AuditControlPacket,
            AuditEvidence,
            AuditFinding,
            build_audit_queue,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            packet = AuditControlPacket.from_dict(ctl.show())
            completed = {}
            for unit in build_audit_queue():
                key = f"{packet.idempotency_run_key}:{unit}:{HEAD_A}"
                if unit == "changed_code":
                    finding = AuditFinding(
                        finding_id=f"{packet.idempotency_run_key}:{unit}",
                        unit=unit,
                        summary="persisted finding",
                    )
                    completed[key] = {
                        "unit": unit,
                        "target_sha": HEAD_A,
                        "outcome": "FINDING",
                        "findings": [finding.to_dict()],
                        "evidence": AuditEvidence(
                            status="COMPLETE",
                            unit=unit,
                            target_sha=HEAD_A,
                            notes="persisted finding",
                        ).to_dict(),
                        "audit_request": "",
                    }
                else:
                    completed[key] = {
                        "unit": unit,
                        "target_sha": HEAD_A,
                        "outcome": "PASS",
                        "findings": [],
                        "evidence": AuditEvidence(
                            status="COMPLETE",
                            unit=unit,
                            target_sha=HEAD_A,
                            notes="ok",
                        ).to_dict(),
                        "audit_request": "",
                    }
            restored = AuditControlPacket.from_dict(
                {
                    **packet.to_dict(),
                    "completed_units": completed,
                    "open_findings": [],
                    "audit_status": "IDLE",
                    "current_unit": None,
                }
            )
            FileCheckpointStore(Path(tmp) / "data").save(restored)
            out = ctl.run_slice()
            self.assertEqual(out["action"], "queue_complete")
            self.assertEqual(out["packet"]["audit_status"], "FINDINGS")
            self.assertEqual(len(out["packet"]["open_findings"]), 1)
            self.assertIsNone(out["packet"]["last_audited_sha"])

    def test_22_evidence_rejects_short_sha_prefix(self):
        from atlas.chat_audit import ExternalEvidenceUnitExecutor

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(
                tmp,
                executor=ExternalEvidenceUnitExecutor(
                    {
                        "status": "COMPLETE",
                        "unit": "changed_code",
                        "target_sha": HEAD_A[:7],
                        "notes": "ok",
                        "outcome": "PASS",
                    }
                ),
            )
            with self.assertRaises(ValidationError):
                ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)

    def test_23_full_mode_preserved_across_head_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(
                repository=REPO,
                branch=BRANCH,
                head=HEAD_A,
                mode="full",
            )
            ctl.run_slice()
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            out = ctl.run_slice()
            self.assertEqual(out["packet"]["mode"], "full")

    def test_24_file_handoff_persists_finding(self):
        from atlas.chat_audit import FileWorkPacketHandoff

        with tempfile.TemporaryDirectory() as tmp:
            handoff = FileWorkPacketHandoff(Path(tmp) / "data")
            executor = FixedUnitExecutor(
                outcomes={"changed_code": "FINDING"},
                finding_summaries={"changed_code": "bug"},
            )
            ctl = self._ctl(tmp, executor=executor, handoff=handoff)
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["outcome"], "FINDING")
            self.assertTrue(handoff.handoffs)
            path = Path(handoff.handoffs[0]["handoff_path"])
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
