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
    CycleAdvanceResult,
    DispatchResult,
    DispatchSpawnedButUnobservedError,
    DispatchSpawnCleanupUncertainError,
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
            enforce_worktree_identity=False,
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

    def test_pass_rejected_when_worktree_dirty_at_controller_gate(self):
        """Adapter PASS on a dirty tree must not finalize PASSED."""
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            git_state = {"dirty": ""}

            def fake_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): (
                        "feature/autonomous-work-controller-poc"
                    ),
                    ("git", "rev-parse", "HEAD"): HEAD_A,
                    ("git", "status", "--porcelain", "--untracked-files=all"): (
                        git_state["dirty"]
                    ),
                }
                return mapping[tuple(argv)]

            class DirtyAfterAudit:
                def __init__(self) -> None:
                    self.calls = []

                def audit(self, event, record):
                    self.calls.append((event, record))
                    git_state["dirty"] = " M dirty.py\n"
                    return AuditResult(verdict="PASS", findings="looks good")

            ctl = WorkController(
                Path(tmp) / "data",
                audit=DirtyAfterAudit(),
                work_packet=RecordingWorkPacketAdapter(),
                dispatcher=RecordingCursorDispatcher(),
                enforce_worktree_identity=True,
                git_runner=fake_git,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertIn("PASS rejected", outcome["findings"])
            self.assertIn("dirty", outcome["findings"].lower())
            self.assertEqual(ctl.show("awc-poc")["state"], "HUMAN_REQUIRED")

    def test_rework_rejected_before_packet_mutation_when_dirty(self):
        """Fixed/OpenAI-style REWORK must not mutate packet on dirty porcelain."""
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()

            def fake_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): (
                        "feature/autonomous-work-controller-poc"
                    ),
                    ("git", "rev-parse", "HEAD"): HEAD_A,
                    ("git", "status", "--porcelain", "--untracked-files=all"): (
                        " M dirty-rework.py\n"
                    ),
                }
                return mapping[tuple(argv)]

            packets = RecordingWorkPacketAdapter()
            dispatcher = RecordingCursorDispatcher()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=dispatcher,
                enforce_worktree_identity=True,
                git_runner=fake_git,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "rework_unclean_snapshot")
            self.assertIn("REWORK rejected", outcome["findings"])
            self.assertIn("dirty", outcome["findings"].lower())
            self.assertEqual(packets.updates, [])
            self.assertEqual(dispatcher.requests, [])

    def test_rework_rejected_when_head_changes_during_clean_check(self):
        """A commit between identity and porcelain must not mutate or dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            git_state = {"head": HEAD_A}

            def fake_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
                    git_state["head"] = HEAD_B
                    return ""
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): (
                        "feature/autonomous-work-controller-poc"
                    ),
                    ("git", "rev-parse", "HEAD"): git_state["head"],
                }
                return mapping[tuple(argv)]

            packets = RecordingWorkPacketAdapter()
            dispatcher = RecordingCursorDispatcher()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=dispatcher,
                enforce_worktree_identity=True,
                git_runner=fake_git,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "rework_unclean_snapshot")
            self.assertIn("head mismatch", outcome["findings"])
            self.assertEqual(packets.updates, [])
            self.assertEqual(dispatcher.requests, [])

    def test_resource_preflight_block_is_human_required_without_spawn(self):
        from atlas.work_controller import PersistSession, PtyPersistCursorDispatcher

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            existing = [
                PersistSession(session_id="keep-me", workspace="/tmp/unrelated")
            ]
            state = {"spawn": 0, "stopped": []}

            def _refuse_spawn(_command: list[str], _worktree: str) -> int:
                state["spawn"] += 1
                return 1

            def fake_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): (
                        "feature/autonomous-work-controller-poc"
                    ),
                    ("git", "rev-parse", "HEAD"): HEAD_A,
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
                }
                return mapping[tuple(argv)]

            def preflight() -> tuple[int, str]:
                return (
                    2,
                    "RESULT=BLOCK\nEXIT_CODE=2\nREASON=persistent sessions reached block threshold\n",
                )

            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=lambda: list(existing),
                list_target_procs=lambda _wt: [(111, "keep")],
                spawn=_refuse_spawn,
                git_runner=fake_git,
                resource_preflight=preflight,
                terminate_process_group=lambda pid: state["stopped"].append(pid),
                poll_interval_sec=0.01,
                poll_timeout_sec=0.05,
                sleeper=lambda _s: None,
            )
            packets = RecordingWorkPacketAdapter()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=dispatcher,
                enforce_worktree_identity=True,
                git_runner=fake_git,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "resource_preflight_blocked")
            self.assertEqual(outcome["resource_preflight_result"], "BLOCK")
            self.assertIn("block threshold", outcome["resource_preflight_reason"])
            self.assertEqual(state["spawn"], 0)
            self.assertEqual(state["stopped"], [])
            self.assertEqual(existing[0].session_id, "keep-me")
            self.assertTrue(
                any(item.get("kind") == "dispatch_blocked" for item in packets.updates)
            )

    def test_pass_rejected_when_worktree_already_dirty_before_finalize(self):
        """Dirty porcelain present for the whole PASS path still fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()

            def fake_git(argv: list[str], cwd: str) -> str:
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
                    return cwd
                # Register only checks identity (not porcelain). Keep HEAD/branch
                # valid while porcelain stays dirty for the PASS gate.
                mapping = {
                    ("git", "remote", "get-url", "origin"): (
                        "datarelay-labs/datarelay-atlas"
                    ),
                    ("git", "branch", "--show-current"): (
                        "feature/autonomous-work-controller-poc"
                    ),
                    ("git", "rev-parse", "HEAD"): HEAD_A,
                    ("git", "status", "--porcelain", "--untracked-files=all"): (
                        " M already-dirty.py\n"
                    ),
                }
                return mapping[tuple(argv)]

            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="PASS", findings="adapter pass")
                ),
                work_packet=RecordingWorkPacketAdapter(),
                dispatcher=RecordingCursorDispatcher(),
                enforce_worktree_identity=True,
                git_runner=fake_git,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertIn("PASS rejected", outcome["findings"])
            self.assertNotEqual(ctl.show("awc-poc")["state"], "PASSED")

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
            self.assertEqual(outcome["resume_prompt"], "/work-resume")
            self.assertEqual(len(dispatcher.requests), 1)
            req = dispatcher.requests[0]
            self.assertEqual(req.resume_prompt, "/work-resume")
            self.assertEqual(req.attempt, 2)
            self.assertEqual(req.repository, "datarelay-labs/datarelay-atlas")
            self.assertEqual(req.expected_head, HEAD_A)
            self.assertEqual(
                build_persist_resume_command(req),
                ["agent", "persist", "--force", "--trust", "/work-resume"],
            )
            self.assertEqual(
                build_persist_resume_command(req),
                outcome["dispatch_command"],
            )
            self.assertEqual(len(packets.updates), 1)
            self.assertIn("fix gaps", packets.updates[0]["findings"])
            self.assertEqual(ctl.show("awc-poc")["attempt"], 2)

    def test_dispatch_boundary_validation_error_finalizes_human_required(self):
        """Dispatcher ValidationError after REWORK ⇒ HUMAN_REQUIRED, no dispatch."""

        class FailingDispatcher:
            def __init__(self) -> None:
                self.requests = []

            def start_resume(self, request):
                self.requests.append(request)
                raise ValidationError("worktree is dirty; git status --porcelain is not empty")

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            dispatcher = FailingDispatcher()
            packets = RecordingWorkPacketAdapter()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=dispatcher,
                observer=RecordingObserver(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
                max_attempts=3,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["action"], "stop")
            self.assertEqual(outcome["reason"], "dispatch_boundary_failed")
            self.assertIn("dispatch blocked at boundary", outcome["findings"])
            self.assertEqual(len(dispatcher.requests), 1)
            self.assertEqual(len(packets.updates), 2)
            self.assertEqual(packets.updates[0]["kind"], "rework")
            self.assertEqual(packets.updates[1]["kind"], "dispatch_blocked")
            self.assertNotEqual(outcome["state"], "REWORK_DISPATCHED")
            shown = ctl.show("awc-poc")
            self.assertEqual(shown["state"], "HUMAN_REQUIRED")
            self.assertEqual(shown["attempt"], 0)

    def test_work_packet_mutation_failure_blocks_dispatch(self):
        """Work Packet mutation ValidationError ⇒ HUMAN_REQUIRED, no Cursor spawn."""

        class FailingWorkPacket:
            def __init__(self) -> None:
                self.calls = 0

            def apply_rework_findings(self, **kwargs):
                self.calls += 1
                raise ValidationError("gh issue edit failed")

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            dispatcher = RecordingCursorDispatcher()
            packets = FailingWorkPacket()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=dispatcher,
                observer=RecordingObserver(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
                max_attempts=3,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["verdict"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "work_packet_mutation_failed")
            self.assertIn("work packet mutation failed", outcome["findings"])
            self.assertEqual(packets.calls, 1)
            self.assertEqual(dispatcher.requests, [])
            self.assertNotEqual(outcome["state"], "REWORK_DISPATCHED")

    def test_work_packet_mutation_happens_before_dispatch(self):
        """REWORK ordering: mutate canonical packet, then dispatch Cursor."""

        class OrderedProbe:
            def __init__(self) -> None:
                self.order: list[str] = []

            def apply_rework_findings(self, **kwargs):
                self.order.append("packet")

            def start_resume(self, request):
                self.order.append("dispatch")
                return DispatchResult(
                    session_id="sess-ordered",
                    command=build_persist_resume_command(request),
                )

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            probe = OrderedProbe()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=probe,
                dispatcher=probe,
                observer=RecordingObserver(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
                max_attempts=3,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "REWORK_DISPATCHED")
            self.assertEqual(probe.order, ["packet", "dispatch"])

    def test_spawned_but_unobserved_requires_human(self):
        """Unobserved spawn is not a proven dispatch; compensate and stop."""

        class ObservingFailDispatcher:
            def __init__(self) -> None:
                self.requests = []

            def start_resume(self, request):
                self.requests.append(request)
                raise DispatchSpawnedButUnobservedError(
                    "session did not appear",
                    session_hint="proc:4242",
                    command=build_persist_resume_command(request),
                )

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            dispatcher = ObservingFailDispatcher()
            packets = RecordingWorkPacketAdapter()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=dispatcher,
                observer=RecordingObserver(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
                max_attempts=3,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "spawned_but_unobserved")
            self.assertEqual(outcome["dispatch_session_hint"], "proc:4242")
            # Packet mutation then compensating blocked update.
            self.assertEqual(len(packets.updates), 2)
            self.assertNotEqual(ctl.show("awc-poc")["state"], "REWORK_DISPATCHED")
            replay = ctl.handle_completion(self._event())
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(len(packets.updates), 2)
            self.assertEqual(len(dispatcher.requests), 1)

    def test_cleanup_uncertainty_does_not_compensate_packet(self):
        """Termination failure must not rewrite the packet as safely blocked."""

        class UncertainDispatcher:
            def start_resume(self, request):
                raise DispatchSpawnCleanupUncertainError(
                    "owned spawn cleanup uncertain: operation not permitted",
                    session_hint="proc:4242",
                    command=build_persist_resume_command(request),
                    cleanup_error="operation not permitted",
                )

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            packets = RecordingWorkPacketAdapter()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=FixedAuditAdapter(
                    AuditResult(verdict="REWORK", findings="fix gaps")
                ),
                work_packet=packets,
                dispatcher=UncertainDispatcher(),
                observer=RecordingObserver(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
                max_attempts=3,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "spawn_cleanup_uncertain")
            self.assertNotEqual(outcome["state"], "REWORK_DISPATCHED")
            self.assertEqual(
                [item["kind"] for item in packets.updates], ["rework"]
            )
            shown = ctl.show("awc-poc")
            self.assertIn("not compensated", shown["last_findings"])
            self.assertIn("proc:4242", shown["last_findings"])

    def test_secret_blocked_codex_audit_stops_without_reconcile_loop(self):
        from atlas.codex_audit import CodexAuditProvider

        secret = "OPENAI_API_KEY=" + ("x" * 24)
        calls = {"n": 0}

        class CountingAudit(CodexAuditProvider):
            def audit(self, event, record):
                calls["n"] += 1
                return super().audit(event, record)

        def boom(command, prompt, cwd):
            raise AssertionError("codex runner must not be called")

        provider = CountingAudit(
            runner=boom,
            require_identity=False,
            evidence_bundle={
                "schema": "awc.codex_evidence_bundle.v1",
                "git": {"evidence_status": "OK", "status": "clean"},
                "note": secret,
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            ctl = WorkController(
                Path(tmp) / "data",
                audit=provider,
                work_packet=RecordingWorkPacketAdapter(),
                dispatcher=RecordingCursorDispatcher(),
                observer=RecordingObserver(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/autonomous-work-controller-poc",
                worktree_path=str(worktree),
                expected_head=HEAD_A,
                max_attempts=3,
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertNotEqual(outcome["state"], "REWORK_DISPATCHED")
            findings = ctl.show("awc-poc")["last_findings"]
            self.assertIn("credential-like material", findings)
            self.assertNotIn(secret, findings)
            self.assertNotIn("x" * 24, findings)
            self.assertEqual(ctl.show("awc-poc")["state"], "HUMAN_REQUIRED")
            reconciled = ctl.reconcile("awc-poc")
            self.assertEqual(calls["n"], 1)
            self.assertEqual(reconciled[0]["state"], "HUMAN_REQUIRED")
            self.assertFalse(reconciled[0].get("reconciled", False))

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
                enforce_worktree_identity=False,
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

    def _advanced(self) -> CycleAdvanceResult:
        return CycleAdvanceResult(
            kind="advanced",
            successor_issue=18,
            successor_branch="feature/autonomous-work-controller-poc",
            successor_workstream="next-cycle",
            transition_id=f"12:{HEAD_A}:evt-1",
        )

    def test_pass_without_successor_stops_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "PASSED")
            self.assertEqual(outcome["cycle"], "no_successor")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(packets.updates, [])
            self.assertEqual(len(packets.cycle_calls), 1)

    def test_rework_and_human_required_do_not_advance_cycle(self):
        for verdict in ("REWORK", "HUMAN_REQUIRED"):
            with tempfile.TemporaryDirectory() as tmp:
                ctl, dispatcher, packets = self._ctl(tmp, verdict=verdict)
                outcome = ctl.handle_completion(self._event())
                self.assertEqual(packets.cycle_calls, [])
                if verdict == "REWORK":
                    self.assertEqual(outcome["state"], "REWORK_DISPATCHED")
                else:
                    self.assertEqual(dispatcher.requests, [])
                    self.assertEqual(outcome["state"], "HUMAN_REQUIRED")

    def test_pass_activates_successor_and_dispatches_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            packets.cycle_script = [self._advanced()]
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["action"], "successor_dispatched")
            self.assertEqual(outcome["state"], "SUCCESSOR_DISPATCHED")
            self.assertEqual(len(dispatcher.requests), 1)
            request = dispatcher.requests[0]
            self.assertEqual(request.issue_number, 18)
            self.assertEqual(request.attempt, 1)
            self.assertEqual(request.resume_prompt, "/work-resume")
            self.assertIn("/work-resume", outcome["dispatch_command"])
            shown = ctl.show("awc-poc")
            self.assertEqual(shown["issue_number"], 18)
            self.assertEqual(shown["attempt"], 0)
            self.assertEqual(shown["expected_head"], HEAD_A)
            self.assertIn("evt-1", shown["processed_event_ids"])
            replay = ctl.handle_completion(self._event())
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(len(dispatcher.requests), 1)
            self.assertEqual(len(packets.cycle_calls), 1)

    def test_ambiguous_cycle_does_not_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            packets.cycle_result = CycleAdvanceResult(
                kind="human_required",
                reason="ambiguous queued successors: #18, #19",
            )
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "cycle_human_required")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(ctl.show("awc-poc")["issue_number"], 12)

    def test_dispatch_failure_leaves_one_active_successor_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, _dispatcher, packets = self._ctl(tmp, verdict="PASS")
            packets.cycle_script = [self._advanced()]

            class RefusingDispatcher:
                def __init__(self) -> None:
                    self.calls = 0

                def start_resume(self, request):
                    self.calls += 1
                    raise ValidationError("spawn refused")

            refusing = RefusingDispatcher()
            ctl.dispatcher = refusing
            outcome = ctl.handle_completion(self._event())
            self.assertEqual(outcome["state"], "HUMAN_REQUIRED")
            self.assertEqual(outcome["reason"], "cycle_dispatch_blocked")
            self.assertEqual(outcome["successor_issue"], 18)
            self.assertEqual(refusing.calls, 1)
            self.assertEqual(len(packets.cycle_calls), 1)
            self.assertEqual(ctl.show("awc-poc")["issue_number"], 18)

    def test_reconcile_pending_dispatch_once_and_claimed_dispatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            record = ctl.store.get("awc-poc")
            record.state = "CYCLE_DISPATCH_PENDING"
            record.issue_number = 18
            record.attempt = 0
            record.pending_event = self._event()
            ctl.store.put(record)
            outcome = ctl.reconcile("awc-poc")[0]
            self.assertEqual(outcome["state"], "SUCCESSOR_DISPATCHED")
            self.assertEqual(len(dispatcher.requests), 1)
            self.assertEqual(dispatcher.requests[0].issue_number, 18)

        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, _packets = self._ctl(tmp, verdict="PASS")
            record = ctl.store.get("awc-poc")
            record.state = "CYCLE_DISPATCHING"
            record.pending_event = self._event()
            ctl.store.put(record)
            outcome = ctl.reconcile("awc-poc")[0]
            self.assertEqual(outcome["reason"], "cycle_dispatch_uncertain")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(ctl.show("awc-poc")["state"], "HUMAN_REQUIRED")

    def test_successor_completion_accepts_new_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl, dispatcher, packets = self._ctl(tmp, verdict="PASS")
            packets.cycle_script = [self._advanced()]
            ctl.handle_completion(self._event())
            self.assertEqual(len(dispatcher.requests), 1)
            outcome = ctl.handle_completion(
                self._event(
                    event_id="evt-2",
                    issue_number=18,
                    head=HEAD_B,
                    attempt=1,
                )
            )
            self.assertEqual(outcome["state"], "PASSED")
            self.assertEqual(outcome["cycle"], "no_successor")
            self.assertEqual(ctl.show("awc-poc")["expected_head"], HEAD_B)
            self.assertEqual(len(dispatcher.requests), 1)


if __name__ == "__main__":
    unittest.main()
