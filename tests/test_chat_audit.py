"""Continuous Chat Audit Supervisor PoC regressions (ADR-0007)."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from atlas.chat_audit import (
    RESUME_COMMAND,
    ChatAuditController,
    FakeBrowserRolloverProvider,
    FileCheckpointStore,
    FixedCoordinationRefresher,
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
            coordination=FixedCoordinationRefresher(),
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

            # Advance HEAD → fair rotation starts at affected_contracts (index 1).
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            second = ctl.run_slice()
            self.assertEqual(second["action"], "slice_complete")
            self.assertEqual(second["unit"], "affected_contracts")
            packet = second["packet"]
            self.assertEqual(packet["last_audited_sha"], HEAD_A)
            self.assertEqual(packet["current_target_sha"], HEAD_B)
            self.assertEqual(packet["mode"], "delta")
            self.assertEqual(packet["fair_start_index"], 1)
            self.assertIn(
                ("affected_contracts", HEAD_B), executor.calls[len(first_calls) :]
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
            raw.current_unit_index = 0
            raw.slice_claim = {
                "claim_id": "replay-claim",
                "unit": "changed_code",
                "run_key": raw.idempotency_run_key,
                "target_sha": HEAD_A,
                "state": "timed_out",
            }
            store.save(raw)
            replay = ctl.run_slice()
            self.assertEqual(replay["action"], "idempotent_replay")
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["unit"], "changed_code")

    def test_06_non_live_timeout_slice_rotates_on_head_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor(timeout_units={"changed_code"})
            ctl = self._ctl(tmp, executor=executor)
            timed = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(timed["action"], "timeout_checkpointed")
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            advanced = ctl.run_slice()
            self.assertEqual(advanced["action"], "slice_complete")
            self.assertEqual(ctl.show()["current_target_sha"], HEAD_B)
            self.assertFalse(ctl.show()["head_drift"])

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
                coordination=FixedCoordinationRefresher(),
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
            coordination=FixedCoordinationRefresher(),
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
            self.assertEqual(out["unit"], "affected_contracts")
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
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "failed_closed")
            self.assertEqual(out["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(out["reason"], "executor_exception")
            self.assertEqual(out["packet"]["slice_claim"]["state"], "failed")

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
            self.assertEqual(nxt["unit"], "affected_contracts")
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
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "failed_closed")
            self.assertEqual(out["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(out["reason"], "executor_exception")
            self.assertIn(
                "exact 40-char",
                out["packet"]["session"]["notes"],
            )

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

    def test_25_restored_completed_entry_rejects_prefix_sha(self):
        from atlas.chat_audit import (
            AuditEvidence,
            validate_completed_unit_entry,
        )

        unit = "changed_code"
        key = f"runkey:{unit}:{HEAD_A}"
        evidence = AuditEvidence(
            status="COMPLETE",
            unit=unit,
            target_sha=HEAD_A[:7],
            notes="ok",
        ).to_dict()
        entry = {
            "unit": unit,
            "target_sha": HEAD_A[:7],
            "outcome": "PASS",
            "findings": [],
            "evidence": evidence,
            "audit_request": "",
        }
        with self.assertRaises(ValidationError):
            validate_completed_unit_entry(
                key, entry, expected_run_key="runkey", expected_target_sha=HEAD_A
            )

    def test_26_restored_completed_evidence_rejects_prefix_sha(self):
        from atlas.chat_audit import (
            AuditEvidence,
            validate_completed_unit_entry,
        )

        unit = "changed_code"
        key = f"runkey:{unit}:{HEAD_A}"
        evidence = AuditEvidence(
            status="COMPLETE",
            unit=unit,
            target_sha=HEAD_A[:7],
            notes="ok",
        ).to_dict()
        entry = {
            "unit": unit,
            "target_sha": HEAD_A,
            "outcome": "PASS",
            "findings": [],
            "evidence": evidence,
            "audit_request": "",
        }
        with self.assertRaises(ValidationError):
            validate_completed_unit_entry(
                key, entry, expected_run_key="runkey", expected_target_sha=HEAD_A
            )

    def test_27_handoff_filenames_collision_resistant_and_idempotent(self):
        from atlas.chat_audit import (
            AuditControlPacket,
            AuditFinding,
            FileWorkPacketHandoff,
            handoff_filename_for_finding_id,
        )

        self.assertNotEqual(
            handoff_filename_for_finding_id("find.a"),
            handoff_filename_for_finding_id("find_a"),
        )
        with self.assertRaises(ValidationError):
            AuditFinding.from_dict(
                {
                    "finding_id": "a/b",
                    "unit": "changed_code",
                    "summary": "bad id",
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            handoff = FileWorkPacketHandoff(Path(tmp) / "data")
            packet = AuditControlPacket(
                target_repository=REPO,
                target_branch=BRANCH,
                current_target_sha=HEAD_A,
                audit_queue=[
                    "changed_code",
                    "affected_contracts",
                    "affected_tests_ci",
                    "security_impact",
                    "docs_spec_drift",
                ],
                idempotency_run_key="runkey",
            )
            f1 = AuditFinding(
                finding_id="find.a", unit="changed_code", summary="one"
            )
            f2 = AuditFinding(
                finding_id="find_a", unit="changed_code", summary="two"
            )
            r1 = handoff.upsert_implementation_packet(packet, f1)
            r2 = handoff.upsert_implementation_packet(packet, f2)
            path1 = Path(r1["handoff_path"])
            path2 = Path(r2["handoff_path"])
            self.assertNotEqual(path1, path2)
            self.assertTrue(path1.exists())
            self.assertTrue(path2.exists())
            self.assertEqual(json.loads(path1.read_text())["finding_id"], "find.a")
            self.assertEqual(json.loads(path2.read_text())["finding_id"], "find_a")
            # Same-ID upsert is stable/idempotent (same path, overwrite in place).
            r1b = handoff.upsert_implementation_packet(packet, f1)
            self.assertEqual(Path(r1b["handoff_path"]), path1)
            self.assertEqual(json.loads(path1.read_text())["finding_id"], "find.a")
            self.assertEqual(json.loads(path2.read_text())["finding_id"], "find_a")

    def test_28_durable_files_never_persist_raw_secrets(self):
        """Checkpoint + handoff must not retain Basic/Bearer/URI/password secrets."""
        from atlas.chat_audit import (
            ExternalEvidenceUnitExecutor,
            FileWorkPacketHandoff,
        )

        basic = "Authorization: Basic dXNlcjpwYXNz"
        bearer = "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz012345"
        db_url = "DATABASE_URL=postgresql://audit_user:s3cret-pass@db.example/app"
        password_assign = "POSTGRES_PASSWORD=hunter2-literal"
        notes = f"{basic}\n{bearer}\n{db_url}\n{password_assign}"
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            handoff = FileWorkPacketHandoff(data)
            executor = ExternalEvidenceUnitExecutor(
                {
                    "status": "COMPLETE",
                    "unit": "changed_code",
                    "target_sha": HEAD_A,
                    "notes": notes,
                    "outcome": "FINDING",
                    "truncated": False,
                    "findings": [
                        {
                            "finding_id": "sec-1",
                            "unit": "changed_code",
                            "summary": notes,
                            "severity": "P1",
                        }
                    ],
                }
            )
            ctl = self._ctl(tmp, executor=executor, handoff=handoff)
            # Point store at same data root as handoff
            ctl.store = FileCheckpointStore(data)
            ctl.handoff = handoff
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["outcome"], "FINDING")
            checkpoint_text = (data / "chat-audit.json").read_text(encoding="utf-8")
            handoff_files = list((data / "chat-audit-handoffs").glob("*.json"))
            self.assertTrue(handoff_files)
            handoff_text = handoff_files[0].read_text(encoding="utf-8")
            for blob in (checkpoint_text, handoff_text):
                self.assertNotIn("dXNlcjpwYXNz", blob)
                self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz012345", blob)
                self.assertNotIn("s3cret-pass", blob)
                self.assertNotIn("hunter2-literal", blob)
                self.assertNotIn("audit_user:s3cret-pass@", blob)
                self.assertIn("<redacted>", blob)

    def test_29_multiline_quoted_credential_redacted(self):
        from atlas.secrets import redact_sensitive_audit_text, contains_unsafe_secret

        raw = 'OPENAI_API_KEY="sk-live-abcdefghijklmnopqrstuvwxyz012345\nstill-secret"'
        cleaned = redact_sensitive_audit_text(raw)
        self.assertNotIn("sk-live-", cleaned)
        self.assertNotIn("still-secret", cleaned)
        self.assertFalse(contains_unsafe_secret(cleaned))

    def test_30_finding_id_and_severity_reject_secrets_and_unknown(self):
        from atlas.chat_audit import AuditFinding

        with self.assertRaises(ValidationError):
            AuditFinding.from_dict(
                {
                    "finding_id": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
                    "unit": "changed_code",
                    "summary": "x",
                }
            )
        with self.assertRaises(ValidationError):
            AuditFinding.from_dict(
                {
                    "finding_id": "ok-1",
                    "unit": "changed_code",
                    "summary": "x",
                    "severity": "CRITICAL",
                }
            )
        for finding_id in (
            "OPENAI_API_KEY:hunter2",
            "API_KEY:hunter2",
            "ACCESS_KEY:hunter2",
            "PASSWORD:hunter2",
        ):
            with self.assertRaises(ValidationError):
                AuditFinding.from_dict(
                    {
                        "finding_id": finding_id,
                        "unit": "changed_code",
                        "summary": "x",
                    }
                )
        accepted = AuditFinding.from_dict(
            {
                "finding_id": "changed-code-note",
                "unit": "changed_code",
                "summary": "x",
            }
        )
        self.assertEqual(accepted.finding_id, "changed-code-note")

    def test_31_uppercase_completed_unit_key_canonicalized(self):
        from atlas.chat_audit import AuditControlPacket, AuditEvidence, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        upper_key = f"{run_key}:changed_code:{HEAD_A.upper()}"
        packet = AuditControlPacket.from_dict(
            {
                "schema_version": 1,
                "target_repository": REPO,
                "target_branch": BRANCH,
                "current_target_sha": HEAD_A,
                "audit_status": "IDLE",
                "audit_queue": [
                    "changed_code",
                    "affected_contracts",
                    "affected_tests_ci",
                    "security_impact",
                    "docs_spec_drift",
                ],
                "idempotency_run_key": run_key,
                "completed_units": {
                    upper_key: {
                        "unit": "changed_code",
                        "target_sha": HEAD_A,
                        "outcome": "PASS",
                        "findings": [],
                        "evidence": AuditEvidence(
                            status="COMPLETE",
                            unit="changed_code",
                            target_sha=HEAD_A,
                            notes="ok",
                        ).to_dict(),
                        "audit_request": "",
                    }
                },
            }
        )
        canon = f"{run_key}:changed_code:{HEAD_A}"
        self.assertIn(canon, packet.completed_units)
        self.assertNotIn(upper_key, packet.completed_units)

    def test_32_restored_prefix_sha_rejected(self):
        from atlas.chat_audit import AuditControlPacket, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A[:12],
            "audit_status": "IDLE",
            "audit_queue": [
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            "idempotency_run_key": run_key,
        }
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(base)
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    **base,
                    "current_target_sha": HEAD_A,
                    "last_audited_sha": HEAD_B[:12],
                    "idempotency_run_key": make_run_key(REPO, BRANCH, HEAD_A),
                }
            )

    def test_33_malformed_restored_state_fail_closed(self):
        from atlas.chat_audit import AuditControlPacket, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A,
            "audit_status": "IN_SLICE",
            "audit_queue": [
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            "idempotency_run_key": run_key,
            "current_unit": "not_in_queue",
            "current_unit_index": 0,
        }
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(base)
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    **base,
                    "current_unit": "changed_code",
                    "mode": "weird",
                }
            )
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    **base,
                    "current_unit": "changed_code",
                    "idempotency_run_key": "arbitrary-not-derived",
                }
            )

    def test_34_timeout_resume_rejects_malformed_checkpoint(self):
        from atlas.chat_audit import AuditControlPacket, make_run_key

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            path = Path(tmp) / "data" / "chat-audit.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["audit_status"] = "IN_SLICE"
            raw["current_unit"] = "not_in_queue"
            raw["current_unit_index"] = 0
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValidationError):
                ctl.run_slice()
            # Sanity: well-formed timeout resume still works.
            run_key = make_run_key(REPO, BRANCH, HEAD_A)
            good = AuditControlPacket.from_dict(
                {
                    **raw,
                    "current_unit": "changed_code",
                    "current_unit_index": 0,
                    "idempotency_run_key": run_key,
                    "slice_claim": {
                        "claim_id": "c1",
                        "unit": "changed_code",
                        "run_key": run_key,
                        "target_sha": HEAD_A,
                        "state": "timed_out",
                    },
                }
            )
            FileCheckpointStore(Path(tmp) / "data").save(good)
            out = ctl.run_slice()
            self.assertIn(out["action"], {"slice_complete", "timeout_checkpointed"})

    def test_35_fake_identity_without_worktree_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(Path(tmp) / "data")
            ctl = ChatAuditController(
                store,
                executor=FixedUnitExecutor(),
                coordination=FixedCoordinationRefresher(),
                allow_trusted_identity=False,
            )
            with self.assertRaises(ValidationError):
                ctl.run_slice(
                    repository="evil/fake-repo",
                    branch="main",
                    head=HEAD_A,
                )

    def test_36_worktree_identity_rejects_fake_asserted_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(Path(tmp) / "data")
            real_head = {"head": HEAD_A}

            def identity():
                from atlas.chat_audit import Identity

                return Identity(
                    repository=REPO, branch=BRANCH, head=real_head["head"]
                )

            ctl = ChatAuditController(
                store,
                executor=FixedUnitExecutor(),
                coordination=FixedCoordinationRefresher(),
                identity_resolver=identity,
            )
            with self.assertRaises(ValidationError):
                ctl.run_slice(
                    repository=REPO,
                    branch=BRANCH,
                    head=HEAD_B,  # fake asserted SHA
                )

    def test_37_finding_unit_mismatch_refused_before_handoff(self):
        from atlas.chat_audit import ExternalEvidenceUnitExecutor

        handoff = RecordingWorkPacketHandoff()
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(
                tmp,
                executor=ExternalEvidenceUnitExecutor(
                    {
                        "status": "COMPLETE",
                        "unit": "changed_code",
                        "target_sha": HEAD_A,
                        "notes": "ok",
                        "outcome": "FINDING",
                        "truncated": False,
                        "findings": [
                            {
                                "finding_id": "mismatch-1",
                                "unit": "security_impact",
                                "summary": "wrong unit",
                                "severity": "P2",
                            }
                        ],
                    }
                ),
                handoff=handoff,
            )
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(handoff.handoffs, [])
            # Immediate retry/reconcile is allowed (failed claim, not lease-blocked).
            retry = ctl.run_slice()
            self.assertIn(
                retry["action"],
                {"failed_closed", "slice_complete", "awaiting_evidence"},
            )

    def test_38_executor_exception_reconciles_for_immediate_retry(self):
        class BoomExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, packet, unit, audit_request):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("executor boom OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123")
                from atlas.chat_audit import AuditEvidence, SliceResult

                return SliceResult(
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    outcome="PASS",
                    evidence=AuditEvidence(
                        status="COMPLETE",
                        unit=unit,
                        target_sha=packet.current_target_sha,
                        notes="recovered",
                    ),
                    audit_request=audit_request,
                )

        with tempfile.TemporaryDirectory() as tmp:
            boom = BoomExecutor()
            ctl = self._ctl(tmp, executor=boom)  # type: ignore[arg-type]
            first = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(first["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(
                first["packet"]["slice_claim"]["state"], "failed"
            )
            notes = first["packet"]["session"]["notes"]
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz0123", notes)
            second = ctl.run_slice()
            self.assertEqual(second["action"], "slice_complete")
            self.assertEqual(second["outcome"], "PASS")
            self.assertEqual(boom.calls, 2)

    def test_39_handoff_exception_reconciles_without_duplicate_side_effects(self):
        class FlakyHandoff:
            def __init__(self) -> None:
                self.calls = 0
                self.handoffs: list[dict] = []

            def upsert_implementation_packet(self, packet, finding):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("github write unavailable")
                record = {
                    "action": "created",
                    "finding": finding.to_dict(),
                    "issue_number": 999,
                }
                self.handoffs.append(record)
                return record

        executor = FixedUnitExecutor(
            outcomes={"changed_code": "FINDING"},
            finding_summaries={"changed_code": "bug"},
        )
        flaky = FlakyHandoff()
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp, executor=executor, handoff=flaky)  # type: ignore[arg-type]
            first = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(first["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(flaky.calls, 1)
            self.assertEqual(flaky.handoffs, [])
            self.assertEqual(ctl.show()["open_findings"], [])
            second = ctl.run_slice()
            self.assertEqual(second["action"], "slice_complete")
            self.assertEqual(second["outcome"], "FINDING")
            self.assertEqual(len(flaky.handoffs), 1)
            self.assertEqual(flaky.calls, 2)

    def test_40_github_checkpoint_and_handoff_adapters_roundtrip(self):
        import base64
        import hashlib
        import subprocess

        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import (
            GitHubAIWorkHandoff,
            GitHubContentsCheckpointStore,
        )

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
        )
        state: dict[str, Any] = {"sha": None, "raw": None}

        claims: dict[str, dict] = {}

        def runner(argv: list[str], cwd: str):
            # Control-branch bootstrap metadata.
            if argv[:2] == ["gh", "api"] and "repos/" + REPO == argv[-1]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in argv[-1]:
                if argv[-1].endswith("/atlas/chat-audit-control"):
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps({"object": {"sha": "1"*40}}), stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1"*40}}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "--method" not in argv and "/contents/" in " ".join(argv):
                endpoint = argv[-1]
                if "handoff-claims" in endpoint:
                    key = endpoint.split("/contents/")[-1].split("?")[0]
                    if key not in claims:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                        )
                    raw = claims[key]["raw"]
                    encoded = base64.b64encode(raw.encode()).decode()
                    return subprocess.CompletedProcess(
                        argv, 0,
                        stdout=json.dumps({"type":"file","sha":claims[key]["sha"],"content":encoded,"encoding":"base64"}),
                        stderr="",
                    )
                if state["sha"] is None:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                encoded = base64.b64encode(
                    str(state["raw"]).encode("utf-8")
                ).decode("ascii")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "file",
                            "sha": state["sha"],
                            "content": encoded,
                            "encoding": "base64",
                        }
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                endpoint = ""
                for a in argv:
                    if a.startswith("repos/"):
                        endpoint = a
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                if "git/refs" in endpoint:
                    return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
                if "handoff-claims" in endpoint:
                    key = endpoint.split("/contents/")[-1]
                    expected = body.get("sha")
                    if key in claims and claims[key]["sha"] != expected:
                        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: HTTP 409")
                    raw = base64.b64decode(body["content"]).decode()
                    new_sha = hashlib.sha1(raw.encode()).hexdigest()
                    claims[key] = {"raw": raw, "sha": new_sha}
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps({"content": {"sha": new_sha}}), stderr=""
                    )
                expected = body.get("sha")
                if state["sha"] is None and expected:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="gh: HTTP 409"
                    )
                if state["sha"] is not None and expected != state["sha"]:
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout="",
                        stderr=(
                            f'gh: HTTP 409 {{"message":"is at {state["sha"]} '
                            f'but expected {expected}"}}'
                        ),
                    )
                if state["sha"] is not None and expected is None:
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout="",
                        stderr='gh: HTTP 422 {"message":"sha wasn\'t supplied"}',
                    )
                raw = base64.b64decode(body["content"]).decode("utf-8")
                new_sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
                state["raw"] = raw
                state["sha"] = new_sha
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"content": {"sha": new_sha}}),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="[]", stderr=""
                )
            if argv[:3] == ["gh", "issue", "create"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="https://github.com/datarelay-labs/datarelay-atlas/issues/321\n",
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "view"]:
                number = int(argv[3])
                body = (
                    f"<!-- atlas-chat-audit-finding-id:gh-1 -->\n"
                    f"TARGET_REPO={REPO}\nSTATUS=ACTIVE\n"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": number,
                            "title": "[AI Work] Audit finding: gh-1",
                            "state": "OPEN",
                            "body": body,
                            "url": f"https://github.com/{REPO}/issues/{number}",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCheckpointStore(Path(tmp) / "cache")
            store = GitHubContentsCheckpointStore(
                repository=REPO,
                issue_number=20,
                cache=cache,
                command_runner=runner,
            )
            self.assertIsNone(store.load())
            store.save(packet)
            loaded = store.load()
            assert loaded is not None
            self.assertEqual(loaded.idempotency_run_key, run_key)
            self.assertEqual(loaded.canonical_revision, 1)
            self.assertTrue(cache.path.exists())

            handoff = GitHubAIWorkHandoff(
                repository=REPO, command_runner=runner
            )
            finding = AuditFinding(
                finding_id="gh-1",
                unit="changed_code",
                summary="prove github handoff",
                severity="P2",
            )
            record = handoff.upsert_implementation_packet(packet, finding)
            self.assertEqual(record["action"], "created")
            self.assertEqual(record["issue_number"], 321)

            def runner2(argv: list[str], cwd: str):
                import subprocess

                if argv[:2] == ["gh", "api"] and "repos/" + REPO == argv[-1]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"default_branch": "main"}),
                        stderr="",
                    )
                if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in " ".join(argv):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"object": {"sha": "1" * 40}}),
                        stderr="",
                    )
                if argv[:2] == ["gh", "api"] and "/contents/" in " ".join(argv):
                    # Claim already finalized by first handoff.
                    for key, val in claims.items():
                        if key in " ".join(argv):
                            encoded = base64.b64encode(val["raw"].encode()).decode()
                            return subprocess.CompletedProcess(
                                argv,
                                0,
                                stdout=json.dumps(
                                    {
                                        "type": "file",
                                        "sha": val["sha"],
                                        "content": encoded,
                                    }
                                ),
                                stderr="",
                            )
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                if argv[:3] == ["gh", "issue", "edit"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="", stderr=""
                    )
                if argv[:3] == ["gh", "issue", "view"]:
                    number = int(argv[3])
                    body = (
                        f"<!-- atlas-chat-audit-finding-id:gh-1 -->\n"
                        f"TARGET_REPO={REPO}\nSTATUS=ACTIVE\n"
                    )
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            {
                                "number": number,
                                "title": "[AI Work] Audit finding: gh-1",
                                "state": "OPEN",
                                "body": body,
                                "url": f"https://github.com/{REPO}/issues/{number}",
                            }
                        ),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="unexpected"
                )

            handoff2 = GitHubAIWorkHandoff(
                repository=REPO, command_runner=runner2
            )
            updated = handoff2.upsert_implementation_packet(packet, finding)
            self.assertEqual(updated["action"], "updated")
            self.assertEqual(updated["issue_number"], 321)

    def test_41_github_contents_cas_rejects_stale_independent_writer(self):
        """Sequential stale writer must lose after the first Contents PUT."""
        import base64
        import hashlib
        import subprocess
        import threading

        from atlas.chat_audit import (
            AuditControlPacket,
            CheckpointCasConflict,
            make_run_key,
        )
        from atlas.chat_audit_github import GitHubContentsCheckpointStore

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
            canonical_revision=1,
            next_action="writer-base",
        )
        raw0 = json.dumps(base.to_dict(), indent=2, sort_keys=True) + "\n"
        state = {
            "sha": hashlib.sha1(raw0.encode("utf-8")).hexdigest(),
            "raw": raw0,
            "puts": 0,
            "lock": threading.Lock(),
        }

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and "--method" not in argv:
                encoded = base64.b64encode(state["raw"].encode("utf-8")).decode(
                    "ascii"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "file",
                            "sha": state["sha"],
                            "content": encoded,
                            "encoding": "base64",
                        }
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                with state["lock"]:
                    expected = body.get("sha")
                    if expected != state["sha"]:
                        return subprocess.CompletedProcess(
                            argv,
                            1,
                            stdout="",
                            stderr=(
                                f'gh: HTTP 409 {{"message":"is at {state["sha"]} '
                                f'but expected {expected}"}}'
                            ),
                        )
                    raw = base64.b64decode(body["content"]).decode("utf-8")
                    new_sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
                    state["raw"] = raw
                    state["sha"] = new_sha
                    state["puts"] += 1
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"content": {"sha": new_sha}}),
                        stderr="",
                    )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        store_a = GitHubContentsCheckpointStore(
            repository=REPO, issue_number=20, command_runner=runner
        )
        store_b = GitHubContentsCheckpointStore(
            repository=REPO, issue_number=20, command_runner=runner
        )
        loaded_a = store_a.load()
        loaded_b = store_b.load()
        assert loaded_a is not None and loaded_b is not None
        self.assertEqual(loaded_a.canonical_revision, 1)
        self.assertEqual(store_a._cas_blob_sha, store_b._cas_blob_sha)

        loaded_a.next_action = "writer-one"
        store_a.save(loaded_a)
        self.assertEqual(state["puts"], 1)
        self.assertEqual(loaded_a.canonical_revision, 2)
        after_a = json.loads(state["raw"])
        self.assertEqual(after_a["next_action"], "writer-one")

        loaded_b.next_action = "writer-two"
        with self.assertRaises(CheckpointCasConflict):
            store_b.save(loaded_b)
        self.assertEqual(state["puts"], 1)
        after_b = json.loads(state["raw"])
        self.assertEqual(after_b["next_action"], "writer-one")
        self.assertEqual(after_b["canonical_revision"], 2)

    def test_42_cas_conflict_during_run_slice_is_human_required(self):
        from atlas.chat_audit import (
            AuditControlPacket,
            ChatAuditController,
            CheckpointCasConflict,
            FixedUnitExecutor,
            MemoryCheckpointStore,
            make_run_key,
        )

        class CasFailStore(MemoryCheckpointStore):
            def save(self, packet):
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: contents blob SHA "
                    "conflict (stale or concurrent writer)"
                )

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
            canonical_revision=1,
        )
        ctl = ChatAuditController(
            store=CasFailStore(packet),
            executor=FixedUnitExecutor(),
            coordination=FixedCoordinationRefresher(),
            allow_trusted_identity=True,
        )
        result = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
        self.assertEqual(result["action"], "failed_closed")
        self.assertEqual(result["outcome"], "HUMAN_REQUIRED")
        self.assertEqual(result["reason"], "checkpoint_cas_conflict")

    def test_43_concurrent_writers_barrier_exactly_one_contents_put_wins(self):
        """Both stores finish load before either PUT; server CAS allows one win."""
        import base64
        import hashlib
        import subprocess
        import threading

        from atlas.chat_audit import (
            AuditControlPacket,
            CheckpointCasConflict,
            make_run_key,
        )
        from atlas.chat_audit_github import GitHubContentsCheckpointStore

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
            canonical_revision=1,
            next_action="writer-base",
        )
        raw0 = json.dumps(base.to_dict(), indent=2, sort_keys=True) + "\n"
        state = {
            "sha": hashlib.sha1(raw0.encode("utf-8")).hexdigest(),
            "raw": raw0,
            "puts_attempted": 0,
            "puts_succeeded": 0,
            "lock": threading.Lock(),
        }
        put_barrier = threading.Barrier(2, timeout=5)
        load_barrier = threading.Barrier(2, timeout=5)

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and "--method" not in argv:
                encoded = base64.b64encode(state["raw"].encode("utf-8")).decode(
                    "ascii"
                )
                sha = state["sha"]
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "file",
                            "sha": sha,
                            "content": encoded,
                            "encoding": "base64",
                        }
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                # Force both writers to enter PUT holding the same loaded SHA.
                put_barrier.wait()
                with state["lock"]:
                    state["puts_attempted"] += 1
                    expected = body.get("sha")
                    if expected != state["sha"]:
                        return subprocess.CompletedProcess(
                            argv,
                            1,
                            stdout="",
                            stderr=(
                                f'gh: HTTP 409 {{"message":"is at {state["sha"]} '
                                f'but expected {expected}"}}'
                            ),
                        )
                    raw = base64.b64decode(body["content"]).decode("utf-8")
                    new_sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
                    state["raw"] = raw
                    state["sha"] = new_sha
                    state["puts_succeeded"] += 1
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"content": {"sha": new_sha}}),
                        stderr="",
                    )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        store_a = GitHubContentsCheckpointStore(
            repository=REPO, issue_number=20, command_runner=runner
        )
        store_b = GitHubContentsCheckpointStore(
            repository=REPO, issue_number=20, command_runner=runner
        )
        loaded: dict[str, AuditControlPacket | None] = {"a": None, "b": None}
        errors: list[BaseException] = []

        def load_a():
            loaded["a"] = store_a.load()
            load_barrier.wait()

        def load_b():
            loaded["b"] = store_b.load()
            load_barrier.wait()

        t1 = threading.Thread(target=load_a)
        t2 = threading.Thread(target=load_b)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertIsNotNone(loaded["a"])
        self.assertIsNotNone(loaded["b"])
        assert loaded["a"] is not None and loaded["b"] is not None
        self.assertEqual(store_a._cas_blob_sha, store_b._cas_blob_sha)
        self.assertEqual(loaded["a"].canonical_revision, 1)

        loaded["a"].next_action = "writer-one"
        loaded["b"].next_action = "writer-two"
        outcomes: dict[str, str] = {}

        def save_a():
            try:
                store_a.save(loaded["a"])  # type: ignore[arg-type]
                outcomes["a"] = "ok"
            except CheckpointCasConflict as exc:
                outcomes["a"] = f"conflict:{exc}"
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                outcomes["a"] = f"error:{exc}"

        def save_b():
            try:
                store_b.save(loaded["b"])  # type: ignore[arg-type]
                outcomes["b"] = "ok"
            except CheckpointCasConflict as exc:
                outcomes["b"] = f"conflict:{exc}"
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                outcomes["b"] = f"error:{exc}"

        s1 = threading.Thread(target=save_a)
        s2 = threading.Thread(target=save_b)
        s1.start()
        s2.start()
        s1.join(timeout=5)
        s2.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(state["puts_attempted"], 2)
        self.assertEqual(state["puts_succeeded"], 1)
        winners = [k for k, v in outcomes.items() if v == "ok"]
        losers = [k for k, v in outcomes.items() if v.startswith("conflict:")]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        final = json.loads(state["raw"])
        self.assertEqual(final["canonical_revision"], 2)
        self.assertIn(final["next_action"], {"writer-one", "writer-two"})
        self.assertEqual(
            final["next_action"],
            "writer-one" if winners[0] == "a" else "writer-two",
        )


    def test_44_cheap_path_fails_closed_on_coordination_rework(self):
        from atlas.chat_audit import FixedCoordinationRefresher

        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor()
            ctl = self._ctl(tmp, executor=executor)
            ctl.coordination = FixedCoordinationRefresher(
                {
                    "status": "HUMAN_REQUIRED",
                    "outcome": "HUMAN_REQUIRED",
                    "reasons": ["actionable_review"],
                }
            )
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            while ctl.run_slice()["action"] != "queue_complete":
                pass
            cheap = ctl.run_slice()
            self.assertEqual(cheap["action"], "cheap_no_change_rework")
            self.assertEqual(cheap["outcome"], "HUMAN_REQUIRED")
            self.assertIn("actionable_review", cheap["reason"])
            self.assertEqual(ctl.show()["audit_status"], "FINDINGS")
            self.assertIsNotNone(ctl.show()["last_coordination_refresh"])

    def test_45_head_advance_fair_rotation_avoids_starvation(self):
        heads = [f"{i:040x}" for i in range(1, 8)]
        executed = []
        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor()
            ctl = self._ctl(tmp, head=heads[0], executor=executor)
            ctl.initialize(repository=REPO, branch=BRANCH, head=heads[0])
            for head in heads:
                ctl._test_head["head"] = head  # type: ignore[attr-defined]
                out = ctl.run_slice()
                self.assertEqual(out["action"], "slice_complete")
                executed.append(out["unit"])
        # Across > queue-length HEAD advances, every default unit class appears.
        for unit in [
            "changed_code",
            "affected_contracts",
            "affected_tests_ci",
            "security_impact",
            "docs_spec_drift",
        ]:
            self.assertIn(unit, executed)
        self.assertNotEqual(executed, ["changed_code"] * len(executed))

    def test_46_failed_contents_put_does_not_poison_local_revision(self):
        import base64
        import hashlib
        import subprocess

        from atlas.chat_audit import AuditControlPacket, make_run_key
        from atlas.chat_audit_github import GitHubContentsCheckpointStore
        from atlas.provenance import ValidationError

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
            canonical_revision=0,
        )
        state = {"sha": None, "raw": None, "fail_once": True}

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and "repos/" + REPO == argv[-1]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in argv[-1]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1"*40}}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "--method" not in argv and "/contents/" in " ".join(argv):
                if state["sha"] is None:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                encoded = base64.b64encode(state["raw"].encode()).decode()
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=json.dumps({"type":"file","sha":state["sha"],"content":encoded}),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                endpoint = " ".join(argv)
                if "git/refs" in endpoint:
                    return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                if state["fail_once"]:
                    state["fail_once"] = False
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="gh: HTTP 502 transient"
                    )
                raw = base64.b64decode(body["content"]).decode()
                new_sha = hashlib.sha1(raw.encode()).hexdigest()
                state["raw"] = raw
                state["sha"] = new_sha
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"content": {"sha": new_sha}}), stderr=""
                )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        store = GitHubContentsCheckpointStore(
            repository=REPO, issue_number=20, command_runner=runner
        )
        self.assertIsNone(store.load())
        with self.assertRaises(ValidationError):
            store.save(packet)
        self.assertEqual(packet.canonical_revision, 0)
        store.save(packet)
        self.assertEqual(packet.canonical_revision, 1)


    def test_47_concurrent_handoff_claim_cas_one_create(self):
        """Barrier: two handoffs for same finding; exactly one issue create."""
        import base64
        import hashlib
        import subprocess
        import threading

        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import GitHubAIWorkHandoff

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
        )
        finding = AuditFinding(
            finding_id="same-id",
            unit="changed_code",
            summary="dup",
            severity="P2",
        )
        claims: dict[str, dict] = {}
        creates = {"n": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}":
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1"*40}}), stderr=""
                )
            joined = " ".join(argv)
            if argv[:2] == ["gh", "api"] and "--method" not in argv and "/contents/" in joined:
                key = joined.split("/contents/")[-1].split("?")[0]
                with lock:
                    if key not in claims:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                        )
                    raw = claims[key]["raw"]
                    encoded = base64.b64encode(raw.encode()).decode()
                    return subprocess.CompletedProcess(
                        argv, 0,
                        stdout=json.dumps({"type":"file","sha":claims[key]["sha"],"content":encoded}),
                        stderr="",
                    )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                if "git/refs" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                key = [a for a in argv if a.startswith("repos/")][0].split("/contents/")[-1]
                # Synchronize only claim *creates* (no expected sha).
                if body.get("sha") is None:
                    barrier.wait()
                with lock:
                    expected = body.get("sha")
                    if key in claims and claims[key]["sha"] != expected:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="gh: HTTP 409"
                        )
                    if key not in claims and expected:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="gh: HTTP 409"
                        )
                    raw = base64.b64decode(body["content"]).decode()
                    new_sha = hashlib.sha1(raw.encode()).hexdigest()
                    claims[key] = {"raw": raw, "sha": new_sha}
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps({"content": {"sha": new_sha}}), stderr=""
                    )
            if argv[:3] == ["gh", "issue", "create"]:
                with lock:
                    creates["n"] += 1
                    n = 900 + creates["n"]
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=f"https://github.com/{REPO}/issues/{n}\n",
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "list"]:
                with lock:
                    if creates["n"] == 0:
                        return subprocess.CompletedProcess(
                            argv, 0, stdout="[]", stderr=""
                        )
                    n = 900 + creates["n"]
                    marker = "<!-- atlas-chat-audit-finding-id:same-id -->"
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                {
                                    "number": n,
                                    "title": "[AI Work] Audit finding: same-id",
                                    "body": f"{marker}\nTARGET_REPO={REPO}\nSTATUS=ACTIVE\n",
                                    "url": f"https://github.com/{REPO}/issues/{n}",
                                }
                            ]
                        ),
                        stderr="",
                    )
            if argv[:3] == ["gh", "issue", "view"]:
                number = int(argv[3])
                body = (
                    f"<!-- atlas-chat-audit-finding-id:same-id -->\n"
                    f"TARGET_REPO={REPO}\nSTATUS=ACTIVE\n"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": number,
                            "title": "[AI Work] Audit finding: same-id",
                            "state": "OPEN",
                            "body": body,
                            "url": f"https://github.com/{REPO}/issues/{number}",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected:"+str(argv[:6]))

        a = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        b = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        outcomes = {}
        errors = []

        def run_a():
            try:
                outcomes["a"] = a.upsert_implementation_packet(packet, finding)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                outcomes["a"] = {"error": str(exc)}

        def run_b():
            try:
                outcomes["b"] = b.upsert_implementation_packet(packet, finding)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                outcomes["b"] = {"error": str(exc)}

        t1 = threading.Thread(target=run_a)
        t2 = threading.Thread(target=run_b)
        t1.start(); t2.start(); t1.join(5); t2.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(creates["n"], 1)
        actions = {outcomes["a"].get("action"), outcomes["b"].get("action")}
        self.assertEqual(actions, {"created", "updated"})

    def test_48_discover_checkpoint_issue_from_active_workstream(self):
        import subprocess
        from atlas.chat_audit_github import discover_checkpoint_issue

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and "/contents/" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                )
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "number": 20,
                                "title": "[AI Work] Continuous Chat Audit",
                                "body": (
                                    "WORKSTREAM=continuous-chat-audit-supervisor-poc\n"
                                    "STATUS=ACTIVE\n"
                                ),
                            }
                        ]
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        self.assertEqual(
            discover_checkpoint_issue(repository=REPO, command_runner=runner),
            20,
        )


    def test_49_mark_session_refuses_fake_repo_against_worktree(self):
        """Mutating session paths must bind to authoritative worktree identity."""
        from atlas.chat_audit import (
            AuditControlPacket,
            ChatAuditController,
            FileCheckpointStore,
            Identity,
            make_run_key,
        )

        git_calls: list[list[str]] = []

        def git_runner(argv: list[str], cwd: str) -> str:
            git_calls.append(list(argv))
            raise AssertionError(f"git should not be needed after identity resolve: {argv}")

        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(Path(tmp) / "data")
            # Authoritative identity is the real Atlas repo/branch/HEAD.
            def identity():
                return Identity(repository=REPO, branch=BRANCH, head=HEAD_A)

            fake = AuditControlPacket(
                target_repository="fake-owner/fake-repo",
                target_branch="fake-branch",
                current_target_sha=HEAD_B,
                audit_queue=[
                    "changed_code",
                    "affected_contracts",
                    "affected_tests_ci",
                    "security_impact",
                    "docs_spec_drift",
                ],
                idempotency_run_key=make_run_key(
                    "fake-owner/fake-repo", "fake-branch", HEAD_B
                ),
            )
            store.save(fake)
            ctl = ChatAuditController(
                store,
                identity_resolver=identity,
                git_runner=git_runner,
                worktree_path=tmp,
                enforce_worktree_identity=True,
                coordination=FixedCoordinationRefresher(),
            )
            with self.assertRaises(ValidationError) as ctx:
                ctl.mark_session("STALLED", notes="should-fail")
            self.assertIn("repository", str(ctx.exception).lower())
            self.assertEqual(store.load().session.state, "ACTIVE")
            # show/resume also refuse fake packet against real identity
            with self.assertRaises(ValidationError):
                ctl.show()
            with self.assertRaises(ValidationError):
                ctl.resume_instruction_payload()
            with self.assertRaises(ValidationError):
                ctl.perform_rollover()
            # No git calls: identity_resolver supplied authority.
            self.assertEqual(git_calls, [])

    def test_50_unquoted_credential_suffix_fully_redacted(self):
        from atlas.secrets import (
            contains_unsafe_secret,
            redact_sensitive_audit_text,
            sanitize_durable_text,
        )

        samples = [
            "PASSWORD=hunter2 more-secret",
            "token=abc123 extra-sensitive-fragment",
            "CLIENT_SECRET=first second-third",
        ]
        for raw in samples:
            cleaned = redact_sensitive_audit_text(raw)
            self.assertNotIn("hunter2", cleaned)
            self.assertNotIn("more-secret", cleaned)
            self.assertNotIn("abc123", cleaned)
            self.assertNotIn("extra-sensitive-fragment", cleaned)
            self.assertNotIn("first", cleaned)
            self.assertNotIn("second-third", cleaned)
            self.assertIn("<redacted>", cleaned)
            self.assertFalse(contains_unsafe_secret(cleaned))
            self.assertEqual(sanitize_durable_text(raw), cleaned)
            # Original suffix bytes must not survive durable notes.
            from atlas.chat_audit import AuditFinding, sanitize_finding

            finding = sanitize_finding(
                AuditFinding(
                    finding_id="safe-id",
                    unit="changed_code",
                    summary=raw,
                    severity="P2",
                )
            )
            self.assertNotIn("more-secret", finding.summary)
            self.assertNotIn("hunter2", finding.summary)

    def test_51_production_cli_injects_github_coordination_refresher(self):
        """Production path must never fall back to FixedCoordinationRefresher."""
        import argparse
        from unittest.mock import patch

        from atlas.chat_audit import FixedCoordinationRefresher
        from atlas.chat_audit_github import GitHubCoordinationRefresher
        from atlas.cli import _chat_audit_from_args

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                data_root=tmp,
                repository=REPO,
                checkpoint_issue=20,
                unit_adapter="evidence",
                allow_local_checkpoint=False,
                evidence_file=None,
                handoff="github",
                rollover_provider="fake",
                stagehand_approved=False,
                worktree=None,
                allow_trusted_identity=False,
            )
            local_args = argparse.Namespace(**vars(args))
            local_args.handoff = "local"
            with self.assertRaises(ValidationError) as local_rejected:
                _chat_audit_from_args(local_args)
            self.assertIn("local finding handoff", str(local_rejected.exception))
            with patch(
                "atlas.cli.resolve_checkpoint_store",
                return_value=FileCheckpointStore(Path(tmp) / "data"),
            ):
                ctl = _chat_audit_from_args(args)
            self.assertIsInstance(
                ctl.coordination, GitHubCoordinationRefresher
            )
            self.assertNotIsInstance(
                ctl.coordination, FixedCoordinationRefresher
            )

            # Controller refuses implicit Fixed default.
            with self.assertRaises(ValidationError) as ctx:
                ChatAuditController(FileCheckpointStore(Path(tmp) / "data2"))
            self.assertIn("CoordinationRefresher", str(ctx.exception))

            # Offline/test path may use Fixed explicitly.
            offline_args = argparse.Namespace(
                data_root=tmp,
                repository=None,
                checkpoint_issue=None,
                unit_adapter="fixed",
                allow_local_checkpoint=True,
                evidence_file=None,
                handoff="local",
                rollover_provider="fake",
                stagehand_approved=False,
                worktree=None,
                allow_trusted_identity=True,
            )
            offline = _chat_audit_from_args(offline_args)
            self.assertIsInstance(
                offline.coordination, FixedCoordinationRefresher
            )

    def test_52_sanitize_packet_rejects_nested_coordination_and_slice_secrets(self):
        """Durable persistence must not leave nested/extra secret leaves intact."""
        from atlas.chat_audit import (
            AuditControlPacket,
            make_run_key,
            sanitize_coordination_snapshot,
            sanitize_packet_for_persistence,
            sanitize_slice_dict,
        )

        queue = [
            "changed_code",
            "affected_contracts",
            "affected_tests_ci",
            "security_impact",
            "docs_spec_drift",
        ]
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=queue,
            idempotency_run_key=make_run_key(REPO, BRANCH, HEAD_A),
            last_coordination_refresh={
                "collector": "test",
                "status": "OK",
                "outcome": "PASSED",
                "reasons": [],
                "reviews": {
                    "status": "OK",
                    "actionable": False,
                    "count": 1,
                    "review_body": "PASSWORD=hunter2-live",
                },
            },
        )
        with self.assertRaises(ValidationError) as ctx:
            sanitize_packet_for_persistence(packet)
        self.assertIn("review_body", str(ctx.exception))

        # Unknown slice extras are rejected (not copied through).
        with self.assertRaises(ValidationError) as ctx2:
            sanitize_slice_dict(
                {
                    "unit": "changed_code",
                    "outcome": "PASS",
                    "GH_TOKEN": "ghs_live_secret_value",
                }
            )
        self.assertIn("GH_TOKEN", str(ctx2.exception))

        # Allowlisted nested strings are still recursively redacted.
        cleaned = sanitize_coordination_snapshot(
            {
                "collector": "test",
                "status": "OK",
                "outcome": "PASSED",
                "reasons": ["PASSWORD=hunter2 more-secret"],
                "reviews": {"status": "OK", "actionable": False, "count": 0},
            }
        )
        assert cleaned is not None
        self.assertNotIn("hunter2", cleaned["reasons"][0])
        self.assertNotIn("more-secret", cleaned["reasons"][0])

        # Cheap-path missing outcome fails closed (not synthetic PASS).
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.coordination = FixedCoordinationRefresher(
                {"status": "OK", "reasons": []}  # outcome intentionally absent
            )
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            while ctl.run_slice()["action"] != "queue_complete":
                pass
            cheap = ctl.run_slice()
            self.assertEqual(cheap["action"], "cheap_no_change_rework")
            self.assertEqual(cheap["outcome"], "HUMAN_REQUIRED")
            self.assertIn("outcome_missing", cheap["reason"])

    def test_53_malformed_nested_restore_and_negative_rollover_fail_closed(self):
        from atlas.chat_audit import AuditControlPacket, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A,
            "audit_status": "IDLE",
            "audit_queue": [
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            "idempotency_run_key": run_key,
        }
        cases = [
            {**base, "open_findings": "PASSWORD=x"},
            {**base, "open_findings": [1]},
            {**base, "session": "broken"},
            {**base, "session": {"state": "ACTIVE", "rollover_count": -3}},
        ]
        for raw in cases:
            with self.assertRaises(ValidationError):
                AuditControlPacket.from_dict(raw)

    def test_54_incomplete_executing_claim_fails_closed_before_executor(self):
        import time

        from atlas.chat_audit import AuditControlPacket, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A,
            "audit_status": "IN_SLICE",
            "current_unit": "changed_code",
            "current_unit_index": 0,
            "audit_queue": [
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            "idempotency_run_key": run_key,
        }
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {**base, "slice_claim": {"state": "executing"}}
            )
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    **base,
                    "slice_claim": {
                        "claim_id": "c1",
                        "unit": "changed_code",
                        "run_key": run_key,
                        "target_sha": HEAD_A,
                        "state": "executing",
                        "claimed_at": "soon",
                    },
                }
            )
        with self.assertRaises(ValidationError):
            AuditControlPacket.from_dict(
                {
                    **base,
                    "slice_claim": {
                        "claim_id": "c1",
                        "unit": "changed_code",
                        "run_key": run_key,
                        "target_sha": HEAD_A,
                        "state": "executing",
                        "claimed_at": time.time() + 10_000,
                    },
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            path = Path(tmp) / "data" / "chat-audit.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["audit_status"] = "IN_SLICE"
            raw["current_unit"] = "changed_code"
            raw["current_unit_index"] = 0
            raw["slice_claim"] = {"state": "executing"}
            path.write_text(json.dumps(raw), encoding="utf-8")
            calls_before = 0

            class Counting(FixedUnitExecutor):
                def execute(self, packet, unit, audit_request):
                    nonlocal calls_before
                    calls_before += 1
                    return super().execute(packet, unit, audit_request)

            ctl.executor = Counting()
            with self.assertRaises(ValidationError):
                ctl.run_slice()
            self.assertEqual(calls_before, 0)

    def test_55_coordination_maybe_outcome_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.coordination = FixedCoordinationRefresher(
                {"status": "OK", "outcome": "MAYBE", "reasons": []}
            )
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            while ctl.run_slice()["action"] != "queue_complete":
                pass
            cheap = ctl.run_slice()
            self.assertEqual(cheap["action"], "cheap_no_change_rework")
            self.assertEqual(cheap["outcome"], "HUMAN_REQUIRED")
            self.assertIn("outcome_unsupported", cheap["reason"])
            self.assertIn("maybe", cheap["reason"].lower())

    def test_56_pending_handoff_claim_not_stealable_during_create(self):
        """A owns pending+blocked create; B must not overwrite/create second Issue."""
        import base64
        import hashlib
        import subprocess
        import threading

        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import GitHubAIWorkHandoff

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=run_key,
        )
        finding = AuditFinding(
            finding_id="steal-id",
            unit="changed_code",
            summary="dup",
            severity="P2",
        )
        claims: dict[str, dict] = {}
        creates = {"n": 0}
        lock = threading.Lock()
        a_in_create = threading.Event()
        release_create = threading.Event()

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}":
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1" * 40}}), stderr=""
                )
            joined = " ".join(argv)
            if argv[:2] == ["gh", "api"] and "--method" not in argv and "/contents/" in joined:
                key = joined.split("/contents/")[-1].split("?")[0]
                with lock:
                    if key not in claims:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                        )
                    raw = claims[key]["raw"]
                    encoded = base64.b64encode(raw.encode()).decode()
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            {
                                "type": "file",
                                "sha": claims[key]["sha"],
                                "content": encoded,
                            }
                        ),
                        stderr="",
                    )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                if "git/refs" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                key = [a for a in argv if a.startswith("repos/")][0].split("/contents/")[-1]
                with lock:
                    expected = body.get("sha")
                    if key in claims and claims[key]["sha"] != expected:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="gh: HTTP 409"
                        )
                    # Refuse pending overwrite of live pending with different owner.
                    raw = base64.b64decode(body["content"]).decode()
                    incoming = json.loads(raw)
                    if key in claims:
                        existing = json.loads(claims[key]["raw"])
                        if (
                            existing.get("issue_number") is None
                            and incoming.get("issue_number") is None
                            and existing.get("owner_token")
                            and incoming.get("owner_token")
                            != existing.get("owner_token")
                        ):
                            return subprocess.CompletedProcess(
                                argv, 1, stdout="", stderr="gh: HTTP 409"
                            )
                    new_sha = hashlib.sha1(raw.encode()).hexdigest()
                    claims[key] = {"raw": raw, "sha": new_sha}
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"content": {"sha": new_sha}}),
                        stderr="",
                    )
            if argv[:3] == ["gh", "issue", "list"]:
                with lock:
                    if creates["n"] == 0:
                        return subprocess.CompletedProcess(
                            argv, 0, stdout="[]", stderr=""
                        )
                    n = 901
                    marker = "<!-- atlas-chat-audit-finding-id:steal-id -->"
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                {
                                    "number": n,
                                    "title": "[AI Work] Audit finding: steal-id",
                                    "body": f"{marker}\nTARGET_REPO={REPO}\n",
                                    "url": f"https://github.com/{REPO}/issues/{n}",
                                }
                            ]
                        ),
                        stderr="",
                    )
            if argv[:3] == ["gh", "issue", "create"]:
                a_in_create.set()
                release_create.wait(timeout=5)
                with lock:
                    creates["n"] += 1
                    n = 900 + creates["n"]
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=f"https://github.com/{REPO}/issues/{n}\n",
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "view"]:
                number = int(argv[3])
                body = (
                    f"<!-- atlas-chat-audit-finding-id:steal-id -->\n"
                    f"TARGET_REPO={REPO}\nSTATUS=ACTIVE\n"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": number,
                            "title": "[AI Work] Audit finding: steal-id",
                            "state": "OPEN",
                            "body": body,
                            "url": f"https://github.com/{REPO}/issues/{number}",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected:" + str(argv[:6])
            )

        a = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        b = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        outcomes: dict[str, Any] = {}
        errors: list[BaseException] = []

        def run_a():
            try:
                outcomes["a"] = a.upsert_implementation_packet(packet, finding)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                outcomes["a"] = {"error": str(exc)}

        def run_b():
            self.assertTrue(a_in_create.wait(timeout=5))
            try:
                outcomes["b"] = b.upsert_implementation_packet(packet, finding)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                outcomes["b"] = {"error": str(exc)}
            finally:
                release_create.set()

        t1 = threading.Thread(target=run_a)
        t2 = threading.Thread(target=run_b)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        self.assertEqual(creates["n"], 1)
        # B either waited then updated, or failed closed without creating.
        self.assertTrue(
            outcomes.get("b", {}).get("action") in {"updated", "created"}
            or any("pending held" in str(e) for e in errors)
            or outcomes.get("b", {}).get("action") == "updated"
        )
        if "error" not in outcomes.get("a", {}):
            self.assertEqual(outcomes["a"]["action"], "created")
        self.assertEqual(creates["n"], 1)

    def test_57_pointer_publish_conflict_requires_matching_canonical(self):
        import base64
        import subprocess

        from atlas.chat_audit import CheckpointCasConflict
        from atlas.chat_audit_github import publish_active_checkpoint_pointer

        state = {
            "sha": "oldsha",
            "raw": json.dumps(
                {"issue_number": 99, "workstream": "continuous-chat-audit-supervisor-poc"}
            ),
        }

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}":
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1" * 40}}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "--method" not in argv and "/contents/" in " ".join(argv):
                encoded = base64.b64encode(state["raw"].encode()).decode()
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {"type": "file", "sha": state["sha"], "content": encoded}
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="gh: HTTP 409 Conflict"
                )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        with self.assertRaises(CheckpointCasConflict):
            publish_active_checkpoint_pointer(
                repository=REPO, issue_number=20, command_runner=runner
            )

        # Matching canonical pointer may succeed on conflict.
        state["raw"] = json.dumps(
            {
                "issue_number": 20,
                "workstream": "continuous-chat-audit-supervisor-poc",
            }
        )
        out = publish_active_checkpoint_pointer(
            repository=REPO, issue_number=20, command_runner=runner
        )
        self.assertEqual(out["issue_number"], 20)
        self.assertTrue(out.get("already_matched"))

    def test_58_discover_rejects_stale_wrong_workstream_pointer(self):
        import base64
        import subprocess

        from atlas.chat_audit_github import discover_checkpoint_issue

        pointer = {
            "issue_number": 12,
            "workstream": "other-workstream",
        }

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and "/contents/" in " ".join(argv):
                encoded = base64.b64encode(json.dumps(pointer).encode()).decode()
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {"type": "file", "sha": "p", "content": encoded}
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "number": 20,
                                "title": "[AI Work] continuous chat audit",
                                "body": (
                                    "WORKSTREAM=continuous-chat-audit-supervisor-poc\n"
                                    "STATUS=ACTIVE\n"
                                    f"TARGET_REPO={REPO}\n"
                                ),
                            }
                        ]
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        self.assertEqual(
            discover_checkpoint_issue(repository=REPO, command_runner=runner),
            20,
        )

    def test_59_coordination_requires_active_wp_and_review_surfaces(self):
        import subprocess

        from atlas.chat_audit import AuditControlPacket, make_run_key
        from atlas.chat_audit_github import GitHubCoordinationRefresher

        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=make_run_key(REPO, BRANCH, HEAD_A),
        )
        calls = {"inline": 0, "conversation": 0, "reviews": 0}

        def mk(
            wp_status: str,
            *,
            include_pr: bool = True,
            inline_p1: bool = False,
            gate: str | None = "READY",
        ):
            def runner(argv: list[str], cwd: str):
                joined = " ".join(argv)
                if argv[:3] == ["gh", "issue", "view"]:
                    body = (
                        f"WORKSTREAM=continuous-chat-audit-supervisor-poc\n"
                        f"STATUS={wp_status}\nTARGET_REPO={REPO}\n"
                    )
                    if gate:
                        body += f"GATE={gate}\n"
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            {
                                "number": 20,
                                "title": "wp",
                                "state": "OPEN",
                                "body": body,
                                "updatedAt": "t",
                            }
                        ),
                        stderr="",
                    )
                if argv[:3] == ["gh", "pr", "list"]:
                    if not include_pr:
                        return subprocess.CompletedProcess(
                            argv, 0, stdout="[]", stderr=""
                        )
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                {
                                    "number": 21,
                                    "title": "p",
                                    "state": "OPEN",
                                    "headRefOid": HEAD_A,
                                    "url": "u",
                                }
                            ]
                        ),
                        stderr="",
                    )
                if argv[:3] == ["gh", "pr", "checks"]:
                    # Real GitHub Actions reusable-workflow check names.
                    out = (
                        "adoption-compliance / compliance\tpass\t1s\t0\t\n"
                        "enforcement-reconcile / reconcile\tpass\t1s\t0\t\n"
                        "affected-tests / affected\tpass\t1s\t0\t\n"
                    )
                    return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
                if len(argv) >= 3 and argv[2] == "graphql":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        "pullRequest": {
                                            "reviewThreads": {
                                                "pageInfo": {
                                                    "hasNextPage": False,
                                                    "endCursor": None,
                                                },
                                                "nodes": [],
                                            }
                                        }
                                    }
                                }
                            }
                        ),
                        stderr="",
                    )
                if "--paginate" in argv and "--slurp" in argv:
                    path = argv[-1]
                    if path.endswith("/reviews"):
                        calls["reviews"] += 1
                        # Historical old-HEAD P1 plus current wrapper.
                        payload = [
                            [
                                {
                                    "body": "P1 old finding",
                                    "state": "COMMENTED",
                                    "commit_id": HEAD_B,
                                },
                                {
                                    "body": "wrapper",
                                    "state": "COMMENTED",
                                    "commit_id": HEAD_A,
                                },
                            ]
                        ]
                    elif path.endswith("/pulls/21/comments"):
                        calls["inline"] += 1
                        body = "P1 inline finding" if inline_p1 else "nit"
                        payload = [[{"body": body, "commit_id": HEAD_A}]]
                    elif path.endswith("/issues/21/comments"):
                        calls["conversation"] += 1
                        payload = [
                            [
                                {
                                    "body": (
                                        "@codex review this exact HEAD for Issue #20 "
                                        "REWORK: P1/P2. Do not merge."
                                    ),
                                    "user": {"login": "RickLee-kr"},
                                },
                                {"body": "REWORK please", "user": {"login": "reviewer"}},
                            ]
                        ]
                    else:
                        payload = [[]]
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(payload), stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="unexpected " + joined
                )

            return runner

        for status in ["UNKNOWN", "COMPLETE", "HUMAN_REQUIRED", "REWORK"]:
            snap = GitHubCoordinationRefresher(
                repository=REPO,
                work_packet_issue=20,
                command_runner=mk(status),
            ).refresh(packet)
            self.assertEqual(snap["status"], "HUMAN_REQUIRED")
            self.assertIn(f"work_packet_{status.lower()}", snap["reasons"])

        absent = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=mk("ACTIVE", include_pr=False),
        ).refresh(packet)
        self.assertEqual(absent["status"], "HUMAN_REQUIRED")
        self.assertIn("pr_absent", absent["reasons"])

        calls["inline"] = calls["conversation"] = calls["reviews"] = 0
        inline = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=mk("ACTIVE", inline_p1=True),
        ).refresh(packet)
        self.assertGreater(calls["inline"], 0)
        self.assertGreater(calls["conversation"], 0)
        self.assertGreater(calls["reviews"], 0)
        self.assertEqual(inline["status"], "HUMAN_REQUIRED")
        self.assertIn("actionable_review", inline["reasons"])

        # Unrelated-only CI green is not PASS.
        def unrelated_ci(argv: list[str], cwd: str):
            base = mk("ACTIVE")(argv, cwd)
            if argv[:3] == ["gh", "pr", "checks"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="unrelated-check\tpass\t1s\t0\t\n",
                    stderr="",
                )
            return base

        ci_only = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=unrelated_ci,
        ).refresh(packet)
        self.assertEqual(ci_only["status"], "HUMAN_REQUIRED")
        self.assertIn("ci_required_checks_missing", ci_only["reasons"])

        # Owner @codex request comments must not alone force actionable_review;
        # old-HEAD P1 reviews must not block current HEAD when no current finding.
        clean = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=mk("ACTIVE", inline_p1=False),
        ).refresh(packet)
        # Conversation has @codex control request + "REWORK please" finding —
        # the latter is still actionable. Use a runner without the human REWORK.
        def clean_runner(argv: list[str], cwd: str):
            base = mk("ACTIVE", inline_p1=False)(argv, cwd)
            if "--paginate" in argv and argv[-1].endswith("/issues/21/comments"):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            [
                                {
                                    "body": (
                                        "@codex review this exact HEAD for Issue #20 "
                                        "REWORK: P1/P2. Do not merge."
                                    ),
                                    "user": {"login": "RickLee-kr"},
                                }
                            ]
                        ]
                    ),
                    stderr="",
                )
            return base

        clean2 = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=clean_runner,
        ).refresh(packet)
        self.assertEqual(clean2["status"], "OK")
        self.assertEqual(clean2["outcome"], "PASSED")
        self.assertNotIn("actionable_review", clean2["reasons"])

        def gated(gate: str | None):
            def runner(argv: list[str], cwd: str):
                base = mk("ACTIVE", inline_p1=False, gate=gate)(argv, cwd)
                if "--paginate" in argv and argv[-1].endswith("/issues/21/comments"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                [
                                    {
                                        "body": (
                                            "@codex review this exact HEAD for Issue #20 "
                                            "REWORK: P1/P2. Do not merge."
                                        ),
                                        "user": {"login": "RickLee-kr"},
                                    }
                                ]
                            ]
                        ),
                        stderr="",
                    )
                return base

            return runner

        rework_gate = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=gated("CHATGPT_EXACT_HEAD_REWORK"),
        ).refresh(packet)
        self.assertEqual(rework_gate["work_packet"]["status"], "ACTIVE")
        self.assertEqual(rework_gate["status"], "HUMAN_REQUIRED")
        self.assertNotEqual(rework_gate["outcome"], "PASSED")
        self.assertTrue(
            any(
                str(item).startswith("work_packet_gate_")
                for item in rework_gate["reasons"]
            )
        )
        missing_gate = GitHubCoordinationRefresher(
            repository=REPO,
            work_packet_issue=20,
            command_runner=gated(None),
        ).refresh(packet)
        self.assertIn("work_packet_gate_missing", missing_gate["reasons"])
        self.assertNotEqual(missing_gate["outcome"], "PASSED")

    def test_60_finalized_handoff_claim_requires_issue_lifecycle(self):
        import base64
        import hashlib
        import subprocess

        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import GitHubAIWorkHandoff

        old_run = make_run_key(REPO, BRANCH, HEAD_B)
        new_run = make_run_key(REPO, BRANCH, HEAD_A)
        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=new_run,
        )
        finding = AuditFinding(
            finding_id="recurrence-1",
            unit="changed_code",
            summary="same finding new head",
            severity="P2",
        )
        marker = "<!-- atlas-chat-audit-finding-id:recurrence-1 -->"
        claim_raw = json.dumps(
            {
                "finding_id": "recurrence-1",
                "issue_number": 77,
                "url": f"https://github.com/{REPO}/issues/77",
                "run_key": old_run,
                "head": HEAD_B,
                "state": "finalized",
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        claims = {
            # digest path computed inside adapter; capture via first GET 404 then PUT.
        }
        views = {"n": 0}
        edits = {"n": 0}
        issue_state = {"state": "OPEN", "marker": True, "title_ok": True, "repo_ok": True}

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}":
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1" * 40}}), stderr=""
                )
            joined = " ".join(argv)
            if argv[:2] == ["gh", "api"] and "--method" not in argv and "/contents/" in joined:
                key = joined.split("/contents/")[-1].split("?")[0]
                if "handoff-claims" in key and key not in claims:
                    # Seed finalized claim on first GET by writing into map.
                    claims[key] = {
                        "raw": claim_raw,
                        "sha": hashlib.sha1(claim_raw.encode()).hexdigest(),
                    }
                if key not in claims:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                encoded = base64.b64encode(claims[key]["raw"].encode()).decode()
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "file",
                            "sha": claims[key]["sha"],
                            "content": encoded,
                        }
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                if "git/refs" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                key = [a for a in argv if a.startswith("repos/")][0].split("/contents/")[-1]
                raw = base64.b64decode(body["content"]).decode()
                new_sha = hashlib.sha1(raw.encode()).hexdigest()
                claims[key] = {"raw": raw, "sha": new_sha}
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"content": {"sha": new_sha}}), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                views["n"] += 1
                body = ""
                if issue_state["marker"]:
                    body += marker + "\n"
                if issue_state["repo_ok"]:
                    body += f"TARGET_REPO={REPO}\nSTATUS=ACTIVE\n"
                title = (
                    "[AI Work] Audit finding: recurrence-1"
                    if issue_state["title_ok"]
                    else "[AI Work] unrelated"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": 77,
                            "title": title,
                            "state": issue_state["state"],
                            "body": body,
                            "url": f"https://github.com/{REPO}/issues/77",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits["n"] += 1
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected:" + str(argv[:6])
            )

        handoff = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        out = handoff.upsert_implementation_packet(packet, finding)
        self.assertTrue(out.get("issue_viewed"))
        self.assertEqual(views["n"], 1)
        self.assertEqual(edits["n"], 1)
        self.assertEqual(out["claim"], "recurrence_update")
        self.assertEqual(out["issue_number"], 77)

        # Closed issue fails closed.
        issue_state["state"] = "CLOSED"
        views["n"] = 0
        with self.assertRaises(ValidationError):
            handoff.upsert_implementation_packet(packet, finding)
        self.assertEqual(views["n"], 1)

        # Wrong marker fails closed.
        issue_state["state"] = "OPEN"
        issue_state["marker"] = False
        with self.assertRaises(ValidationError):
            handoff.upsert_implementation_packet(packet, finding)

        # Missing finding_id on claim fails closed (issue_number-only trust).
        for key in list(claims):
            if "handoff-claims" in key and not key.endswith("_bootstrap.json"):
                claims[key] = {
                    "raw": json.dumps(
                        {"issue_number": 77, "run_key": old_run, "head": HEAD_B}
                    )
                    + "\n",
                    "sha": "x",
                }
        with self.assertRaises(ValidationError) as ctx:
            handoff.upsert_implementation_packet(packet, finding)
        self.assertIn("finding_id", str(ctx.exception).lower())

    def test_61_ensure_control_branch_rejects_arbitrary_422(self):
        import subprocess

        from atlas.chat_audit_github import GitHubContentsCheckpointStore

        def runner(argv: list[str], cwd: str):
            if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}":
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "/git/ref/heads/main" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"object": {"sha": "1" * 40}}), stderr=""
                )
            if (
                argv[:2] == ["gh", "api"]
                and "/git/ref/heads/atlas/chat-audit-control" in " ".join(argv)
                and "--method" not in argv
            ):
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv and "git/refs" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr='gh: HTTP 422 {"message":"Invalid request: sha is not a commit"}',
                )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        store = GitHubContentsCheckpointStore(
            repository=REPO, issue_number=20, command_runner=runner
        )
        with self.assertRaises(ValidationError) as ctx:
            store.ensure_control_branch()
        self.assertNotIn("exists_race", str(ctx.exception).lower())
        self.assertIn("422", str(ctx.exception))

    def test_62_show_and_resume_expose_head_drift_as_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            shown = ctl.show()
            self.assertEqual(shown["current_target_sha"], HEAD_A)
            self.assertEqual(shown["worktree_head"], HEAD_B)
            self.assertTrue(shown["head_drift"])
            self.assertTrue(shown["resumable"])
            payload = ctl.resume_instruction_payload()
            self.assertTrue(payload["head_drift"])
            self.assertTrue(payload["resumable"])
            self.assertEqual(payload["worktree_head"], HEAD_B)
            self.assertEqual(payload["current_target_sha"], HEAD_A)
            advanced = ctl.run_slice()
            self.assertEqual(advanced["action"], "slice_complete")
            self.assertEqual(ctl.show()["current_target_sha"], HEAD_B)
            self.assertFalse(ctl.show()["head_drift"])

    def test_63_non_live_old_slice_rotates_on_head_advance(self):
        import time

        from atlas.chat_audit import AuditControlPacket

        cases = ("timed_out", "failed", "expired_executing")
        for label in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    ctl = self._ctl(tmp)
                    ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
                    packet = AuditControlPacket.from_dict(ctl.show())
                    packet.audit_status = "IN_SLICE"
                    packet.current_unit = "changed_code"
                    packet.current_unit_index = 0
                    claim = {
                        "claim_id": f"old-{label}",
                        "unit": "changed_code",
                        "run_key": packet.idempotency_run_key,
                        "target_sha": HEAD_A,
                        "state": "executing" if label == "expired_executing" else label,
                        "claimed_at": time.time() - 10_000,
                        "lease_seconds": 900,
                    }
                    if label != "expired_executing":
                        claim["state"] = label
                    packet.slice_claim = claim
                    FileCheckpointStore(Path(tmp) / "data").save(packet)
                    ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
                    out = ctl.run_slice()
                    self.assertEqual(out["action"], "slice_complete")
                    shown = ctl.show()
                    self.assertEqual(shown["current_target_sha"], HEAD_B)
                    self.assertNotEqual(shown["audit_status"], "IN_SLICE")
                    self.assertIsNone(shown["slice_claim"])

    def test_64_live_inslice_claim_refuses_head_advance(self):
        import time

        from atlas.chat_audit import AuditControlPacket

        with tempfile.TemporaryDirectory() as tmp:
            executor = FixedUnitExecutor()
            ctl = self._ctl(tmp, executor=executor)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            packet = AuditControlPacket.from_dict(ctl.show())
            packet.audit_status = "IN_SLICE"
            packet.current_unit = "changed_code"
            packet.current_unit_index = 0
            packet.slice_claim = {
                "claim_id": "live-claim",
                "unit": "changed_code",
                "run_key": packet.idempotency_run_key,
                "target_sha": HEAD_A,
                "state": "executing",
                "claimed_at": time.time(),
                "lease_seconds": 900,
            }
            FileCheckpointStore(Path(tmp) / "data").save(packet)
            ctl._test_head["head"] = HEAD_B  # type: ignore[attr-defined]
            calls_before = len(executor.calls)
            with self.assertRaises(ValidationError) as ctx:
                ctl.run_slice()
            self.assertIn("live", str(ctx.exception).lower())
            self.assertEqual(len(executor.calls), calls_before)
            reloaded = FileCheckpointStore(Path(tmp) / "data").load()
            assert reloaded is not None
            self.assertEqual(reloaded.current_target_sha, HEAD_A)
            self.assertEqual(reloaded.slice_claim["state"], "executing")

    def test_65_external_evidence_requires_explicit_outcome_and_truncated(self):
        from atlas.chat_audit import ExternalEvidenceUnitExecutor

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(
                tmp,
                executor=ExternalEvidenceUnitExecutor(
                    {
                        "status": "COMPLETE",
                        "unit": "changed_code",
                        "target_sha": HEAD_A,
                        "notes": "P1: authentication bypass found",
                    }
                ),
            )
            out = ctl.run_slice(repository=REPO, branch=BRANCH, head=HEAD_A)
            self.assertEqual(out["action"], "failed_closed")
            self.assertEqual(out["outcome"], "HUMAN_REQUIRED")
            self.assertNotEqual(out["packet"]["audit_status"], "PASSED")
            self.assertEqual(out["packet"]["open_findings"], [])

            ctl.executor = ExternalEvidenceUnitExecutor(
                {
                    "status": "COMPLETE",
                    "unit": "changed_code",
                    "target_sha": HEAD_A,
                    "notes": "looks fine",
                    "outcome": "PASS",
                }
            )
            missing_flag = ctl.run_slice()
            self.assertEqual(missing_flag["action"], "failed_closed")
            self.assertIn("truncated", missing_flag["packet"]["session"]["notes"])

            ctl.executor = ExternalEvidenceUnitExecutor(
                {
                    "status": "COMPLETE",
                    "unit": "changed_code",
                    "target_sha": HEAD_A,
                    "notes": "looks fine",
                    "outcome": "PASS",
                    "truncated": "false",
                }
            )
            non_bool = ctl.run_slice()
            self.assertEqual(non_bool["action"], "failed_closed")

    def test_66_restored_passed_without_evidence_rejected(self):
        from atlas.chat_audit import AuditControlPacket, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A,
            "audit_queue": [
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            "idempotency_run_key": run_key,
            "completed_units": {},
            "open_findings": [],
        }
        corrupted = [
            {**base, "audit_status": "PASSED", "last_audited_sha": HEAD_A},
            {**base, "audit_status": "FINDINGS", "last_audited_sha": None},
            {**base, "audit_status": "IDLE", "last_audited_sha": HEAD_A},
            {
                **base,
                "audit_status": "IN_SLICE",
                "last_audited_sha": HEAD_A,
                "current_unit": "changed_code",
                "current_unit_index": 0,
                "slice_claim": {
                    "claim_id": "c",
                    "unit": "changed_code",
                    "run_key": run_key,
                    "target_sha": HEAD_A,
                    "state": "timed_out",
                },
            },
        ]
        for raw in corrupted:
            with self.assertRaises(ValidationError):
                AuditControlPacket.from_dict(raw)

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            path = Path(tmp) / "data" / "chat-audit.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["audit_status"] = "PASSED"
            stored["last_audited_sha"] = HEAD_A
            stored["completed_units"] = {}
            path.write_text(json.dumps(stored), encoding="utf-8")
            with self.assertRaises(ValidationError):
                ctl.run_slice()

    def _finding_executor(self, finding_id: str, severity: str = "P1"):
        from atlas.chat_audit import AuditEvidence, AuditFinding, SliceResult

        class _Executor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, packet, unit, audit_request):
                self.calls.append(unit)
                return SliceResult(
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    outcome="FINDING",
                    findings=[
                        AuditFinding(
                            finding_id=finding_id,
                            unit=unit,
                            summary="bounded finding",
                            severity=severity,
                        )
                    ],
                    evidence=AuditEvidence(
                        status="COMPLETE",
                        unit=unit,
                        target_sha=packet.current_target_sha,
                        notes="complete evidence",
                        truncated=False,
                    ),
                    audit_request=audit_request,
                )

        return _Executor()

    def test_67_invalid_executor_findings_rejected_before_handoff(self):
        from atlas.chat_audit import AuditFinding

        cases = (
            ("bad/id", "P1", "finding_id"),
            ("ok-id", "INVALID", "severity"),
        )
        for finding_id, severity, needle in cases:
            with self.subTest(finding_id=finding_id, severity=severity):
                with self.assertRaises(ValidationError):
                    AuditFinding.from_dict(
                        {
                            "finding_id": finding_id,
                            "unit": "changed_code",
                            "summary": "x",
                            "severity": severity,
                        }
                    )
                with tempfile.TemporaryDirectory() as tmp:
                    handoff = RecordingWorkPacketHandoff()
                    executor = self._finding_executor(finding_id, severity)
                    ctl = self._ctl(tmp, executor=executor, handoff=handoff)
                    ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
                    result = ctl.run_slice()
                    self.assertEqual(result["action"], "failed_closed")
                    self.assertEqual(result["reason"], "result_validation_exception")
                    self.assertEqual(handoff.handoffs, [])
                    self.assertEqual(executor.calls, ["changed_code"])
                    packet = result["packet"]
                    self.assertEqual(packet["audit_status"], "IN_SLICE")
                    self.assertEqual(packet["slice_claim"]["state"], "failed")
                    self.assertEqual(packet["open_findings"], [])
                    self.assertEqual(packet["completed_units"], {})
                    self.assertIn(needle, packet["session"]["notes"])
                    self.assertNotIn("lost or stolen", packet["session"]["notes"])
                    stored = json.loads(
                        (Path(tmp) / "data" / "chat-audit.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(stored["audit_status"], "IN_SLICE")
                    self.assertEqual(stored["slice_claim"]["state"], "failed")
                    self.assertEqual(stored["slice_claim"]["claim_id"], packet["slice_claim"]["claim_id"])

    def test_68_password_finding_id_rejected_before_handoff(self):
        from atlas.chat_audit import AuditFinding
        from atlas.secrets import contains_unsafe_secret

        finding_id = "PASSWORD:hunter2"
        self.assertFalse(contains_unsafe_secret(finding_id))
        with self.assertRaises(ValidationError):
            AuditFinding.from_dict(
                {
                    "finding_id": finding_id,
                    "unit": "changed_code",
                    "summary": "x",
                    "severity": "P1",
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            handoff = RecordingWorkPacketHandoff()
            ctl = self._ctl(
                tmp,
                executor=self._finding_executor(finding_id),
                handoff=handoff,
            )
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            result = ctl.run_slice()
            self.assertEqual(result["action"], "failed_closed")
            self.assertEqual(result["reason"], "result_validation_exception")
            self.assertEqual(handoff.handoffs, [])
            self.assertIn("credential-like", result["packet"]["session"]["notes"])
            self.assertNotIn("hunter2", json.dumps(result["packet"]))
            stored = json.loads(
                (Path(tmp) / "data" / "chat-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored["slice_claim"]["state"], "failed")
            self.assertNotIn("hunter2", json.dumps(stored))
            self.assertNotIn("lost or stolen", stored["slice_claim"].get("error", ""))

    def test_69_result_save_failure_keeps_original_claim_retryable(self):
        class FailBeforeCommit(FileCheckpointStore):
            def __init__(self, data_root: Path):
                super().__init__(data_root)
                self.fail_result_saves = 1

            def save(self, packet):
                if (
                    packet.audit_status == "SLICE_COMPLETE"
                    and self.fail_result_saves > 0
                ):
                    self.fail_result_saves -= 1
                    raise OSError("transient result save failure")
                super().save(packet)

        with tempfile.TemporaryDirectory() as tmp:
            store = FailBeforeCommit(Path(tmp) / "data")
            executor = FixedUnitExecutor()
            current = {"head": HEAD_A}

            def identity():
                from atlas.chat_audit import Identity

                return Identity(repository=REPO, branch=BRANCH, head=current["head"])

            ctl = ChatAuditController(
                store,
                executor=executor,
                handoff=RecordingWorkPacketHandoff(),
                coordination=FixedCoordinationRefresher(),
                identity_resolver=identity,
            )
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            failed = ctl.run_slice()
            self.assertEqual(failed["action"], "failed_closed")
            self.assertEqual(failed["reason"], "result_save_exception")
            self.assertIn(
                "transient result save failure", failed["packet"]["session"]["notes"]
            )
            self.assertNotIn("lost or stolen", failed["packet"]["session"]["notes"])
            claim = failed["packet"]["slice_claim"]
            self.assertEqual(claim["state"], "failed")
            self.assertEqual(failed["packet"]["audit_status"], "IN_SLICE")
            self.assertEqual(failed["packet"]["completed_units"], {})
            stored = json.loads(
                (Path(tmp) / "data" / "chat-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored["audit_status"], "IN_SLICE")
            self.assertEqual(stored["slice_claim"]["state"], "failed")
            self.assertEqual(stored["slice_claim"]["claim_id"], claim["claim_id"])
            self.assertIn("transient result save failure", stored["slice_claim"]["error"])
            retried = ctl.run_slice()
            self.assertEqual(retried["action"], "slice_complete")
            self.assertEqual(retried["unit"], "changed_code")
            self.assertEqual(len(executor.calls), 2)

    def test_70_committed_result_cache_error_replays_server_truth(self):
        class CommitThenCacheError(FileCheckpointStore):
            def __init__(self, data_root: Path):
                super().__init__(data_root)
                self.cache_failures = 0

            def save(self, packet):
                super().save(packet)
                if (
                    packet.audit_status == "SLICE_COMPLETE"
                    and self.cache_failures == 0
                ):
                    self.cache_failures += 1
                    raise OSError("cache follow-up failed")

        with tempfile.TemporaryDirectory() as tmp:
            store = CommitThenCacheError(Path(tmp) / "data")
            executor = FixedUnitExecutor()
            current = {"head": HEAD_A}

            def identity():
                from atlas.chat_audit import Identity

                return Identity(repository=REPO, branch=BRANCH, head=current["head"])

            ctl = ChatAuditController(
                store,
                executor=executor,
                handoff=RecordingWorkPacketHandoff(),
                coordination=FixedCoordinationRefresher(),
                identity_resolver=identity,
            )
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            replayed = ctl.run_slice()
            self.assertEqual(replayed["action"], "idempotent_replay")
            self.assertTrue(replayed["idempotent_replay"])
            self.assertEqual(replayed["outcome"], "PASS")
            self.assertEqual(replayed["reason"], "result_already_committed")
            self.assertNotIn("lost or stolen", json.dumps(replayed))
            packet = replayed["packet"]
            self.assertEqual(packet["audit_status"], "SLICE_COMPLETE")
            self.assertIsNone(packet["slice_claim"])
            self.assertTrue(
                any(key.endswith(f":changed_code:{HEAD_A}") for key in packet["completed_units"])
            )
            stored = json.loads(
                (Path(tmp) / "data" / "chat-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored["audit_status"], "SLICE_COMPLETE")
            self.assertIsNone(stored["slice_claim"])
            self.assertEqual(len(executor.calls), 1)
            nxt = ctl.run_slice()
            self.assertEqual(nxt["action"], "slice_complete")
            self.assertEqual(nxt["unit"], "affected_contracts")
            self.assertEqual(len(executor.calls), 2)

    def _blank_packet(self):
        from atlas.chat_audit import AuditControlPacket, make_run_key

        return AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=make_run_key(REPO, BRANCH, HEAD_A),
            canonical_revision=0,
        )

    def _contents_runner(self, state: dict[str, Any]):
        import base64
        import subprocess

        def runner(argv: list[str], cwd: str):
            joined = " ".join(argv)
            if argv[:2] == ["gh", "api"] and "--method" not in argv:
                if "/contents/" not in joined:
                    if "/git/ref/heads/" in joined:
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            stdout=json.dumps({"object": {"sha": "1" * 40}}),
                            stderr="",
                        )
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"default_branch": "main"}),
                        stderr="",
                    )
                if state.get("raw") is None:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                encoded = base64.b64encode(state["raw"].encode("utf-8")).decode(
                    "ascii"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "file",
                            "sha": state["sha"],
                            "content": encoded,
                        }
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv:
                if "git/refs" in joined:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="{}", stderr=""
                    )
                idx = argv.index("--input")
                body = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
                raw = base64.b64decode(body["content"]).decode("utf-8")
                state["puts"] = int(state.get("puts") or 0) + 1
                state["expected_shas"] = list(state.get("expected_shas") or [])
                state["expected_shas"].append(body.get("sha"))
                mode = state.get("put_mode") or "ok"
                if mode == "reset":
                    state["raw"] = raw
                    state["sha"] = state.get("remote_sha") or "remote-blob"
                    state["put_mode"] = "ok"
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout="",
                        stderr="connection reset by peer",
                    )
                if mode == "differ":
                    state["raw"] = state.get("other_raw") or "{\"other\":true}\n"
                    state["sha"] = "other-blob"
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout="",
                        stderr="connection reset by peer",
                    )
                state["raw"] = raw
                state["sha"] = state.get("remote_sha") or "server-blob"
                if mode == "omit-sha":
                    state["put_mode"] = "ok"
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="{}", stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"content": {"sha": state["sha"]}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        return runner

    def test_71_canonical_load_and_save_survive_cache_disk_full(self):
        from atlas.chat_audit_github import GitHubContentsCheckpointStore

        class DiskFullCache(FileCheckpointStore):
            def save(self, packet):
                raise OSError("disk full")

        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskFullCache(Path(tmp) / "data")
            state: dict[str, Any] = {"raw": None, "sha": None, "puts": 0}
            store = GitHubContentsCheckpointStore(
                repository=REPO,
                issue_number=20,
                cache=cache,
                command_runner=self._contents_runner(state),
            )
            packet = self._blank_packet()
            packet.canonical_revision = 1
            state["sha"] = "loaded-blob"
            state["raw"] = json.dumps(packet.to_dict(), indent=2, sort_keys=True) + "\n"
            loaded = store.load()
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.canonical_revision, 1)
            self.assertEqual(store._cas_blob_sha, "loaded-blob")
            self.assertIn("disk full", store.last_cache_error or "")
            loaded.audit_status = "IDLE"
            store.save(loaded)
            self.assertEqual(loaded.canonical_revision, 2)
            self.assertEqual(store._cas_blob_sha, "server-blob")
            self.assertIn("disk full", store.last_cache_error or "")
            self.assertEqual(state["puts"], 1)
            self.assertEqual(state["expected_shas"], ["loaded-blob"])

    def test_72_ambiguous_put_adopts_server_blob_without_retry(self):
        from atlas.chat_audit import CheckpointCasConflict
        from atlas.chat_audit_github import GitHubContentsCheckpointStore

        state: dict[str, Any] = {
            "raw": None,
            "sha": None,
            "puts": 0,
            "put_mode": "reset",
            "remote_sha": "remote-blob",
        }
        store = GitHubContentsCheckpointStore(
            repository=REPO,
            issue_number=20,
            command_runner=self._contents_runner(state),
        )
        packet = self._blank_packet()
        self.assertIsNone(store.load())
        store.save(packet)
        self.assertEqual(packet.canonical_revision, 1)
        self.assertEqual(store._cas_blob_sha, "remote-blob")
        self.assertEqual(state["puts"], 1)
        self.assertIsNone(store.last_cache_error)
        store.save(packet)
        self.assertEqual(packet.canonical_revision, 2)
        self.assertEqual(state["puts"], 2)
        self.assertEqual(state["expected_shas"][1], "remote-blob")

        differ: dict[str, Any] = {
            "raw": None,
            "sha": None,
            "puts": 0,
            "put_mode": "differ",
            "other_raw": "{\"not\":\"the packet\"}\n",
        }
        other = GitHubContentsCheckpointStore(
            repository=REPO,
            issue_number=20,
            command_runner=self._contents_runner(differ),
        )
        stale = self._blank_packet()
        self.assertIsNone(other.load())
        with self.assertRaises(CheckpointCasConflict):
            other.save(stale)
        self.assertEqual(stale.canonical_revision, 0)
        self.assertIsNone(other._cas_blob_sha)

    def test_73_missing_put_sha_rereads_checkpoint_and_handoff_claim(self):
        import hashlib

        from atlas.chat_audit_github import (
            GitHubAIWorkHandoff,
            GitHubContentsCheckpointStore,
        )

        state: dict[str, Any] = {
            "raw": None,
            "sha": None,
            "puts": 0,
            "put_mode": "omit-sha",
            "remote_sha": "real-blob-2",
        }
        store = GitHubContentsCheckpointStore(
            repository=REPO,
            issue_number=20,
            command_runner=self._contents_runner(state),
        )
        packet = self._blank_packet()
        self.assertIsNone(store.load())
        store.save(packet)
        invented = hashlib.sha1(state["raw"].encode("utf-8")).hexdigest()
        self.assertEqual(store._cas_blob_sha, "real-blob-2")
        self.assertNotEqual(store._cas_blob_sha, invented)
        self.assertEqual(packet.canonical_revision, 1)
        self.assertEqual(state["puts"], 1)
        store.save(packet)
        self.assertEqual(state["expected_shas"][1], "real-blob-2")

        claim_state: dict[str, Any] = {
            "raw": None,
            "sha": None,
            "puts": 0,
            "put_mode": "omit-sha",
            "remote_sha": "real-claim-sha",
        }
        handoff = GitHubAIWorkHandoff(
            repository=REPO,
            command_runner=self._contents_runner(claim_state),
        )
        claim = {"finding_id": "safe-id", "issue_number": None}
        sha = handoff._put_claim("safe-id", claim, expected_sha=None)
        invented_claim = hashlib.sha1(claim_state["raw"].encode("utf-8")).hexdigest()
        self.assertEqual(sha, "real-claim-sha")
        self.assertNotEqual(sha, invented_claim)
        self.assertEqual(claim_state["puts"], 1)
        claim_state["put_mode"] = "reset"
        claim_state["remote_sha"] = "remote-claim-sha"
        reset_sha = handoff._put_claim("safe-id", claim, expected_sha=sha)
        self.assertEqual(reset_sha, "remote-claim-sha")
        self.assertEqual(claim_state["puts"], 2)
        self.assertEqual(claim_state["expected_shas"][1], "real-claim-sha")

    def test_74_marker_lookup_failure_does_not_create_issue(self):
        import subprocess

        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import GitHubAIWorkHandoff

        packet = self._blank_packet()
        packet.idempotency_run_key = make_run_key(REPO, BRANCH, HEAD_A)
        finding = AuditFinding(
            finding_id="safe-id",
            unit="changed_code",
            summary="lookup",
            severity="P2",
        )
        creates: list[int] = []

        def runner(argv: list[str], cwd: str, *, list_mode: str = "rate"):
            joined = " ".join(argv)
            if argv[:2] == ["gh", "api"] and "--method" not in argv:
                if "/contents/" in joined:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                if "/git/ref/heads/" in joined:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"object": {"sha": "1" * 40}}),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:3] == ["gh", "issue", "list"]:
                if list_mode == "rate":
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="rate limit exceeded"
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout="not-json", stderr=""
                )
            if argv[:3] == ["gh", "issue", "create"]:
                creates.append(901)
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=f"https://github.com/{REPO}/issues/901\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        handoff = GitHubAIWorkHandoff(
            repository=REPO,
            command_runner=lambda argv, cwd: runner(argv, cwd, list_mode="rate"),
        )
        with self.assertRaises(ValidationError) as raised:
            handoff.upsert_implementation_packet(packet, finding)
        self.assertIn("unavailable", str(raised.exception))
        self.assertEqual(creates, [])

        handoff_bad = GitHubAIWorkHandoff(
            repository=REPO,
            command_runner=lambda argv, cwd: runner(argv, cwd, list_mode="json"),
        )
        with self.assertRaises(ValidationError) as raised_json:
            handoff_bad.upsert_implementation_packet(packet, finding)
        self.assertIn("non-JSON", str(raised_json.exception))
        self.assertEqual(creates, [])

    def test_75_substring_title_is_not_handoff_identity(self):
        import subprocess

        from atlas.chat_audit import AuditFinding
        from atlas.chat_audit_github import GitHubAIWorkHandoff

        packet = self._blank_packet()
        finding = AuditFinding(
            finding_id="stable-id",
            unit="changed_code",
            summary="identity",
            severity="P1",
        )
        marker = "<!-- atlas-chat-audit-finding-id:stable-id -->"
        edits: list[int] = []

        def runner(argv: list[str], cwd: str):
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": 77,
                            "title": "Unrelated stable-id migration note",
                            "state": "OPEN",
                            "body": "ordinary unrelated issue body",
                            "url": f"https://github.com/{REPO}/issues/77",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                edits.append(77)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        handoff = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        with self.assertRaises(ValidationError):
            handoff._update_existing_claim(
                claim={
                    "issue_number": 77,
                    "finding_id": "stable-id",
                    "run_key": "old",
                    "head": HEAD_B,
                },
                packet=packet,
                safe=finding,
                title="[AI Work] Audit finding: stable-id",
                body=f"{marker}\nTARGET_REPO={REPO}\n",
                marker=marker,
            )
        self.assertEqual(edits, [])

    def test_76_discover_excludes_explicit_wrong_target_repo(self):
        import subprocess

        from atlas.chat_audit_github import discover_checkpoint_issue

        def runner_for(items: list[dict[str, Any]]):
            def runner(argv: list[str], cwd: str):
                if argv[:2] == ["gh", "api"] and "/contents/" in " ".join(argv):
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                if argv[:3] == ["gh", "issue", "list"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(items), stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="unexpected"
                )

            return runner

        workstream = (
            "WORKSTREAM=continuous-chat-audit-supervisor-poc\nSTATUS=ACTIVE\n"
        )
        wrong = {
            "number": 99,
            "title": "[AI Work] other repo",
            "body": workstream + "TARGET_REPO=other-owner/other-repo\n",
        }
        legacy = {
            "number": 7,
            "title": "[AI Work] legacy",
            "body": workstream,
        }
        matching = {
            "number": 20,
            "title": "[AI Work] this repo",
            "body": workstream + f"TARGET_REPO={REPO}\n",
        }
        with self.assertRaises(ValidationError):
            discover_checkpoint_issue(
                repository=REPO, command_runner=runner_for([wrong])
            )
        self.assertEqual(
            discover_checkpoint_issue(
                repository=REPO,
                command_runner=runner_for([wrong, legacy, matching]),
            ),
            20,
        )
        self.assertEqual(
            discover_checkpoint_issue(
                repository=REPO, command_runner=runner_for([wrong, legacy])
            ),
            7,
        )

    def test_77_marker_search_finds_issue_beyond_title_page(self):
        import subprocess

        from atlas.chat_audit import AuditFinding
        from atlas.chat_audit_github import (
            HANDOFF_MARKER_SEARCH_LIMIT,
            GitHubAIWorkHandoff,
        )

        packet = self._blank_packet()
        finding = AuditFinding(
            finding_id="beyond-page",
            unit="changed_code",
            summary="older handoff",
            severity="P2",
        )
        marker = "<!-- atlas-chat-audit-finding-id:beyond-page -->"
        creates: list[int] = []
        searches: list[str] = []

        def runner(argv: list[str], cwd: str):
            joined = " ".join(argv)
            if argv[:2] == ["gh", "api"] and "--method" not in argv:
                if "/contents/" in joined:
                    return subprocess.CompletedProcess(
                        argv, 1, stdout="", stderr="Not Found (HTTP 404)"
                    )
                if "/git/ref/heads/" in joined:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"object": {"sha": "1" * 40}}),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"default_branch": "main"}), stderr=""
                )
            if argv[:2] == ["gh", "api"] and "--method" in argv and "/contents/" in joined:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"content": {"sha": "claim-sha"}}),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "list"]:
                search = argv[argv.index("--search") + 1]
                searches.append(search)
                if marker not in search:
                    decoys = [
                        {
                            "number": 1000 + i,
                            "title": "[AI Work] other",
                            "body": "no marker",
                            "url": f"https://github.com/{REPO}/issues/{1000 + i}",
                        }
                        for i in range(60)
                    ]
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(decoys), stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "number": 77,
                                "title": "[AI Work] Audit finding: beyond-page",
                                "body": (
                                    f"{marker}\nTARGET_REPO={REPO}\nSTATUS=ACTIVE\n"
                                ),
                                "url": f"https://github.com/{REPO}/issues/77",
                            }
                        ]
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "create"]:
                creates.append(901)
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=f"https://github.com/{REPO}/issues/901\n",
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        handoff = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        record = handoff.upsert_implementation_packet(packet, finding)
        self.assertEqual(creates, [])
        self.assertEqual(record["issue_number"], 77)
        self.assertEqual(record["claim"], "reconciled_marker")
        self.assertTrue(searches)
        self.assertTrue(all(marker in item for item in searches))
        self.assertGreater(HANDOFF_MARKER_SEARCH_LIMIT, 50)

        def truncated(argv: list[str], cwd: str):
            if argv[:3] == ["gh", "issue", "list"]:
                page = [
                    {
                        "number": i,
                        "title": "[AI Work] filler",
                        "body": "no",
                        "url": f"https://github.com/{REPO}/issues/{i}",
                    }
                    for i in range(1, HANDOFF_MARKER_SEARCH_LIMIT + 1)
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(page), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        limited = GitHubAIWorkHandoff(repository=REPO, command_runner=truncated)
        with self.assertRaises(ValidationError) as raised:
            limited._find_issue_by_marker(marker)
        self.assertIn("truncated", str(raised.exception))

    def test_78_checkpoint_over_contents_inline_limit_is_not_put(self):
        from atlas.chat_audit_github import (
            CHECKPOINT_MAX_BYTES,
            GitHubContentsCheckpointStore,
        )

        state: dict[str, Any] = {"raw": None, "sha": None, "puts": 0}
        store = GitHubContentsCheckpointStore(
            repository=REPO,
            issue_number=20,
            command_runner=self._contents_runner(state),
        )
        packet = self._blank_packet()
        self.assertIsNone(store.load())
        packet.session.notes = "n" * (CHECKPOINT_MAX_BYTES + 1)
        with self.assertRaises(ValidationError) as raised:
            store.save(packet)
        self.assertIn("inline limit", str(raised.exception))
        self.assertEqual(state["puts"], 0)
        self.assertEqual(packet.canonical_revision, 0)

        from atlas.chat_audit_github import assert_checkpoint_inline_size

        at_limit = "n" * CHECKPOINT_MAX_BYTES
        assert_checkpoint_inline_size(at_limit)
        with self.assertRaises(ValidationError):
            assert_checkpoint_inline_size(at_limit + "n")
        small_state: dict[str, Any] = {"raw": None, "sha": None, "puts": 0}
        small_store = GitHubContentsCheckpointStore(
            repository=REPO,
            issue_number=20,
            command_runner=self._contents_runner(small_state),
        )
        small = self._blank_packet()
        self.assertIsNone(small_store.load())
        small_store.save(small)
        self.assertEqual(small_state["puts"], 1)
        loaded = small_store.load()
        assert loaded is not None
        self.assertEqual(loaded.canonical_revision, 1)

        def missing_content(argv: list[str], cwd: str):
            import subprocess

            if argv[:2] == ["gh", "api"] and "/contents/" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {"type": "file", "sha": "blob-only", "content": ""}
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unexpected"
            )

        bare = GitHubContentsCheckpointStore(
            repository=REPO,
            issue_number=20,
            command_runner=missing_content,
        )
        with self.assertRaises(ValidationError) as missing:
            bare.load()
        self.assertIn("missing content", str(missing.exception))

    def test_79_work_packet_gate_persists_through_same_head_coordination(self):
        import subprocess

        from atlas.chat_audit_github import GitHubCoordinationRefresher
        from atlas.chat_audit import sanitize_coordination_snapshot

        old = "cccccccccccccccccccccccccccccccccccccccc"
        historical = []
        for index in range(40):
            root_id = 1000 + index
            historical.append(
                {
                    "id": root_id,
                    "user": {"login": "reviewer"},
                    "body": "P1 historical finding " + ("y" * 400),
                    "commit_id": HEAD_A,
                    "original_commit_id": old,
                }
            )
            historical.append(
                {
                    "id": 2000 + index,
                    "in_reply_to_id": root_id,
                    "user": {"login": "author"},
                    "body": "RESOLUTION=RESOLVED",
                    "isResolved": True,
                    "commit_id": HEAD_A,
                    "original_commit_id": old,
                }
            )

        def runner_for(gate: str):
            def runner(argv: list[str], cwd: str):
                if argv[:3] == ["gh", "issue", "view"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            {
                                "number": 20,
                                "title": "wp",
                                "state": "OPEN",
                                "body": (
                                    "WORKSTREAM=continuous-chat-audit-supervisor-poc\n"
                                    f"STATUS=ACTIVE\nTARGET_REPO={REPO}\n"
                                    f"GATE={gate}\n"
                                ),
                                "updatedAt": "t",
                            }
                        ),
                        stderr="",
                    )
                if argv[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                {
                                    "number": 21,
                                    "title": "p",
                                    "state": "OPEN",
                                    "headRefOid": HEAD_A,
                                    "url": "u",
                                }
                            ]
                        ),
                        stderr="",
                    )
                if argv[:3] == ["gh", "pr", "checks"]:
                    out = (
                        "adoption-compliance / compliance\tpass\t1s\t0\t\n"
                        "enforcement-reconcile / reconcile\tpass\t1s\t0\t\n"
                        "affected-tests / affected\tpass\t1s\t0\t\n"
                    )
                    return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
                if len(argv) >= 3 and argv[2] == "graphql":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        "pullRequest": {
                                            "reviewThreads": {
                                                "pageInfo": {
                                                    "hasNextPage": False,
                                                    "endCursor": None,
                                                },
                                                "nodes": [],
                                            }
                                        }
                                    }
                                }
                            }
                        ),
                        stderr="",
                    )
                if "--paginate" in argv and "--slurp" in argv:
                    path = argv[-1]
                    if path.endswith("/reviews"):
                        payload = [
                            [
                                {
                                    "body": "exact-head review, no finding",
                                    "state": "COMMENTED",
                                    "commit_id": HEAD_A,
                                }
                            ]
                        ]
                    elif path.endswith("/pulls/21/comments"):
                        payload = [historical]
                    else:
                        payload = [
                            [
                                {
                                    "body": (
                                        "@codex review this exact HEAD. Do not merge."
                                    ),
                                    "user": {"login": "RickLee-kr"},
                                }
                            ]
                        ]
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(payload), stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="unexpected"
                )

            return runner

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(tmp)
            ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
            while ctl.run_slice()["action"] != "queue_complete":
                pass
            ctl.coordination = GitHubCoordinationRefresher(
                repository=REPO,
                work_packet_issue=20,
                command_runner=runner_for("CHATGPT_EXACT_HEAD_REWORK"),
            )
            blocked = ctl.run_slice()
            self.assertEqual(blocked["action"], "cheap_no_change_rework")
            self.assertEqual(blocked["outcome"], "HUMAN_REQUIRED")
            self.assertNotIn("reviews_incomplete", blocked["coordination"]["reasons"])
            self.assertEqual(
                blocked["coordination"]["work_packet"]["gate"],
                "CHATGPT_EXACT_HEAD_REWORK",
            )
            self.assertEqual(blocked["coordination"]["reviews"]["status"], "OK")
            stored = ctl.store.load()
            assert stored is not None
            assert stored.last_coordination_refresh is not None
            self.assertEqual(
                stored.last_coordination_refresh["work_packet"]["gate"],
                "CHATGPT_EXACT_HEAD_REWORK",
            )
            poisoned = copy.deepcopy(stored.last_coordination_refresh)
            poisoned["work_packet"]["extra"] = "nope"
            with self.assertRaises(ValidationError) as rejected:
                sanitize_coordination_snapshot(poisoned)
            self.assertIn("unsupported fields: extra", str(rejected.exception))

            ctl.coordination = GitHubCoordinationRefresher(
                repository=REPO,
                work_packet_issue=20,
                command_runner=runner_for("READY"),
            )
            passed = ctl.run_slice()
            self.assertEqual(passed["action"], "cheap_no_change")
            self.assertEqual(passed["outcome"], "PASSED")
            self.assertEqual(passed["coordination"]["reviews"]["status"], "OK")
            self.assertFalse(passed["coordination"]["reviews"]["actionable"])
            reloaded = ctl.store.load()
            assert reloaded is not None
            assert reloaded.last_coordination_refresh is not None
            self.assertEqual(
                reloaded.last_coordination_refresh["work_packet"]["gate"], "READY"
            )

    def test_80_checkpoint_pointer_errors_do_not_select_fallback(self):
        import base64
        import subprocess

        from atlas.chat_audit_github import (
            CHECKPOINT_DISCOVERY_SEARCH_LIMIT,
            discover_checkpoint_issue,
        )

        def encoded_pointer(number: int) -> str:
            raw = json.dumps(
                {
                    "workstream": "continuous-chat-audit-supervisor-poc",
                    "issue_number": number,
                }
            ).encode("utf-8")
            return json.dumps(
                {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
            )

        fallback = [
            {
                "number": 99,
                "title": "[AI Work] fallback",
                "body": (
                    "WORKSTREAM=continuous-chat-audit-supervisor-poc\n"
                    "STATUS=ACTIVE\n"
                    f"TARGET_REPO={REPO}\n"
                ),
            }
        ]

        def runner_for(
            *,
            pointer_code: int,
            pointer_stdout: str = "",
            pointer_stderr: str = "",
            view_code: int = 0,
            view_stdout: str = "",
            list_items: list[dict[str, Any]] | None = None,
        ):
            calls = {"list": 0}

            def runner(argv: list[str], cwd: str):
                if argv[:2] == ["gh", "api"] and "/contents/" in " ".join(argv):
                    return subprocess.CompletedProcess(
                        argv,
                        pointer_code,
                        stdout=pointer_stdout,
                        stderr=pointer_stderr,
                    )
                if argv[:3] == ["gh", "issue", "view"]:
                    return subprocess.CompletedProcess(
                        argv, view_code, stdout=view_stdout, stderr="rate limit exceeded"
                    )
                if argv[:3] == ["gh", "issue", "list"]:
                    calls["list"] += 1
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(list_items if list_items is not None else fallback),
                        stderr="",
                    )
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

            return runner, calls

        missing, missing_calls = runner_for(
            pointer_code=1, pointer_stderr="Not Found (HTTP 404)"
        )
        self.assertEqual(
            discover_checkpoint_issue(repository=REPO, command_runner=missing),
            99,
        )
        self.assertEqual(missing_calls["list"], 1)

        limited, limited_calls = runner_for(
            pointer_code=1, pointer_stderr="rate limit exceeded"
        )
        with self.assertRaises(ValidationError) as rate:
            discover_checkpoint_issue(repository=REPO, command_runner=limited)
        self.assertIn("pointer unavailable", str(rate.exception))
        self.assertEqual(limited_calls["list"], 0)

        malformed, malformed_calls = runner_for(
            pointer_code=0, pointer_stdout="not-json"
        )
        with self.assertRaises(ValidationError) as bad:
            discover_checkpoint_issue(repository=REPO, command_runner=malformed)
        self.assertIn("malformed", str(bad.exception))
        self.assertEqual(malformed_calls["list"], 0)

        unverified, unverified_calls = runner_for(
            pointer_code=0,
            pointer_stdout=encoded_pointer(20),
            view_code=1,
        )
        with self.assertRaises(ValidationError) as view_err:
            discover_checkpoint_issue(repository=REPO, command_runner=unverified)
        self.assertIn("verification unavailable", str(view_err.exception))
        self.assertEqual(unverified_calls["list"], 0)

        full_page = [
            {
                "number": index,
                "title": "[AI Work] page",
                "body": "WORKSTREAM=continuous-chat-audit-supervisor-poc\nSTATUS=ACTIVE\n",
            }
            for index in range(1, CHECKPOINT_DISCOVERY_SEARCH_LIMIT + 1)
        ]
        truncated, _calls = runner_for(
            pointer_code=1,
            pointer_stderr="Not Found (HTTP 404)",
            list_items=full_page,
        )
        with self.assertRaises(ValidationError) as page:
            discover_checkpoint_issue(repository=REPO, command_runner=truncated)
        self.assertIn("truncated", str(page.exception))

    def test_81_durable_sanitizer_stays_cheap_on_ordinary_text(self):
        import time

        from atlas.secrets import contains_unsafe_secret, sanitize_durable_text

        ceiling_s = 0.5
        for size in (1000, 4000):
            raw = "x" * size
            started = time.perf_counter()
            cleaned = sanitize_durable_text(raw)
            elapsed = time.perf_counter() - started
            self.assertEqual(cleaned, raw)
            self.assertLess(elapsed, ceiling_s)
        adversarial = ("abc_" * 1000)[:4000]
        near_miss = ("key=" * 800)[:4000]
        for raw in (adversarial, near_miss):
            started = time.perf_counter()
            cleaned = sanitize_durable_text(raw)
            elapsed = time.perf_counter() - started
            self.assertEqual(cleaned, raw)
            self.assertFalse(contains_unsafe_secret(cleaned))
            self.assertLess(elapsed, ceiling_s)
        assigned = "POSTGRES_PASSWORD=hunter2-literal"
        redacted = sanitize_durable_text(assigned)
        self.assertIn("<redacted>", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertFalse(contains_unsafe_secret(redacted))
        over = ("n" * 3990) + "PASSWORD=hunter2-live-value"
        self.assertGreater(len(over), 4000)
        with self.assertRaises(ValidationError) as rejected:
            sanitize_durable_text(over)
        self.assertIn("exceeds 4000", str(rejected.exception))

    def test_82_completed_unit_canonical_key_collision_rejected(self):
        from atlas.chat_audit import AuditControlPacket, AuditEvidence, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A,
            "audit_status": "IDLE",
            "audit_queue": [
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            "idempotency_run_key": run_key,
        }

        def entry(outcome: str) -> dict[str, Any]:
            return {
                "unit": "changed_code",
                "target_sha": HEAD_A,
                "outcome": outcome,
                "findings": (
                    [
                        {
                            "finding_id": "finding-1",
                            "unit": "changed_code",
                            "summary": "kept",
                            "severity": "P1",
                        }
                    ]
                    if outcome == "FINDING"
                    else []
                ),
                "evidence": AuditEvidence(
                    status="COMPLETE",
                    unit="changed_code",
                    target_sha=HEAD_A,
                    notes="ok",
                    truncated=False,
                ).to_dict(),
                "audit_request": "",
            }

        finding_key = f"{run_key}:changed_code:{HEAD_A}"
        alias_key = f"{run_key}:changed_code:{HEAD_A.upper()}"
        orders = (
            [(finding_key, entry("FINDING")), (alias_key, entry("PASS"))],
            [(alias_key, entry("PASS")), (finding_key, entry("FINDING"))],
        )
        for pairs in orders:
            with self.assertRaises(ValidationError) as rejected:
                AuditControlPacket.from_dict(
                    {**base, "completed_units": dict(pairs)}
                )
            self.assertIn("canonical key collision", str(rejected.exception))

    def test_83_restored_evidence_requires_explicit_fields(self):
        from atlas.chat_audit import AuditEvidence, sanitize_slice_dict

        complete = {
            "status": "COMPLETE",
            "unit": "changed_code",
            "target_sha": HEAD_A,
            "notes": "ok",
            "truncated": False,
        }
        loaded = AuditEvidence.from_dict(complete)
        self.assertFalse(loaded.truncated)
        self.assertEqual(loaded.status, "COMPLETE")
        for key in ("status", "unit", "target_sha"):
            missing = dict(complete)
            del missing[key]
            with self.assertRaises(ValidationError) as rejected:
                AuditEvidence.from_dict(missing)
            self.assertIn(f"evidence.{key} is required", str(rejected.exception))
            self.assertNotIsInstance(rejected.exception, KeyError)
        omitted = dict(complete)
        del omitted["truncated"]
        for raw in (
            omitted,
            {**complete, "truncated": None},
            {**complete, "truncated": 0},
            {**complete, "truncated": "false"},
        ):
            with self.assertRaises(ValidationError) as rejected:
                AuditEvidence.from_dict(raw)
            self.assertIn(
                "evidence.truncated must be an explicit boolean",
                str(rejected.exception),
            )
        with self.assertRaises(ValidationError) as slice_rejected:
            sanitize_slice_dict(
                {
                    "unit": "changed_code",
                    "outcome": "PASS",
                    "evidence": omitted,
                }
            )
        self.assertIn(
            "evidence.truncated must be an explicit boolean",
            str(slice_rejected.exception),
        )

    def test_84_executing_claim_requires_active_slice(self):
        import time

        from atlas.chat_audit import AuditControlPacket, make_run_key

        run_key = make_run_key(REPO, BRANCH, HEAD_A)
        queue = [
            "changed_code",
            "affected_contracts",
            "affected_tests_ci",
            "security_impact",
            "docs_spec_drift",
        ]
        claim = {
            "claim_id": "live-claim",
            "unit": "changed_code",
            "run_key": run_key,
            "target_sha": HEAD_A,
            "state": "executing",
            "claimed_at": time.time(),
            "lease_seconds": 900,
        }
        base = {
            "schema_version": 1,
            "target_repository": REPO,
            "target_branch": BRANCH,
            "current_target_sha": HEAD_A,
            "audit_queue": queue,
            "idempotency_run_key": run_key,
            "slice_claim": claim,
        }
        with self.assertRaises(ValidationError) as idle:
            AuditControlPacket.from_dict(
                {**base, "audit_status": "IDLE", "current_unit": None}
            )
        self.assertIn("audit_status=IN_SLICE", str(idle.exception))
        loaded = AuditControlPacket.from_dict(
            {
                **base,
                "audit_status": "IN_SLICE",
                "current_unit": "changed_code",
                "current_unit_index": 0,
            }
        )
        assert loaded.slice_claim is not None
        self.assertEqual(loaded.slice_claim["state"], "executing")

    def test_85_cache_error_replays_noncompletion_canonical_results(self):
        class CommitThenCacheError(FileCheckpointStore):
            def __init__(self, data_root: Path, predicate):
                super().__init__(data_root)
                self.predicate = predicate
                self.cache_failures = 0

            def save(self, packet):
                super().save(packet)
                if self.cache_failures == 0 and self.predicate(packet):
                    self.cache_failures += 1
                    raise OSError("cache follow-up failed")

        class AwaitExecutor:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def execute(self, packet, unit, audit_request):
                from atlas.chat_audit import AuditEvidence, SliceResult

                self.calls.append((unit, packet.current_target_sha))
                return SliceResult(
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    outcome="AWAITING_EVIDENCE",
                    audit_request=audit_request,
                    evidence=AuditEvidence(
                        status="MISSING",
                        unit=unit,
                        target_sha=packet.current_target_sha,
                        notes="need external evidence",
                        truncated=False,
                    ),
                )

        cases = (
            (
                "TIMEOUT",
                FixedUnitExecutor(timeout_units={"changed_code"}),
                lambda packet: (packet.slice_claim or {}).get("state") == "timed_out",
                "TIMEOUT",
                "IN_SLICE",
                "timed_out",
            ),
            (
                "AWAITING_EVIDENCE",
                AwaitExecutor(),
                lambda packet: packet.audit_status == "AWAITING_EVIDENCE",
                "AWAITING_EVIDENCE",
                "AWAITING_EVIDENCE",
                "awaiting_evidence",
            ),
            (
                "FAILED_CLOSED",
                FixedUnitExecutor(truncate_units={"changed_code"}),
                lambda packet: packet.audit_status == "FAILED_CLOSED",
                "REJECTED",
                "FAILED_CLOSED",
                None,
            ),
        )
        for _name, executor, predicate, outcome, status, claim_state in cases:
            with tempfile.TemporaryDirectory() as tmp:
                store = CommitThenCacheError(Path(tmp) / "data", predicate)
                current = {"head": HEAD_A}

                def identity(current=current):
                    from atlas.chat_audit import Identity

                    return Identity(
                        repository=REPO, branch=BRANCH, head=current["head"]
                    )

                ctl = ChatAuditController(
                    store,
                    executor=executor,
                    handoff=RecordingWorkPacketHandoff(),
                    coordination=FixedCoordinationRefresher(),
                    identity_resolver=identity,
                )
                ctl.initialize(repository=REPO, branch=BRANCH, head=HEAD_A)
                replayed = ctl.run_slice()
                self.assertEqual(replayed["action"], "idempotent_replay", _name)
                self.assertEqual(replayed["reason"], "result_already_committed")
                self.assertEqual(replayed["outcome"], outcome)
                self.assertNotIn("lost or stolen", json.dumps(replayed))
                self.assertEqual(replayed["packet"]["audit_status"], status)
                claim = replayed["packet"]["slice_claim"]
                if claim_state is None:
                    self.assertIsNone(claim)
                else:
                    self.assertEqual(claim["state"], claim_state)
                self.assertEqual(len(executor.calls), 1)
                stored = json.loads(
                    (Path(tmp) / "data" / "chat-audit.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(stored["audit_status"], status)

    def test_86_github_handoff_revalidates_finding_before_write(self):
        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import GitHubAIWorkHandoff

        packet = AuditControlPacket(
            target_repository=REPO,
            target_branch=BRANCH,
            current_target_sha=HEAD_A,
            audit_queue=[
                "changed_code",
                "affected_contracts",
                "affected_tests_ci",
                "security_impact",
                "docs_spec_drift",
            ],
            idempotency_run_key=make_run_key(REPO, BRANCH, HEAD_A),
        )

        def runner(argv, cwd):
            raise AssertionError(f"github write attempted: {argv}")

        handoff = GitHubAIWorkHandoff(repository=REPO, command_runner=runner)
        for finding in (
            AuditFinding(
                finding_id="OPENAI_API_KEY:hunter2",
                unit="changed_code",
                summary="x",
                severity="P1",
            ),
            AuditFinding(
                finding_id="ok-1",
                unit="changed_code",
                summary="x",
                severity="CRITICAL",
            ),
        ):
            with self.assertRaises(ValidationError):
                handoff.upsert_implementation_packet(packet, finding)


if __name__ == "__main__":
    unittest.main()
