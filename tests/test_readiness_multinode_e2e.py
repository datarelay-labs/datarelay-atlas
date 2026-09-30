from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.readiness_dispatch import activate_and_dispatch_single_worker
from atlas.readiness_graph import plan_readiness
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    PersistSession,
    PtyPersistCursorDispatcher,
)

REPO = "datarelay-labs/datarelay-atlas"
WORKSTREAM = "readiness-multinode-e2e"

BASE = 201
READY_A = 202
CONFLICT_B = 203
INDEPENDENT_C = 204
OWNER_GATED_D = 205
HUMAN_GATED_E = 206

BRANCH_SHARED = "feature/e2e-shared"
BRANCH_INDEPENDENT = "feature/e2e-independent"
BRANCH_OWNER = "feature/e2e-owner"
BRANCH_HUMAN = "feature/e2e-human"

HEAD_BASE = "1" * 40
HEAD_A = "2" * 40
HEAD_B = "3" * 40
HEAD_C = "4" * 40
HEAD_D = "5" * 40
HEAD_E = "6" * 40


def _packet_body(
    *,
    status: str,
    queue_state: str,
    branch: str,
    head: str,
    after_issue: int | None = None,
) -> str:
    after = f"AFTER_ISSUE={after_issue}\n" if after_issue is not None else ""
    return (
        "PACKET_VERSION=2\n"
        f"TARGET_REPO={REPO}\n"
        f"WORKSTREAM={WORKSTREAM}\n"
        f"STATUS={status}\n"
        f"QUEUE_STATE={queue_state}\n"
        f"BRANCH={branch}\n"
        "TASK_KIND=DEVELOPMENT\n"
        "OWNER_INTENT=Readiness multi-node deterministic E2E.\n"
        f"LAST_VERIFIED_HEAD={head}\n"
        "IMPLEMENTER=CURSOR\n"
        "CHANGE_RISK=HIGH\n"
        "INTENT_REVISION=1\n"
        f"{after}"
        "\n"
        "## Goal\n\n"
        "Exercise one bounded readiness lifecycle.\n"
    )


def _node(
    *,
    node_id: str,
    issue_number: int,
    branch: str,
    head: str,
    status: str,
    queue_state: str,
    priority: int,
    dependencies: list[dict[str, str]] | None = None,
    owner_gate: bool = False,
    human_required: bool = False,
) -> dict:
    return {
        "node_id": node_id,
        "issue_number": issue_number,
        "repository": REPO,
        "branch": branch,
        "head": head,
        "packet_status": status,
        "queue_state": queue_state,
        "dependencies": dependencies or [],
        "resources": [],
        "authority_state": "TRUSTED",
        "owner_gate": owner_gate,
        "human_required": human_required,
        "priority": priority,
    }


def _graph(*, max_wip: int) -> dict:
    dependency = [{"node_id": "base", "relation": "REQUIRES_COMPLETE"}]
    return {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": [
            _node(
                node_id="base",
                issue_number=BASE,
                branch=BRANCH_SHARED,
                head=HEAD_BASE,
                status="COMPLETE",
                queue_state="NONE",
                priority=0,
            ),
            _node(
                node_id="a-ready",
                issue_number=READY_A,
                branch=BRANCH_SHARED,
                head=HEAD_A,
                status="PAUSED",
                queue_state="QUEUED",
                priority=1,
                dependencies=dependency,
            ),
            _node(
                node_id="b-conflict",
                issue_number=CONFLICT_B,
                branch=BRANCH_SHARED,
                head=HEAD_B,
                status="PAUSED",
                queue_state="QUEUED",
                priority=2,
                dependencies=dependency,
            ),
            _node(
                node_id="c-independent",
                issue_number=INDEPENDENT_C,
                branch=BRANCH_INDEPENDENT,
                head=HEAD_C,
                status="PAUSED",
                queue_state="QUEUED",
                priority=3,
                dependencies=dependency,
            ),
            _node(
                node_id="d-owner-gated",
                issue_number=OWNER_GATED_D,
                branch=BRANCH_OWNER,
                head=HEAD_D,
                status="PAUSED",
                queue_state="QUEUED",
                priority=0,
                dependencies=dependency,
                owner_gate=True,
            ),
            _node(
                node_id="e-human-gated",
                issue_number=HUMAN_GATED_E,
                branch=BRANCH_HUMAN,
                head=HEAD_E,
                status="PAUSED",
                queue_state="QUEUED",
                priority=0,
                dependencies=dependency,
                human_required=True,
            ),
        ],
    }


