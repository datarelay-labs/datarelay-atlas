"""Slice E1 supervise-once: descriptor discovery, one effect per project."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.audit_claim import (
    AuditBudget,
    AuditClaim,
    IssueAuditLedger,
    MemoryClaimStore,
    make_audit_claim_key,
)
from atlas.cli import build_parser
from atlas.final_audit import AuditTelemetry
from atlas.host_worker import (
    HostWorkerConfig,
    ProjectDescriptor,
    chat_domain_lock_path,
    host_worker_run_lock,
)
from atlas.provenance import ValidationError
from atlas.supervisor import supervise_once
from atlas.work_controller import AuditResult, GitHubWorkPacketAdapter, PersistSession


HEAD = "a" * 40
OTHER = "b" * 40
REPO = "datarelay-labs/datarelay-atlas"
REPO_B = "datarelay-labs/engineering-system"
BRANCH = "feature/autonomous-local-supervisor-gpt56-audit"
WORKSTREAM = "autonomous-local-supervisor-gpt56-final-audit"
CHAT = "chatAtlasDurable1"
CHAT_B = "chatSystemDurable1"
HOST = "testhost"


def _packet_body(
    *,
    repository: str = REPO,
    head: str = HEAD,
    status: str = "ACTIVE",
) -> str:
    return f"""PACKET_VERSION=1
TARGET_REPO={repository}
WORKSTREAM={WORKSTREAM}
STATUS={status}
QUEUE_STATE=NONE
BRANCH={BRANCH}
TASK_KIND=DEVELOPMENT
OWNER_INTENT=Replace unreliable scheduled-Chat orchestration.
LAST_VERIFIED_HEAD={head}
GATE=IMPLEMENTATION
NEXT_ACTION=CURSOR_IMPLEMENT_SLICE_E1

## Goal

Supervise once.

## Current State

Discovered.

## Next Action

One effect.

## Constraints

- No merge.

## Canonical References

- Issue packet

## Latest Evidence

pending

## Blockers

