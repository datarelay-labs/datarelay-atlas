"""Slice D exact-HEAD disposition: REWORK redispatch, PASS checkpoint, stop."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.audit_claim import (
    AuditClaim,
    IssueAuditLedger,
    MemoryClaimStore,
    WorkPacketSnapshot,
    make_audit_claim_key,
)
from atlas.audit_disposition import (
    DeterministicGateSnapshot,
    MemoryPacketStore,
    apply_exact_head_disposition,
    run_completed_audit_disposition,
)
from atlas.chat_audit import CheckpointCasConflict
from atlas.cli import build_parser
from atlas.host_worker import HostWorkerConfig, ProjectDescriptor, actual_host_id, load_host_worker_config
from atlas.provenance import ValidationError
from atlas.work_controller import GitHubWorkPacketAdapter, PersistSession, render_rework_work_packet_body


HEAD = "a" * 40
OTHER = "b" * 40
REPO = "datarelay-labs/datarelay-atlas"
BRANCH = "feature/autonomous-local-supervisor-gpt56-audit"
WORKSTREAM = "autonomous-local-supervisor-gpt56-final-audit"
CHAT = "chat-47-durable"
HOST = "testhost"
SECRET = "OPENAI_API_KEY=sk-fake-secret-1234567890"


def _packet_body(head: str = HEAD) -> str:
    return f"""PACKET_VERSION=1
TARGET_REPO={REPO}
WORKSTREAM={WORKSTREAM}
STATUS=ACTIVE
QUEUE_STATE=NONE
BRANCH={BRANCH}
TASK_KIND=DEVELOPMENT
OWNER_INTENT=Replace unreliable scheduled-Chat orchestration.
LAST_VERIFIED_HEAD={head}
GATE=IMPLEMENTATION
NEXT_ACTION=CURSOR_IMPLEMENT_SLICE_D_AUTONOMOUS_REWORK_REDISPATCH

## Goal

One exact-head disposition.

## Current State

Audited.

## Next Action

Apply the disposition.

## Constraints

- No merge.

## Canonical References

- Issue #47

## Latest Evidence

pending

## Blockers