class MultiNodeGitHubRunner:
    def __init__(self) -> None:
        self.edit_count = 0
        self.packets = {
            BASE: {
                "state": "CLOSED",
                "body": _packet_body(
                    status="COMPLETE",
                    queue_state="NONE",
                    branch=BRANCH_SHARED,
                    head=HEAD_BASE,
                ),
            },
            READY_A: {
                "state": "OPEN",
                "body": _packet_body(
                    status="PAUSED",
                    queue_state="QUEUED",
                    branch=BRANCH_SHARED,
                    head=HEAD_A,
                    after_issue=BASE,
                ),
            },
            CONFLICT_B: {
                "state": "OPEN",
                "body": _packet_body(
                    status="PAUSED",
                    queue_state="QUEUED",
                    branch=BRANCH_SHARED,
                    head=HEAD_B,
                    after_issue=BASE,
                ),
            },
            INDEPENDENT_C: {
                "state": "OPEN",
                "body": _packet_body(
                    status="PAUSED",
                    queue_state="QUEUED",
                    branch=BRANCH_INDEPENDENT,
                    head=HEAD_C,
                    after_issue=BASE,
                ),
            },
            OWNER_GATED_D: {
                "state": "OPEN",
                "body": _packet_body(
                    status="PAUSED",
                    queue_state="QUEUED",
                    branch=BRANCH_OWNER,
                    head=HEAD_D,
                    after_issue=BASE,
                ),
            },
            HUMAN_GATED_E: {
                "state": "OPEN",
                "body": _packet_body(
                    status="PAUSED",
                    queue_state="QUEUED",
                    branch=BRANCH_HUMAN,
                    head=HEAD_E,
                    after_issue=BASE,
                ),
            },
        }

    def _issue_payload(self, issue_number: int, *, rest: bool) -> dict:
        packet = self.packets[issue_number]
        login = f"e2e-author-{issue_number}"
        payload = {
            "number": issue_number,
            "title": f"[AI Work] e2e packet {issue_number}",
            "state": packet["state"],
            "body": packet["body"],
            "updatedAt": f"2026-09-28T00:00:{self.edit_count:02d}Z",
        }
        payload["user" if rest else "author"] = {"login": login}
        return payload

    def __call__(
        self, argv: list[str], _cwd: str
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "issue", "view"]:
            issue_number = int(argv[3])
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(self._issue_payload(issue_number, rest=False)),
                stderr="",
            )
        if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
            issues = [
                self._issue_payload(number, rest=True)
                for number in sorted(self.packets)
                if self.packets[number]["state"] == "OPEN"
            ]
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps([issues]),
                stderr="",
            )
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"permission": "write"}),
                stderr="",
            )
        if argv[:3] == ["gh", "issue", "edit"]:
            issue_number = int(argv[3])
            body_path = Path(argv[argv.index("--body-file") + 1])
            self.packets[issue_number]["body"] = body_path.read_text(
                encoding="utf-8"
            )
            self.edit_count += 1
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected GitHub command: {argv}")


class PersistentHarness:
    def __init__(self, *, worktree: str) -> None:
        self.worktree = str(Path(worktree).resolve())
        self.spawned = False
        self.spawn_count = 0
        self.command: list[str] | None = None

    def list_sessions(self) -> list[PersistSession]:
        if not self.spawned:
            return []
        return [
            PersistSession(
                session_id="e2e-persist-session",
                workspace=self.worktree,
                status="running",
                task="/work-resume",
            )
        ]

    def spawn(self, command: list[str], cwd: str) -> int:
        if str(Path(cwd).resolve()) != self.worktree:
            raise AssertionError("dispatcher used unexpected worktree")
        self.command = list(command)
        self.spawn_count += 1
        self.spawned = True
        return 4242

    @staticmethod
    def owned_session_ids(_pid: int, candidates: set[str]) -> set[str]:
        return set(candidates)

    def git_runner(self, argv: list[str], cwd: str) -> str:
        if str(Path(cwd).resolve()) != self.worktree:
            raise AssertionError("git validation used unexpected worktree")
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return self.worktree
        if argv == ["git", "remote", "get-url", "origin"]:
            return "https://github.com/datarelay-labs/datarelay-atlas.git"
        if argv == ["git", "branch", "--show-current"]:
            return BRANCH_SHARED
        if argv == ["git", "rev-parse", "HEAD"]:
            return HEAD_A
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return ""
        raise AssertionError(f"unexpected git command: {argv}")