NONE
"""


def _bundle(*, ci: str = "OK", reviews: str = "OK") -> dict:
    return {
        "schema": "awc.codex_evidence_bundle.v1",
        "identity": {
            "repository": REPO,
            "branch": BRANCH,
            "head": HEAD,
        },
        "git": {
            "evidence_status": "OK",
            "base": OTHER,
            "head": HEAD,
            "diff": "diff --git a/atlas/supervisor.py b/atlas/supervisor.py\n+pass\n",
            "changed_files": ["atlas/supervisor.py"],
        },
        "work_packet": {"status": "OK", "body": "Acceptance: supervise once."},
        "tests": {"status": "PASS", "detail": "focused tests OK"},
        "ci": {"status": ci, "detail": "affected-tests"},
        "pr_reviews": {"status": reviews, "body": "none"},
    }


def _claim(repository: str, issue: int, verdict: str = "REWORK") -> AuditClaim:
    key = make_audit_claim_key(repository, issue, HEAD)
    return AuditClaim(
        repository=repository,
        issue_number=issue,
        branch=BRANCH,
        target_sha=HEAD,
        claim_key=key,
        state="completed",
        month_id="2023-11",
        claimed_at=1_700_000_000.0,
        lease_seconds=60,
        verdict=verdict,
        findings="bounded gap",
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


class ScriptAuditor:
    def __init__(self, verdict: str = "PASS") -> None:
        self.calls = 0
        self.last_telemetry: AuditTelemetry | None = None

    def audit_bundle(self, bundle: dict, **kwargs: object) -> AuditResult:
        del bundle, kwargs
        self.calls += 1
        self.last_telemetry = AuditTelemetry(
            model="gpt-5.6-sol",
            input_tokens=10,
            output_tokens=5,
            cached_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=0.01,
            target_sha=HEAD,
            verdict="PASS",
            duration_sec=0.1,
        )
        return AuditResult(verdict="PASS", findings="bounded")


class FakeHub:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, int], dict] = {}

    def add(
        self,
        repository: str,
        number: int,
        body: str,
        *,
        permission: str = "admin",
        login: str = "packet-author",
        list_body: str | None = None,
    ) -> None:
        self.issues[(repository, number)] = {
            "body": body,
            "list_body": body if list_body is None else list_body,
            "title": "[AI Work] packet",
            "updated": "t0",
            "login": login,
            "permission": permission,
            "state": "OPEN",
        }

    def runner(self, argv: list[str], _cwd: str) -> subprocess.CompletedProcess[str]:
        if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
            repo = argv[4].split("repos/", 1)[1].split("/issues", 1)[0]
            page = [
                {
                    "number": number,
                    "title": item["title"],
                    "body": item["list_body"],
                    "user": {"login": item["login"]},
                }
                for (item_repo, number), item in sorted(self.issues.items())
                if item_repo == repo
            ]
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps([page]), stderr=""
            )
        if argv[:2] == ["gh", "api"] and str(argv[2]).endswith("/permission"):
            parts = str(argv[2]).split("/")
            repo = f"{parts[1]}/{parts[2]}"
            login = parts[4]
            for (item_repo, _number), item in self.issues.items():
                if item_repo == repo and item["login"] == login:
                    if item["permission"] == "error":
                        return subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="denied"
                        )
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps({"permission": item["permission"]}),
                        stderr="",
                    )
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="missing")
        if argv[:3] == ["gh", "issue", "view"]:
            repo = argv[argv.index("--repo") + 1]
            number = int(argv[3])
            item = self.issues[(repo, number)]
            payload = {
                "number": number,
                "title": item["title"],
                "state": item["state"],
                "body": item["body"],
                "updatedAt": item["updated"],
                "author": {"login": item["login"]},
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )
        if argv[:3] == ["gh", "issue", "edit"]:
            repo = argv[argv.index("--repo") + 1]
            number = int(argv[3])
            written = Path(argv[argv.index("--body-file") + 1]).read_text(
                encoding="utf-8"
            )
            item = self.issues[(repo, number)]
            item["body"] = written
            item["list_body"] = written
            item["updated"] = "t1"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)


class SuperviseOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key-not-real"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "atlas"
        self.worktree_b = self.root / "system"
        self.worktree.mkdir()
        self.worktree_b.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.hub = FakeHub()
        self.auditor = ScriptAuditor()
        self.stores: dict[str, MemoryClaimStore] = {}
        self.spawned: list[tuple[list[str], str]] = []
        self.evidence_calls: list[str] = []
        self.trees: dict[str, dict] = {
            str(self.worktree.resolve()): {
                "repo": REPO,
                "head": HEAD,
                "dirty": False,
            },
            str(self.worktree_b.resolve()): {
                "repo": REPO_B,
                "head": HEAD,
                "dirty": False,
            },
        }
        self.sessions: list[PersistSession] = []
        self.addCleanup(self._unlock_chats)

    def tearDown(self) -> None:
        if self._prior is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._prior

    def _unlock_chats(self) -> None:
        for chat_id in (CHAT, CHAT_B):
            path = chat_domain_lock_path(chat_id)
            if path.exists():
                path.unlink()

    def _config(self, *projects: ProjectDescriptor) -> HostWorkerConfig:
        if not projects:
            projects = (
                ProjectDescriptor(
                    repository=REPO,
                    worktree=str(self.worktree),
                    cursor_chat_id=CHAT,
                ),
            )
        return HostWorkerConfig(
            state_root=str(self.state.resolve()),
            host_id=HOST,
            projects=tuple(projects),
        )

    def _store_for(self, repository: str) -> MemoryClaimStore:
        return self.stores.setdefault(repository, MemoryClaimStore())

    def _seed(self, repository: str, issue: int, verdict: str = "REWORK") -> None:
        claim = _claim(repository, issue, verdict)
        self._store_for(repository).save(
            IssueAuditLedger(
                repository=repository,
                issue_number=issue,
                month_id="2023-11",
                claims={claim.claim_key: claim},
            ),
            expected_sha=None,
        )

    def _git(self, argv: list[str], cwd: str) -> str:
        spec = self.trees[str(Path(cwd).resolve())]
        if argv[1:] == ["rev-parse", "--show-toplevel"]:
            return str(Path(cwd).resolve())
        if argv[1:] == ["remote", "get-url", "origin"]:
            return f"https://github.com/{spec['repo']}.git"
        if argv[1:] == ["branch", "--show-current"]:
            return BRANCH
        if argv[1:] == ["rev-parse", "HEAD"]:
            return str(spec["head"])
        if argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
            return " M dirty" if spec["dirty"] else ""
        raise AssertionError(argv)

    def _spawn(self, argv: list[str], cwd: str) -> int:
        self.spawned.append((list(argv), cwd))
        return 0

    def _evidence(self, packet: dict, worktree: str) -> dict:
        del worktree
        self.evidence_calls.append(str(packet["repository"]))
        return _bundle()

    def _run(self, **overrides: object) -> dict:
        fields: dict[str, object] = {
            "config": self._config(),
            "packet_adapter": GitHubWorkPacketAdapter(command_runner=self.hub.runner),
            "claim_store_for": self._store_for,
            "auditor": self.auditor,
            "evidence_for": self._evidence,
            "budget": AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=25.0,
                preflight_usd=0.0,
            ),
            "git_runner": self._git,
            "spawn": self._spawn,
            "host_probe": lambda: HOST,
            "list_sessions": lambda: list(self.sessions),
            "list_processes": lambda _path: [],
            "now": lambda: 1_700_000_000.0,
        }
        fields.update(overrides)
        return supervise_once(**fields)  # type: ignore[arg-type]

    def _row(self, outcome: dict, repository: str) -> dict:
        matches = [
            item for item in outcome["projects"] if item["repository"] == repository
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_idle_projects_make_no_model_or_cursor_call(self) -> None:
        outcome = self._run(
            config=self._config(
                ProjectDescriptor(REPO, str(self.worktree), CHAT),
                ProjectDescriptor(REPO_B, str(self.worktree_b), CHAT_B),
            )
        )
        self.assertEqual(outcome["model_calls"], 0)
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self._row(outcome, REPO)["action"], "idle")
        self.assertEqual(self._row(outcome, REPO_B)["action"], "idle")
        self.assertEqual(self.evidence_calls, [])
        self.assertEqual(self.spawned, [])
        self.assertNotIn(CHAT, json.dumps(outcome))

    def test_unique_active_packet_is_discovered_without_a_hardcoded_issue(self) -> None:
        self.hub.add(REPO, 88, _packet_body())
        self.hub.add(
            REPO,
            89,
            _packet_body(),
            permission="read",
            login="reader-login",
        )
        outcome = self._run()
        row = self._row(outcome, REPO)
        self.assertEqual(row["issue_number"], 88)
        self.assertEqual(row["action"], "audited")
        self.assertEqual(self.auditor.calls, 1)
        self.assertEqual(outcome["cursor_calls"], 0)

    def test_zero_and_multiple_active_packets_fail_closed(self) -> None:
        idle = self._run()
        self.assertEqual(self._row(idle, REPO)["action"], "idle")
        self.hub.add(REPO, 88, _packet_body())
        self.hub.add(REPO, 90, _packet_body(), login="second-author")
        outcome = self._run()
        self.assertEqual(self._row(outcome, REPO)["action"], "ambiguous_packet")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.evidence_calls, [])

    def test_live_cursor_makes_no_model_call(self) -> None:
        self.hub.add(REPO, 88, _packet_body())
        self._seed(REPO, 88)
        self.sessions.append(
            PersistSession(session_id="live-session", workspace=str(self.worktree))
        )
        outcome = self._run()
        self.assertEqual(self._row(outcome, REPO)["action"], "cursor_active_noop")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(outcome["model_calls"], 0)
        self.assertEqual(self.evidence_calls, [])
        self.assertEqual(self.spawned, [])

    def test_completed_claim_disposes_once_and_replay_does_not_respawn(self) -> None:
        self.hub.add(REPO, 88, _packet_body())
        self._seed(REPO, 88)
        first = self._run()
        self.assertEqual(self._row(first, REPO)["action"], "redispatched")
        self.assertEqual(first["cursor_calls"], 1)
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][0][:4], ["agent", "--print", "--resume", CHAT])
        second = self._run()
        self.assertEqual(self._row(second, REPO)["action"], "duplicate")
        self.assertEqual(second["cursor_calls"], 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.evidence_calls, [])

    def test_audit_ready_head_claims_once_and_replay_does_not_reaudit(self) -> None:
        self.hub.add(REPO, 91, _packet_body())
        first = self._run()
        self.assertEqual(self._row(first, REPO)["action"], "audited")
        self.assertEqual(self._row(first, REPO)["issue_number"], 91)
        self.assertEqual(self.auditor.calls, 1)
        second = self._run()
        self.assertEqual(self.auditor.calls, 1)
        self.assertEqual(second["cursor_calls"], 0)
        self.assertNotEqual(self._row(second, REPO)["action"], "audited")

    def test_missing_api_key_is_human_required_without_a_paid_call(self) -> None:
        self.hub.add(REPO, 88, _packet_body())
        os.environ.pop("OPENAI_API_KEY", None)
        outcome = self._run()
        row = self._row(outcome, REPO)
        self.assertEqual(row["action"], "missing_key")
        self.assertEqual(row["verdict"], "HUMAN_REQUIRED")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(outcome["cursor_calls"], 0)

    def test_stale_ci_review_and_head_make_no_paid_call(self) -> None:
        self.hub.add(REPO, 88, _packet_body())
        outcome = self._run(evidence_for=lambda _packet, _tree: _bundle(ci="PENDING"))
        self.assertEqual(self._row(outcome, REPO)["action"], "evidence_blocked")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(self.spawned, [])
        reviewed = self._run(
            evidence_for=lambda _packet, _tree: _bundle(reviews="INCOMPLETE")
        )
        self.assertEqual(self._row(reviewed, REPO)["action"], "evidence_blocked")
        self.assertEqual(self.auditor.calls, 0)
        self.trees[str(self.worktree.resolve())]["head"] = OTHER
        stale = self._run()
        self.assertEqual(self._row(stale, REPO)["action"], "identity_refused")
        self.trees[str(self.worktree.resolve())]["head"] = HEAD
        self.trees[str(self.worktree.resolve())]["dirty"] = True
        dirty = self._run()
        self.assertEqual(self._row(dirty, REPO)["action"], "identity_refused")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(self.spawned, [])

    def test_project_failure_does_not_dispatch_the_other_project(self) -> None:
        self.hub.add(
            REPO,
            70,
            _packet_body(),
            permission="error",
        )
        self.hub.add(REPO_B, 61, _packet_body(repository=REPO_B))
        self._seed(REPO_B, 61)
        outcome = self._run(
            config=self._config(
                ProjectDescriptor(REPO, str(self.worktree), CHAT),
                ProjectDescriptor(REPO_B, str(self.worktree_b), CHAT_B),
            )
        )
        self.assertEqual(self._row(outcome, REPO)["action"], "refused")
        self.assertEqual(self._row(outcome, REPO_B)["action"], "redispatched")
        self.assertEqual(len(self.spawned), 1)
        argv, cwd = self.spawned[0]
        self.assertEqual(Path(cwd).resolve(), self.worktree_b.resolve())
        self.assertIn(CHAT_B, argv)
        self.assertNotIn(CHAT, argv)
        self.assertEqual(self.auditor.calls, 0)

    def test_listed_packet_drift_makes_no_effect(self) -> None:
        self.hub.add(
            REPO,
            88,
            _packet_body(head=OTHER),
            list_body=_packet_body(head=HEAD),
        )
        outcome = self._run()
        self.assertEqual(self._row(outcome, REPO)["action"], "canonical_drift")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.evidence_calls, [])

    def test_packet_mutation_during_evidence_makes_no_paid_call(self) -> None:
        self.hub.add(REPO, 88, _packet_body(head=HEAD))

        def evidence(packet: dict, worktree: str) -> dict:
            del worktree
            self.evidence_calls.append(str(packet["repository"]))
            mutated = _packet_body(head=OTHER)
            item = self.hub.issues[(REPO, 88)]
            item["body"] = mutated
            item["list_body"] = mutated
            return _bundle()

        outcome = self._run(evidence_for=evidence)
        self.assertEqual(self._row(outcome, REPO)["action"], "canonical_drift")
        self.assertEqual(self.auditor.calls, 0)
        self.assertEqual(outcome["auditor_calls"], 0)
        self.assertEqual(outcome["cursor_calls"], 0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.evidence_calls, [REPO])

    def test_duplicate_invocation_is_locked(self) -> None:
        with host_worker_run_lock(self.state.resolve() / "supervise-once.lock"):
            with self.assertRaises(ValidationError) as caught:
                self._run()
        self.assertIn("already running", str(caught.exception))
        self.assertEqual(self.auditor.calls, 0)

    def test_cli_supervise_once_has_no_issue_argument(self) -> None:
        parsed = build_parser().parse_args(
            ["host-worker", "supervise-once", "--descriptors", "local.json"]
        )
        self.assertEqual(parsed.hw_command, "supervise-once")
        self.assertFalse(hasattr(parsed, "issue"))
        script = Path("scripts/host-worker-supervise-once.sh").read_text(encoding="utf-8")
        self.assertIn("flock -n", script)
        self.assertIn("supervise-once", script)
        self.assertNotIn("crontab", script)

    def test_wrapper_python_selection(self) -> None:
        s = str(Path("scripts/host-worker-supervise-once.sh").resolve())
        with tempfile.TemporaryDirectory() as tmp:
            r, h = Path(tmp) / "r", Path(tmp) / "h"
            e = {k: v for k, v in os.environ.items() if k != "ATLAS_PYTHON"}
            e.update(PATH="/usr/bin:/bin", HOME=str(h))
            go = lambda c: subprocess.run(["bash","-c",c,"x",s,str(r)],capture_output=True,text=True,env=e)
            mark = lambda p: (p.parent.mkdir(parents=True,exist_ok=True),p.write_text("#!/bin/sh\n"),p.chmod(0o755))
            py = 'source "$1"; atlas_py_bin "$2"'
            mark(r / "py")
            e["ATLAS_PYTHON"] = str(r / "py")
            self.assertEqual(go(py).stdout.strip(), e["ATLAS_PYTHON"])
            mark(h / ".local/bin/gh")
            mark(h / ".local/bin/agent")
            got = go('source "$1"; cron_bin_path').stdout.strip()
            self.assertEqual(subprocess.run(["bash","-c","export PATH=$1; command -v gh && command -v agent","x",got],stdout=subprocess.DEVNULL).returncode,0)
            e["HOME"] = str(r / "e")
            self.assertIn("not found", subprocess.run([s, "d"], capture_output=True, text=True, env=e).stderr)
