"""Continuous Chat Audit Supervisor PoC regressions (ADR-0007)."""

from __future__ import annotations

import json
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
            raw.current_unit_index = 0
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
        from atlas.chat_audit import AuditControlPacket, AuditFinding, make_run_key
        from atlas.chat_audit_github import (
            GitHubAIWorkHandoff,
            GitHubIssueCheckpointStore,
            embed_checkpoint_in_issue_body,
            extract_checkpoint_from_issue_body,
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
        body = embed_checkpoint_in_issue_body("existing note", packet)
        restored = extract_checkpoint_from_issue_body(body)
        assert restored is not None
        self.assertEqual(restored.current_target_sha, HEAD_A)

        calls: list[list[str]] = []
        bodies: dict[str, str] = {"body": body}

        def runner(argv: list[str], cwd: str):
            import subprocess

            calls.append(list(argv))
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": 20,
                            "title": "Audit",
                            "body": bodies["body"],
                            "state": "OPEN",
                        }
                    ),
                    stderr="",
                )
            if argv[:3] == ["gh", "issue", "edit"]:
                # body-file path is last or after --body-file
                idx = argv.index("--body-file")
                bodies["body"] = Path(argv[idx + 1]).read_text(encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
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
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCheckpointStore(Path(tmp) / "cache")
            store = GitHubIssueCheckpointStore(
                repository=REPO,
                issue_number=20,
                cache=cache,
                command_runner=runner,
            )
            store.save(packet)
            loaded = store.load()
            assert loaded is not None
            self.assertEqual(loaded.idempotency_run_key, run_key)
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
            # Idempotent update path
            def runner2(argv: list[str], cwd: str):
                import subprocess

                if argv[:3] == ["gh", "issue", "list"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [
                                {
                                    "number": 321,
                                    "title": "[AI Work] Audit finding: gh-1",
                                    "body": (
                                        "<!-- atlas-chat-audit-finding-id:gh-1 -->\n"
                                        "old"
                                    ),
                                    "url": "https://github.com/datarelay-labs/datarelay-atlas/issues/321",
                                }
                            ]
                        ),
                        stderr="",
                    )
                if argv[:3] == ["gh", "issue", "edit"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="", stderr=""
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


if __name__ == "__main__":
    unittest.main()