class ReadinessMultiNodeE2ETests(unittest.TestCase):
    def test_multi_node_planning_single_effect_and_replay(self) -> None:
        read_only_plan = plan_readiness(_graph(max_wip=2))
        self.assertEqual(read_only_plan["graph_state"], "READY")
        self.assertEqual(
            read_only_plan["selected_node_ids"],
            ["a-ready", "c-independent"],
        )
        by_id = {node["node_id"]: node for node in read_only_plan["nodes"]}
        self.assertEqual(by_id["b-conflict"]["readiness"], "BLOCKED")
        self.assertEqual(
            by_id["b-conflict"]["reasons"],
            ["RESOURCE_CONFLICT"],
        )
        self.assertEqual(
            by_id["b-conflict"]["blocked_by"],
            ["a-ready"],
        )
        self.assertEqual(
            by_id["d-owner-gated"]["readiness"],
            "HUMAN_REQUIRED",
        )
        self.assertEqual(
            by_id["d-owner-gated"]["reasons"],
            ["OWNER_GATE"],
        )
        self.assertEqual(
            by_id["e-human-gated"]["readiness"],
            "HUMAN_REQUIRED",
        )
        self.assertEqual(
            by_id["e-human-gated"]["reasons"],
            ["HUMAN_REQUIRED_GATE"],
        )

        runner = MultiNodeGitHubRunner()
        adapter = GitHubWorkPacketAdapter(command_runner=runner)

        with tempfile.TemporaryDirectory() as worktree:
            harness = PersistentHarness(worktree=worktree)
            dispatcher = PtyPersistCursorDispatcher(
                list_sessions=harness.list_sessions,
                spawn=harness.spawn,
                list_target_procs=lambda _path: [],
                git_runner=harness.git_runner,
                resource_preflight=lambda: (
                    0,
                    "RESULT=PASS\nEXIT_CODE=0\nREASON=deterministic e2e\n",
                ),
                owned_session_ids=harness.owned_session_ids,
                poll_interval_sec=0.001,
                poll_timeout_sec=1.0,
                sleeper=lambda _seconds: None,
            )
            graph_path = Path(worktree) / "effect-graph.json"
            graph_path.write_text(
                json.dumps(_graph(max_wip=1), sort_keys=True),
                encoding="utf-8",
            )

            first = activate_and_dispatch_single_worker(
                graph_path=graph_path,
                workstream=WORKSTREAM,
                worktree_path=worktree,
                packet_adapter=adapter,
                dispatcher=dispatcher,
            )
            self.assertEqual(first["action"], "dispatched")
            self.assertEqual(first["selected_node"]["node_id"], "a-ready")
            self.assertEqual(first["session_id"], "e2e-persist-session")
            self.assertEqual(harness.spawn_count, 1)
            self.assertEqual(
                harness.command,
                ["agent", "persist", "--force", "--trust", "/work-resume"],
            )
            self.assertEqual(runner.edit_count, 1)
            self.assertIn(
                "STATUS=ACTIVE",
                runner.packets[READY_A]["body"],
            )
            self.assertIn(
                "QUEUE_STATE=NONE",
                runner.packets[READY_A]["body"],
            )
            for issue_number in (
                CONFLICT_B,
                INDEPENDENT_C,
                OWNER_GATED_D,
                HUMAN_GATED_E,
            ):
                self.assertIn(
                    "STATUS=PAUSED",
                    runner.packets[issue_number]["body"],
                )
                self.assertIn(
                    "QUEUE_STATE=QUEUED",
                    runner.packets[issue_number]["body"],
                )

            replay = activate_and_dispatch_single_worker(
                graph_path=graph_path,
                workstream=WORKSTREAM,
                worktree_path=worktree,
                packet_adapter=adapter,
                dispatcher=dispatcher,
            )
            self.assertEqual(replay["action"], "denied")
            self.assertEqual(
                replay["authorization"]["decision"],
                "DENY",
            )
            self.assertEqual(harness.spawn_count, 1)
            self.assertEqual(runner.edit_count, 1)


if __name__ == "__main__":
    unittest.main()
