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


def _packet_body(
    head: str = HEAD, *, implementer: str = "CHATGPT_CHAT"
) -> str:
    return f"""PACKET_VERSION=2
TARGET_REPO={REPO}
WORKSTREAM={WORKSTREAM}
STATUS=ACTIVE
QUEUE_STATE=NONE
BRANCH={BRANCH}
TASK_KIND=DEVELOPMENT
OWNER_INTENT=Replace unreliable scheduled-Chat orchestration.
LAST_VERIFIED_HEAD={head}
IMPLEMENTER={implementer}
CHANGE_RISK=HIGH
INTENT_REVISION=1
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

    def test_current_cursor_packet_is_refused_before_probe_or_spawn(self) -> None:
        ledger, sha = self.store.load(47)
        assert ledger is not None

        def forbidden(*_args, **_kwargs):
            raise AssertionError("retired Cursor disposition must not probe or spawn")

        outcome = apply_exact_head_disposition(
            claim=ledger.claims[make_audit_claim_key(REPO, 47, HEAD)],
            ledger=ledger,
            ledger_sha=sha,
            claim_store=self.store,
            packet_store=MemoryPacketStore(_packet_body(implementer="CURSOR")),
            repository=REPO,
            issue_number=47,
            branch=BRANCH,
            workstream=WORKSTREAM,
            head=HEAD,
            packets=[_snapshot()],
            descriptor=ProjectDescriptor(
                repository=REPO,
                worktree=self.worktree,
                cursor_chat_id=CHAT,
            ),
            observed_host=HOST,
            expected_host=HOST,
            worktree_path=self.worktree,
            git_runner=_git(HEAD),
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
            attempt=2,
            gates=_gates(),
            state_root=self.state.name,
            host_probe=forbidden,
        )
        self.assertEqual(outcome["action"], "implementer_refused")
        self.assertEqual(outcome["cursor_calls"], 0)

    def test_chat_rework_authorizes_handoff_without_cursor_spawn(self) -> None:
        packet = MemoryPacketStore(
            _packet_body(implementer="CHATGPT_CHAT")
        )

        def cursor_probe_forbidden(*_args, **_kwargs):
            raise AssertionError("Chat REWORK must not probe Cursor state")

        outcome = self._apply(
            packet_store=packet,
            list_sessions=cursor_probe_forbidden,
            list_processes=cursor_probe_forbidden,
        )

        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(outcome["verdict"], "REWORK")
        self.assertEqual(packet.mutations, 1)
        self.assertEqual(self.spawned, [])
        self.assertIn(
            "WORK_PACKET_MUTATION=AUTHORIZED_HANDOFF", packet.body
        )
        self.assertIn("VERDICT=REWORK", packet.body)
        self.assertNotIn("/work-resume", packet.body)
        self.assertNotIn(CHAT, packet.body)

        ledger, _sha = self.store.load(47)
        assert ledger is not None
        key = make_audit_claim_key(REPO, 47, HEAD)
        self.assertEqual(
            ledger.dispositions[key]["action"], "rework_handoff"
        )

        replay = self._apply(packet_store=packet)
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(replay["cursor_calls"], 0)
        self.assertEqual(packet.mutations, 1)
        self.assertEqual(self.spawned, [])

    def test_chat_rework_preserves_pending_dispatch_text_inside_finding(self) -> None:
        packet = MemoryPacketStore(
            _packet_body(implementer="CHATGPT_CHAT")
        )
        finding = (
            "Finding discusses literal "
            "WORK_PACKET_MUTATION=PENDING_DISPATCH and must remain unchanged."
        )
        outcome = self._apply(
            claim=_claim("REWORK", findings=finding),
            packet_store=packet,
        )

        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertIn(finding, packet.body)
        self.assertIn(
            "WORK_PACKET_MUTATION=AUTHORIZED_HANDOFF", packet.body
        )

    def test_rework_mutates_once_and_authorizes_chat_handoff(self) -> None:
        outcome = self._apply()
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["verdict"], "REWORK")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.packet.mutations, 1)
        self.assertIn("WORK_PACKET_MUTATION=AUTHORIZED_HANDOFF", self.packet.body)
        self.assertNotIn("/work-resume", self.packet.body)

        replay = self._apply()
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(replay["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.packet.mutations, 1)

    def test_missing_or_unknown_implementer_fails_closed(self) -> None:
        for value in (None, "CURSOR", "OTHER"):
            body = _packet_body()
            if value is None:
                body = body.replace("IMPLEMENTER=CHATGPT_CHAT\n", "")
            else:
                body = body.replace(
                    "IMPLEMENTER=CHATGPT_CHAT", f"IMPLEMENTER={value}"
                )
            packet = MemoryPacketStore(body)
            result = self._apply(packet_store=packet)
            self.assertEqual(result["action"], "implementer_refused")
            self.assertEqual(result["cursor_calls"], 0)
            self.assertEqual(packet.mutations, 0)
        self.assertEqual(self.spawned, [])

    def test_stale_dirty_and_cursor_state_do_not_dispatch(self) -> None:
        stale = self._apply(head=OTHER, packets=[_snapshot(head=OTHER)])
        self.assertIn(stale["action"], {"stale_head", "stale_checkpoint"})
        self.assertEqual(stale["cursor_calls"], 0)

        dirty = self._apply(git_runner=_git(HEAD, dirty=True))
        self.assertEqual(dirty["action"], "identity_refused")
        self.assertEqual(dirty["cursor_calls"], 0)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Chat disposition must not probe Cursor state")

        current = self._apply(
            list_sessions=forbidden,
            list_processes=forbidden,
        )
        self.assertEqual(current["action"], "authorized_handoff")
        self.assertEqual(current["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])

    def test_legacy_dispatch_failure_is_unreachable_from_chat_handoff(self) -> None:
        def forbidden_spawn(*_args, **_kwargs):
            raise AssertionError("Chat disposition must not spawn Cursor")

        outcome = self._apply(spawn=forbidden_spawn)
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])
        self.assertIn("WORK_PACKET_MUTATION=AUTHORIZED_HANDOFF", self.packet.body)

        replay = self._apply(spawn=forbidden_spawn)
        self.assertEqual(replay["action"], "duplicate")
        self.assertEqual(replay["cursor_calls"], 0)

    def test_chat_handoff_never_enters_dispatch_compensation(self) -> None:
        outcome = self._apply()
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])
        self.assertIn("bounded gap", outcome["findings"])
        self.assertNotIn("DISPATCH_BLOCKED", self.packet.body)

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

    def test_findings_redact_secret_and_host_path_without_cursor_identity(self) -> None:
        finding = f"fix {SECRET} at {self.worktree}/private/file"
        outcome = self._apply(claim=_claim("REWORK", findings=finding))
        self.assertEqual(outcome["action"], "authorized_handoff")
        encoded = str(outcome)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(self.worktree, encoded)
        self.assertNotIn(CHAT, self.packet.body)
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])

    def test_bugbot_absence_still_authorizes_handoff(self) -> None:
        outcome = self._apply(bugbot_advisory=None)
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])

class BugbotPresentTests(unittest.TestCase):
    def test_bugbot_advisory_is_included_and_absence_is_not_required(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
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
        loaded, sha = store.load(47)
        assert loaded is not None

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Chat disposition must not spawn or probe Cursor")

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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
            bugbot_advisory="nit: bound the retry",
        )
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertIn("nit: bound the retry", packet.body)
        self.assertNotIn(CHAT, packet.body)

class SliceDBoundaryTests(unittest.TestCase):
    def test_legacy_pending_dispatch_is_rewritten_as_chat_handoff(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
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
        loaded, sha = store.load(47)
        assert loaded is not None

        def forbidden(*_args, **_kwargs):
            raise AssertionError("legacy pending marker must not resume Cursor")

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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
            attempt=2,
        )
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertIn("WORK_PACKET_MUTATION=AUTHORIZED_HANDOFF", packet.body)

    def test_cursor_state_is_not_probed_before_chat_handoff(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
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

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Chat disposition must not probe Cursor state")

        outcome = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=sha,
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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
        )
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)

    def test_product_path_uses_github_adapter_and_authorizes_handoff(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        body = {"text": _packet_body(), "updated": "t0"}
        edits: list[str] = []

        def runner(argv: list[str], _cwd: str) -> subprocess.CompletedProcess[str]:
            if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
                page = [{
                    "number": 47,
                    "title": "[AI Work] disposition",
                    "body": body["text"],
                    "user": {"login": "packet-author"},
                }]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps([page]), stderr=""
                )
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
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
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
        config = HostWorkerConfig(
            state_root=state.name,
            host_id=HOST,
            projects=(
                ProjectDescriptor(
                    repository=REPO, worktree=tmp.name, cursor_chat_id=CHAT
                ),
            ),
        )

        def forbidden(*_args, **_kwargs):
            raise AssertionError("product Chat path must not spawn/probe Cursor")

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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
            host_probe=lambda: HOST,
            attempt=2,
        )
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(outcome["packet_mutations"], 1)
        self.assertEqual(len(edits), 1)
        self.assertIn("WORK_PACKET_MUTATION=AUTHORIZED_HANDOFF", edits[0])
        self.assertNotIn(CHAT, edits[0])

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

    def test_replay_of_chat_handoff_does_not_spawn(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
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

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Chat handoff must not spawn/probe Cursor")

        loaded, sha = store.load(47)
        assert loaded is not None
        first = apply_exact_head_disposition(
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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
            attempt=2,
        )
        self.assertEqual(first["action"], "authorized_handoff")
        loaded2, sha2 = store.load(47)
        assert loaded2 is not None
        second = apply_exact_head_disposition(
            claim=loaded2.claims[key],
            ledger=loaded2,
            ledger_sha=sha2,
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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
            attempt=2,
        )
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(second["cursor_calls"], 0)

    def test_chat_handoff_creates_no_cursor_lock_artifact(self) -> None:
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
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

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Chat handoff must not touch Cursor runtime")

        outcome = apply_exact_head_disposition(
            claim=loaded.claims[key],
            ledger=loaded,
            ledger_sha=sha,
            claim_store=store,
            packet_store=MemoryPacketStore(_packet_body()),
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
            spawn=forbidden,
            list_sessions=forbidden,
            list_processes=forbidden,
        )
        self.assertEqual(outcome["action"], "authorized_handoff")
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertFalse((Path(work.name) / "chat-locks").exists())