NONE
"""


def _claim(verdict: str, findings: str = "bounded gap") -> AuditClaim:
    key = make_audit_claim_key(REPO, 47, HEAD)
    return AuditClaim(
        repository=REPO,
        issue_number=47,
        branch=BRANCH,
        target_sha=HEAD,
        claim_key=key,
        state="completed",
        month_id="2023-11",
        claimed_at=1_700_000_000.0,
        lease_seconds=60,
        verdict=verdict,
        findings=findings,
        telemetry={
            "model": "gpt-5.6-sol",
            "input_tokens": 1,
            "output_tokens": 1,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": 0.0,
            "target_sha": HEAD,
            "verdict": verdict,
            "duration_sec": 0.1,
        },
    )


def _snapshot(head: str = HEAD, status: str = "ACTIVE") -> WorkPacketSnapshot:
    return WorkPacketSnapshot(
        repository=REPO,
        issue_number=47,
        branch=BRANCH,
        head=head,
        status=status,
    )


def _gates(head: str = HEAD, **overrides: str) -> DeterministicGateSnapshot:
    fields = {
        "tests_status": "PASS",
        "ci_status": "OK",
        "ci_head": head,
        "review_status": "OK",
        "governance_status": "CURRENT",
        "governance_head": head,
    }
    fields.update(overrides)
    return DeterministicGateSnapshot(**fields)


def _git(head: str, *, dirty: bool = False):
    def runner(argv: list[str], cwd: str) -> str:
        if argv[1:] == ["rev-parse", "--show-toplevel"]:
            return cwd
        if argv[1:] == ["remote", "get-url", "origin"]:
            return f"https://github.com/{REPO}.git"
        if argv[1:] == ["branch", "--show-current"]:
            return BRANCH
        if argv[1:] == ["rev-parse", "HEAD"]:
            return head
        if argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
            return " M dirty" if dirty else ""
        raise ValidationError(f"unexpected git argv {argv}")

    return runner


class SliceDDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = tempfile.TemporaryDirectory()
        self.worktree = self.tmp.name
        self.store = MemoryClaimStore()
        self.packet = MemoryPacketStore(_packet_body())
        self.spawned: list[list[str]] = []
        key = make_audit_claim_key(REPO, 47, HEAD)
        ledger = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            claims={key: _claim("REWORK")},
        )
        self.ledger_sha = self.store.save(ledger, expected_sha=None)
        self.ledger, self.ledger_sha = self.store.load(47)
        assert self.ledger is not None

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.state.cleanup()

    def _spawn(self, code: int = 0):
        def spawn(argv: list[str], cwd: str) -> int:
            self.spawned.append(list(argv))
            return code

        return spawn

    def _apply(self, claim: AuditClaim | None = None, **overrides: object) -> dict:
        ledger, sha = self.store.load(47)
        assert ledger is not None
        fields: dict[str, object] = {
            "claim": claim or ledger.claims[make_audit_claim_key(REPO, 47, HEAD)],
            "ledger": ledger,
            "ledger_sha": sha,
            "claim_store": self.store,
            "packet_store": self.packet,
            "repository": REPO,
            "issue_number": 47,
            "branch": BRANCH,
            "workstream": WORKSTREAM,
            "head": HEAD,
            "packets": [_snapshot()],
            "descriptor": ProjectDescriptor(
                repository=REPO,
                worktree=self.worktree,
                cursor_chat_id=CHAT,
            ),
            "observed_host": HOST,
            "expected_host": HOST,
            "worktree_path": self.worktree,
            "git_runner": _git(HEAD),
            "spawn": self._spawn(),
            "list_sessions": lambda: [],
            "list_processes": lambda _path: [],
            "attempt": 2,
            "gates": _gates(),
            "bugbot_advisory": None,
            "state_root": self.state.name,
            "host_probe": lambda: HOST,
        }
        fields.update(overrides)
        return apply_exact_head_disposition(**fields)  # type: ignore[arg-type]

    def test_rework_mutates_once_and_resumes_the_same_chat(self) -> None:
        outcome = self._apply()
        self.assertEqual(outcome["action"], "redispatched")
        self.assertEqual(outcome["cursor_calls"], 1)
        self.assertEqual(self.packet.mutations, 1)
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0]
        self.assertEqual(argv[:4], ["agent", "--print", "--resume", CHAT])
        self.assertNotIn(CHAT, self.packet.body)
        self.assertIn("bounded gap", self.packet.body)
        self.assertIn("VERDICT=REWORK", self.packet.body)
        self.assertNotIn("SUCCESSOR_ACTIVE", self.packet.body)
        again = self._apply(spawn=self._spawn())
        self.assertEqual(again["action"], "duplicate")
        self.assertEqual(again["cursor_calls"], 0)
        self.assertEqual(self.packet.mutations, 1)
        self.assertEqual(len(self.spawned), 1)

    def test_stale_dirty_active_and_ambiguous_do_not_dispatch(self) -> None:
        stale = self._apply(head=OTHER, packets=[_snapshot(head=OTHER)])
        self.assertEqual(stale["action"], "stale_head")
        dirty = self._apply(git_runner=_git(HEAD, dirty=True))
        self.assertEqual(dirty["action"], "identity_refused")
        active = self._apply(
            list_sessions=lambda: [
                PersistSession(session_id="s", workspace=self.worktree, status="Attached")
            ]
        )
        self.assertEqual(active["action"], "cursor_active_noop")
        ambiguous = self._apply(packets=[])
        self.assertEqual(ambiguous["action"], "ambiguous_packet")
        mismatched = self._apply(
            packet_store=MemoryPacketStore(_packet_body(OTHER))
        )
        self.assertEqual(mismatched["action"], "stale_packet")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.packet.mutations, 0)

    def test_dispatch_failure_keeps_findings_and_replay_does_not_dispatch(self) -> None:
        outcome = self._apply(spawn=self._spawn(code=1))
        self.assertEqual(outcome["action"], "dispatch_blocked")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertIn("bounded gap", outcome["findings"] or "")
        self.assertIn("bounded gap", self.packet.body)
        self.assertIn("WORK_PACKET_MUTATION=DISPATCH_BLOCKED", self.packet.body)
        self.assertNotIn(CHAT, self.packet.body)
        self.assertEqual(self.packet.mutations, 2)
        replay = self._apply(spawn=self._spawn())
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.packet.mutations, 2)

    def test_compensation_conflict_keeps_the_finding_set(self) -> None:
        class FailCompensate(MemoryPacketStore):
            def cas_save(self, body: str, *, expected_body: str, expected_token: str) -> None:
                if self.mutations >= 1:
                    raise CheckpointCasConflict("compensate lost")
                super().cas_save(
                    body, expected_body=expected_body, expected_token=expected_token
                )

        packet = FailCompensate(_packet_body())
        outcome = self._apply(packet_store=packet, spawn=self._spawn(code=1))
        self.assertEqual(outcome["action"], "compensation_failed")
        self.assertIn("bounded gap", outcome["findings"] or "")
        self.assertIn("bounded gap", packet.body)
        self.assertEqual(len(self.spawned), 1)
        replay = self._apply(packet_store=packet, spawn=self._spawn())
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(len(self.spawned), 1)

    def test_pass_without_current_gates_does_not_advance(self) -> None:
        claim = _claim("PASS")
        stale = self._apply(claim=claim, gates=_gates(ci_status="PENDING"))
        self.assertEqual(stale["action"], "no_advancement")
        self.assertEqual(stale["cursor_calls"], 0)
        self.assertEqual(self.packet.mutations, 0)
        self.assertNotIn("AWAITING_EXACT_HEAD_GOVERNANCE", self.packet.body)
        missing = self._apply(claim=claim, gates=None)
        self.assertEqual(missing["action"], "no_advancement")
        self.assertEqual(self.spawned, [])

    def test_pass_with_current_gates_is_a_governance_checkpoint_only(self) -> None:
        outcome = self._apply(claim=_claim("PASS"), bugbot_advisory="review the bound diff")
        self.assertEqual(outcome["action"], "pass_checkpoint")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.packet.mutations, 1)
        self.assertIn("NEXT_ACTION=AWAITING_EXACT_HEAD_GOVERNANCE", self.packet.body)
        self.assertIn(f"AUDIT_BASE_HEAD={HEAD}", self.packet.body)
        self.assertIn("WORK_PACKET_MUTATION=GOVERNANCE_CHECKPOINT", self.packet.body)
        self.assertIn("SUCCESSOR=NONE", self.packet.body)
        self.assertIn("review the bound diff", self.packet.body)
        self.assertNotIn(CHAT, self.packet.body)
        self.assertNotIn("STATUS=COMPLETE", self.packet.body)
        self.assertEqual(self.spawned, [])
        absent = MemoryPacketStore(_packet_body())
        fresh = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        fresh.save(
            IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("PASS")},
            ),
            expected_sha=None,
        )
        loaded, sha = fresh.load(47)
        assert loaded is not None
        quiet = self._apply(
            claim=_claim("PASS"),
            packet_store=absent,
            claim_store=fresh,
            ledger=loaded,
            ledger_sha=sha,
            bugbot_advisory="ABSENT",
        )
        self.assertEqual(quiet["action"], "pass_checkpoint")
        self.assertNotIn("Bugbot advisory", absent.body)
        self.assertEqual(absent.mutations, 1)

    def _based(self) -> MemoryPacketStore:
        needle = f"LAST_VERIFIED_HEAD={HEAD}\n"
        body = _packet_body().replace(needle, f"{needle}AUDIT_BASE_HEAD={OTHER}\n")
        return MemoryPacketStore(body)

    def test_pass_advances_audit_base(self) -> None:
        blocked = self._based()
        stalled = self._apply(claim=_claim("PASS"), packet_store=blocked, gates=None)
        self.assertEqual((stalled["action"], blocked.mutations), ("no_advancement", 0))
        self.assertIn(f"AUDIT_BASE_HEAD={OTHER}", blocked.body)
        advanced = self._based()
        done = self._apply(claim=_claim("PASS"), packet_store=advanced, gates=_gates())
        self.assertEqual(done["action"], "pass_checkpoint")
        self.assertIn(f"AUDIT_BASE_HEAD={HEAD}", advanced.body)
        self.assertNotIn(f"AUDIT_BASE_HEAD={OTHER}", advanced.body)

    def test_rework_keeps_audit_base(self) -> None:
        packet = self._based()
        self.assertEqual(self._apply(packet_store=packet)["action"], "redispatched")
        self.assertIn(f"AUDIT_BASE_HEAD={OTHER}", packet.body)
        self.assertNotIn(f"AUDIT_BASE_HEAD={HEAD}", packet.body)

    def test_human_required_keeps_audit_base(self) -> None:
        packet = self._based()
        outcome = self._apply(claim=_claim("HUMAN_REQUIRED"), packet_store=packet)
        self.assertEqual((outcome["action"], packet.mutations), ("human_required", 0))
        self.assertIn(f"AUDIT_BASE_HEAD={OTHER}", packet.body)

    def test_human_required_persists_reason_without_dispatch(self) -> None:
        outcome = self._apply(claim=_claim("HUMAN_REQUIRED", findings="owner must decide"))
        self.assertEqual(outcome["action"], "human_required")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.packet.mutations, 0)
        self.assertIn("owner must decide", outcome["findings"] or "")
        loaded, _sha = self.store.load(47)
        assert loaded is not None
        key = make_audit_claim_key(REPO, 47, HEAD)
        self.assertEqual(loaded.dispositions[key]["action"], "human_required")
        self.assertNotIn(CHAT, loaded.to_json())
        writes = self.store.writes
        replay = self._apply(claim=_claim("HUMAN_REQUIRED", findings="owner must decide"))
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(self.spawned, [])

    def test_findings_redact_chat_id_secret_and_host_path(self) -> None:
        finding = f"see {CHAT} and {SECRET} under {self.worktree}/notes"
        outcome = self._apply(claim=_claim("REWORK", findings=finding))
        self.assertEqual(outcome["action"], "redispatched")
        blob = self.packet.body + (outcome["findings"] or "")
        self.assertNotIn(CHAT, blob)
        self.assertNotIn("sk-fake-secret", blob)
        self.assertNotIn(self.worktree, blob)
        loaded, _sha = self.store.load(47)
        assert loaded is not None
        self.assertNotIn(CHAT, loaded.to_json())
        self.assertNotIn(self.worktree, loaded.to_json())

    def test_bugbot_absence_still_redispatches(self) -> None:
        outcome = self._apply(bugbot_advisory="BILLING_DISABLED")
        self.assertEqual(outcome["action"], "redispatched")
        self.assertEqual(len(self.spawned), 1)
        self.assertNotIn("BILLING_DISABLED", self.packet.body)


class BugbotPresentTests(unittest.TestCase):
    def test_bugbot_advisory_is_included_and_absence_is_not_required(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        ledger = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            claims={key: _claim("REWORK", findings="bounded gap")},
        )
        store.save(ledger, expected_sha=None)
        packet = MemoryPacketStore(_packet_body())
        spawned: list[list[str]] = []

        def spawn(argv: list[str], _cwd: str) -> int:
            spawned.append(argv)
            return 0

        loaded, sha = store.load(47)
        assert loaded is not None
        outcome = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=sha,
            claim_store=store,
            packet_store=packet,
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO,
                worktree=tmp.name,
                cursor_chat_id=CHAT,
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=tmp.name,
            git_runner=_git(HEAD),
            spawn=spawn,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            bugbot_advisory="nit: bound the retry",
            state_root=state.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(outcome["action"], "redispatched")
        self.assertEqual(len(spawned), 1)
        self.assertIn("nit: bound the retry", packet.body)
        self.assertNotIn(CHAT, packet.body)


class SliceDBoundaryTests(unittest.TestCase):
    def test_pending_dispatch_restart_resumes_once(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        ledger = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            claims={key: _claim("REWORK")},
        )
        store.save(ledger, expected_sha=None)
        pending = render_rework_work_packet_body(
            _packet_body(),
            repository=REPO,
            branch=BRANCH,
            workstream=WORKSTREAM,
            findings="bounded gap",
            attempt=2,
            head=HEAD,
        )
        packet = MemoryPacketStore(pending)
        spawned: list[list[str]] = []

        def spawn(argv: list[str], _cwd: str) -> int:
            spawned.append(argv)
            return 0

        loaded, sha = store.load(47)
        assert loaded is not None
        self.assertIsNone(loaded.dispositions.get(key))
        outcome = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=sha,
            claim_store=store,
            packet_store=packet,
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=tmp.name,
            git_runner=_git(HEAD),
            spawn=spawn,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            attempt=2,
            state_root=state.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(outcome["action"], "redispatched")
        self.assertEqual(outcome["cursor_calls"], 1)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][:4], ["agent", "--print", "--resume", CHAT])
        self.assertEqual(packet.mutations, 0)
        loaded, _sha = store.load(47)
        assert loaded is not None
        self.assertEqual(loaded.dispositions[key]["action"], "redispatched")
        replay = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=_sha,
            claim_store=store,
            packet_store=packet,
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=tmp.name,
            git_runner=_git(HEAD),
            spawn=spawn,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            attempt=2,
            state_root=state.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(len(spawned), 1)
        self.assertEqual(packet.mutations, 0)

    def test_cursor_appearing_before_spawn_fails_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        store.save(
            IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("REWORK")},
            ),
            expected_sha=None,
        )
        packet = MemoryPacketStore(_packet_body())
        spawned: list[list[str]] = []
        seen = {"n": 0}

        def sessions():
            seen["n"] += 1
            if seen["n"] == 1:
                return []
            return [
                PersistSession(session_id="s", workspace=tmp.name, status="Attached")
            ]

        def spawn(argv: list[str], _cwd: str) -> int:
            spawned.append(argv)
            return 0

        loaded, sha = store.load(47)
        assert loaded is not None
        outcome = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=sha,
            claim_store=store,
            packet_store=packet,
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=tmp.name,
            git_runner=_git(HEAD),
            spawn=spawn,
            list_sessions=sessions,
            list_processes=lambda _path: [],
            attempt=2,
            state_root=state.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(outcome["action"], "dispatch_blocked")
        self.assertNotEqual(outcome["action"], "redispatched")
        self.assertEqual(spawned, [])
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertIn("bounded gap", packet.body)
        self.assertIn("WORK_PACKET_MUTATION=DISPATCH_BLOCKED", packet.body)
        self.assertNotIn(CHAT, packet.body)

    def test_product_path_uses_github_adapter_and_one_resume(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        body = {"text": _packet_body(), "updated": "t0"}
        edits: list[str] = []

        def runner(argv: list[str], _cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
                page = [
                    {
                        "number": 47,
                        "title": "[AI Work] disposition",
                        "body": body["text"],
                        "user": {"login": "packet-author"},
                    }
                ]
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([page]), stderr="")
            if argv[:2] == ["gh", "api"] and argv[2].endswith("/permission"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"permission": "admin"}), stderr=""
                )
            if argv[:3] == ["gh", "issue", "view"]:
                payload = {
                    "number": 47,
                    "title": "[AI Work] disposition",
                    "state": "OPEN",
                    "body": body["text"],
                    "updatedAt": body["updated"],
                    "author": {"login": "packet-author"},
                }
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
            if argv[:3] == ["gh", "issue", "edit"]:
                written = Path(argv[argv.index("--body-file") + 1]).read_text()
                edits.append(written)
                body["text"] = written
                body["updated"] = "t1"
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            raise AssertionError(argv)

        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        store.save(
            IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("REWORK")},
            ),
            expected_sha=None,
        )
        spawned: list[list[str]] = []
        config = HostWorkerConfig(
            state_root=state.name,
            host_id=HOST,
            projects=(
                ProjectDescriptor(
                    repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
                ),
            ),
        )
        outcome = run_completed_audit_disposition(
            host_config=config,
            claim_store=store,
            packet_adapter=adapter,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            git_runner=_git(HEAD),
            spawn=lambda argv, _cwd: spawned.append(argv) or 0,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            host_probe=lambda: HOST,
            attempt=2,
        )
        self.assertEqual(outcome["action"], "redispatched")
        self.assertEqual(outcome["cursor_calls"], 1)
        self.assertEqual(outcome["packet_mutations"], 1)
        self.assertEqual(len(edits), 1)
        self.assertIn("VERDICT=REWORK", edits[0])
        self.assertNotIn(CHAT, edits[0])
        self.assertEqual(spawned[0][:4], ["agent", "--print", "--resume", CHAT])
        parsed = build_parser().parse_args(
            [
                "host-worker",
                "dispose-once",
                "--descriptors",
                "descriptors.json",
                "--issue",
                "47",
                "--branch",
                BRANCH,
                "--workstream",
                WORKSTREAM,
                "--head",
                HEAD,
            ]
        )
        self.assertIs(parsed.func.__name__, "cmd_host_worker_dispose_once")

    def test_omitted_host_probe_observes_the_real_host(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        configured = "dev-drlink" if actual_host_id() != "dev-drlink" else "dev-atlas"
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        store.save(
            IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("REWORK")},
            ),
            expected_sha=None,
        )
        spawned: list[list[str]] = []

        def runner(argv: list[str], _cwd: str) -> subprocess.CompletedProcess[str]:
            raise AssertionError(argv)

        outcome = run_completed_audit_disposition(
            host_config=HostWorkerConfig(
                state_root=state.name,
                host_id=configured,
                projects=(
                    ProjectDescriptor(
                        repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
                    ),
                ),
            ),
            claim_store=store,
            packet_adapter=GitHubWorkPacketAdapter(command_runner=runner),
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            git_runner=_git(HEAD),
            spawn=lambda argv, _cwd: spawned.append(argv) or 0,
        )
        self.assertEqual(outcome["action"], "host_refused")
        self.assertEqual(spawned, [])

    def test_forged_disposition_cannot_suppress_work(self) -> None:
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        ledger = IssueAuditLedger(
            repository=REPO,
            issue_number=47,
            month_id="2023-11",
            claims={key: _claim("REWORK")},
        )
        ledger.dispositions[key] = {
            "action": "redispatched",
            "verdict": "PASS",
            "target_sha": "0" * 40,
            "repository": REPO,
            "issue_number": 47,
            "attempt": 1,
            "findings": "forged",
        }
        store.save(ledger, expected_sha=None)
        with self.assertRaises(ValidationError):
            store.load(47)
        spawned: list[list[str]] = []
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        outcome = apply_exact_head_disposition(
            claim=_claim("REWORK"),
            ledger=ledger,
            ledger_sha=None,
            claim_store=store,
            packet_store=MemoryPacketStore(_packet_body()),
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=tmp.name,
            git_runner=_git(HEAD),
            spawn=lambda argv, _cwd: spawned.append(argv) or 0,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            state_root=state.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(outcome["action"], "disposition_refused")
        self.assertNotEqual(outcome["verdict"], "PASS")
        self.assertEqual(spawned, [])

    def test_unconfirmed_resume_is_not_run_again(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)

        class FailRedispatchSave(MemoryClaimStore):
            def save(self, ledger: IssueAuditLedger, *, expected_sha: str | None) -> str:
                if '"action": "redispatched"' in ledger.to_json():
                    raise CheckpointCasConflict("redispatch persist lost")
                return super().save(ledger, expected_sha=expected_sha)

        store = FailRedispatchSave()
        key = make_audit_claim_key(REPO, 47, HEAD)
        store.save(
            IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("REWORK")},
            ),
            expected_sha=None,
        )
        packet = MemoryPacketStore(_packet_body())
        spawned: list[list[str]] = []

        def spawn(argv: list[str], _cwd: str) -> int:
            spawned.append(argv)
            return 0

        def once(ledger_sha: str | None, ledger: IssueAuditLedger) -> dict:
            return apply_exact_head_disposition(
                claim=ledger.claims[key],
                ledger=ledger,
                ledger_sha=ledger_sha,
                claim_store=store,
                packet_store=packet,
                repository=REPO,
                issue_number=47,
                branch=BRANCH,
                workstream=WORKSTREAM,
                head=HEAD,
                packets=[_snapshot()],
                descriptor=ProjectDescriptor(
                    repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
                ),
                observed_host=HOST,
                expected_host=HOST,
                worktree_path=tmp.name,
                git_runner=_git(HEAD),
                spawn=spawn,
                list_sessions=lambda: [],
                list_processes=lambda _path: [],
                attempt=2,
                state_root=state.name,
                host_probe=lambda: HOST,
            )

        loaded, sha = store.load(47)
        assert loaded is not None
        first = once(sha, loaded)
        self.assertEqual(first["action"], "dispatch_unconfirmed")
        self.assertEqual(len(spawned), 1)
        replay_ledger, replay_sha = store.load(47)
        assert replay_ledger is not None
        self.assertEqual(replay_ledger.dispositions[key]["action"], "dispatch_started")
        second = once(replay_sha, replay_ledger)
        self.assertEqual(second["action"], "dispatch_blocked")
        self.assertEqual(len(spawned), 1)
        self.assertIn("bounded gap", packet.body)

    def test_state_root_outside_the_worktree_leaves_no_repo_artifact(self) -> None:
        work = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.addCleanup(state.cleanup)
        store = MemoryClaimStore()
        key = make_audit_claim_key(REPO, 47, HEAD)
        store.save(
            IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("REWORK")},
            ),
            expected_sha=None,
        )
        loaded, sha = store.load(47)
        assert loaded is not None
        packet = MemoryPacketStore(_packet_body())
        outcome = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=sha,
            claim_store=store,
            packet_store=packet,
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO, worktree=work.name, cursor_chat_id=CHAT
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=work.name,
            git_runner=_git(HEAD),
            spawn=lambda _argv, _cwd: 0,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            state_root=state.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(outcome["action"], "redispatched")
        self.assertFalse((Path(work.name) / "chat-locks").exists())
        self.assertTrue(any(Path(state.name).rglob("*.lock")))
        nested = MemoryPacketStore(_packet_body())
        refused = apply_exact_head_disposition(
            claim=_claim("REWORK"),
            ledger=IssueAuditLedger(
                repository=REPO,
                issue_number=47,
                month_id="2023-11",
                claims={key: _claim("REWORK")},
            ),
            ledger_sha=None,
            claim_store=MemoryClaimStore(),
            packet_store=nested,
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO, worktree=work.name, cursor_chat_id=CHAT
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=work.name,
            git_runner=_git(HEAD),
            spawn=lambda _argv, _cwd: 0,
            list_sessions=lambda: [],
            list_processes=lambda _path: [],
            state_root=work.name,
            host_probe=lambda: HOST,
        )
        self.assertEqual(refused["action"], "state_root_refused")
        self.assertEqual(nested.mutations, 0)
        self.assertFalse((Path(work.name) / "chat-locks").exists())
        descriptor = Path(state.name) / "descriptors.json"
        descriptor.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state_root": work.name,
                    "host_id": HOST,
                    "projects": [
                        {
                            "repository": REPO,
                            "worktree": work.name,
                            "cursor_chat_id": CHAT,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            load_host_worker_config(descriptor)
