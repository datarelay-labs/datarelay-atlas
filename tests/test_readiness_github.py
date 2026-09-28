from __future__ import annotations

import json
import subprocess
import unittest
from copy import deepcopy

from atlas.cli import build_parser
from atlas.provenance import ValidationError
from atlas.readiness_github import (
    plan_github_reconciled_readiness,
    reconcile_readiness_graph,
)
from atlas.readiness_graph import plan_readiness
from atlas.work_controller import GitHubWorkPacketAdapter

REPO = "datarelay-labs/datarelay-atlas"
OTHER_REPO = "datarelay-labs/engineering-system"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
def packet_body(
    *,
    repository: str = REPO,
    status: str = "PAUSED",
    queue_state: str | None = "QUEUED",
    branch: str = "feature/readiness",
    head: str = HEAD_A,
) -> str:
    queue = "" if queue_state is None else f"QUEUE_STATE={queue_state}\n"
    return (
        "PACKET_VERSION=2\n"
        f"TARGET_REPO={repository}\n"
        "WORKSTREAM=readiness-test\n"
        f"STATUS={status}\n"
        f"{queue}"
        f"BRANCH={branch}\n"
        "TASK_KIND=DEVELOPMENT\n"
        "OWNER_INTENT=Verify canonical readiness facts.\n"
        f"LAST_VERIFIED_HEAD={head}\n"
    )


class FakeGitHubRunner:
    def __init__(
        self,
        *,
        body: str,
        state: str = "OPEN",
        title: str = "[AI Work] readiness",
        permission: str = "write",
    ) -> None:
        self.body = body
        self.state = state
        self.title = title
        self.permission = permission
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], _cwd: str
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            payload = {
                "number": int(argv[3]),
                "title": self.title,
                "state": self.state,
                "body": self.body,
                "updatedAt": "2026-09-28T00:00:00Z",
                "author": {"login": "trusted-author"},
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )
        if argv[:2] == ["gh", "api"] and str(argv[2]).endswith(
            "/collaborators/trusted-author/permission"
        ):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"permission": self.permission}),
                stderr="",
            )
        raise AssertionError(argv)


def graph_node(
    node_id: str,
    issue_number: int,
    *,
    repository: str = REPO,
    branch: str = "feature/readiness",
    head: str = HEAD_A,
    packet_status: str = "PAUSED",
    queue_state: str = "QUEUED",
    dependencies: list[str] | None = None,
    authority_state: str = "TRUSTED",
    priority: int = 10,
) -> dict:
    return {
        "node_id": node_id,
        "issue_number": issue_number,
        "repository": repository,
        "branch": branch,
        "head": head,
        "packet_status": packet_status,
        "queue_state": queue_state,
        "dependencies": [
            {"node_id": dep, "relation": "REQUIRES_COMPLETE"}
            for dep in (dependencies or [])
        ],
        "resources": [],
        "authority_state": authority_state,
        "owner_gate": False,
        "human_required": False,
        "priority": priority,
    }


def graph(*nodes: dict, max_wip: int = 2) -> dict:
    return {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": list(nodes),
    }
def indexed(plan: dict) -> dict[str, dict]:
    return {item["node_id"]: item for item in plan["nodes"]}


class GitHubReadinessFactTests(unittest.TestCase):
    def test_open_packet_projects_only_bounded_fact(self) -> None:
        runner = FakeGitHubRunner(body=packet_body())
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        fact = adapter.read_readiness_packet_fact(REPO, 98)
        self.assertEqual(
            fact,
            {
                "repository": REPO,
                "issue_number": 98,
                "branch": "feature/readiness",
                "head": HEAD_A,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            },
        )
        self.assertNotIn("body", fact)
        self.assertNotIn("title", fact)

    def test_missing_queue_state_normalizes_to_none(self) -> None:
        runner = FakeGitHubRunner(
            body=packet_body(status="ACTIVE", queue_state=None)
        )
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        fact = adapter.read_readiness_packet_fact(REPO, 98)
        self.assertEqual(fact["queue_state"], "NONE")

    def test_closed_complete_is_valid_dependency_fact(self) -> None:
        runner = FakeGitHubRunner(
            body=packet_body(
                status="COMPLETE",
                queue_state="NONE",
                branch="feature/foundation",
            ),
            state="CLOSED",
        )
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        fact = adapter.read_readiness_packet_fact(REPO, 47)
        self.assertEqual(fact["packet_status"], "COMPLETE")

    def test_closed_noncomplete_and_untrusted_author_fail_closed(self) -> None:
        closed = GitHubWorkPacketAdapter(
            command_runner=FakeGitHubRunner(
                body=packet_body(status="PAUSED"), state="CLOSED"
            )
        )
        with self.assertRaises(ValidationError):
            closed.read_readiness_packet_fact(REPO, 98)

        untrusted = GitHubWorkPacketAdapter(
            command_runner=FakeGitHubRunner(
                body=packet_body(), permission="read"
            )
        )
        with self.assertRaises(ValidationError):
            untrusted.read_readiness_packet_fact(REPO, 98)

    def test_non_packet_and_malformed_identity_fail_closed(self) -> None:
        bad_title = GitHubWorkPacketAdapter(
            command_runner=FakeGitHubRunner(
                body=packet_body(), title="ordinary issue"
            )
        )
        with self.assertRaises(ValidationError):
            bad_title.read_readiness_packet_fact(REPO, 98)

        bad_head = GitHubWorkPacketAdapter(
            command_runner=FakeGitHubRunner(
                body=packet_body(head="deadbeef")
            )
        )
        with self.assertRaises(ValidationError):
            bad_head.read_readiness_packet_fact(REPO, 98)

        lowercase_status = GitHubWorkPacketAdapter(
            command_runner=FakeGitHubRunner(
                body=packet_body().replace("STATUS=PAUSED", "STATUS=paused")
            )
        )
        with self.assertRaises(ValidationError):
            lowercase_status.read_readiness_packet_fact(REPO, 98)

        lowercase_queue = GitHubWorkPacketAdapter(
            command_runner=FakeGitHubRunner(
                body=packet_body().replace(
                    "QUEUE_STATE=QUEUED", "QUEUE_STATE=queued"
                )
            )
        )
        with self.assertRaises(ValidationError):
            lowercase_queue.read_readiness_packet_fact(REPO, 98)


class GitHubReadinessReconciliationTests(unittest.TestCase):
    def test_exact_canonical_facts_preserve_normal_plan(self) -> None:
        payload = graph(
            graph_node(
                "foundation",
                47,
                branch="feature/foundation",
                packet_status="COMPLETE",
                queue_state="NONE",
            ),
            graph_node(
                "candidate",
                98,
                dependencies=["foundation"],
                priority=5,
            ),
        )
        facts = {
            (REPO, 47): {
                "repository": REPO,
                "issue_number": 47,
                "branch": "feature/foundation",
                "head": HEAD_A,
                "packet_status": "COMPLETE",
                "queue_state": "NONE",
            },
            (REPO, 98): {
                "repository": REPO,
                "issue_number": 98,
                "branch": "feature/readiness",
                "head": HEAD_A,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            },
        }

        def read(repo: str, issue: int) -> dict:
            return facts[(repo, issue)]

        expected = plan_readiness(deepcopy(payload))
        actual = plan_github_reconciled_readiness(payload, read)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["selected_node_ids"], ["candidate"])

    def test_lifecycle_drift_marks_node_stale_and_stops_batch(self) -> None:
        payload = graph(
            graph_node("candidate", 98),
            graph_node(
                "independent",
                99,
                repository=OTHER_REPO,
                branch="feature/independent",
                head=HEAD_B,
            ),
        )
        facts = {
            (REPO, 98): {
                "repository": REPO,
                "issue_number": 98,
                "branch": "feature/readiness",
                "head": HEAD_B,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            },
            (OTHER_REPO, 99): {
                "repository": OTHER_REPO,
                "issue_number": 99,
                "branch": "feature/independent",
                "head": HEAD_B,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            },
        }
        plan = plan_github_reconciled_readiness(
            payload, lambda repo, issue: facts[(repo, issue)]
        )
        states = indexed(plan)
        self.assertEqual(plan["graph_state"], "HUMAN_REQUIRED")
        self.assertEqual(plan["selected_node_ids"], [])
        self.assertEqual(states["candidate"]["readiness"], "HUMAN_REQUIRED")
        self.assertIn("AUTHORITY_STALE", states["candidate"]["reasons"])
        self.assertEqual(states["independent"]["readiness"], "BLOCKED")
        self.assertEqual(states["independent"]["reasons"], ["GRAPH_UNSAFE"])

    def test_read_failure_and_content_bearing_fact_are_untrusted(self) -> None:
        payload = graph(graph_node("candidate", 98))

        def failed(_repo: str, _issue: int) -> dict:
            raise ValidationError("permission lookup failed")

        plan = plan_github_reconciled_readiness(payload, failed)
        item = indexed(plan)["candidate"]
        self.assertEqual(item["readiness"], "HUMAN_REQUIRED")
        self.assertIn("AUTHORITY_UNTRUSTED", item["reasons"])

        def content_bearing(_repo: str, _issue: int) -> dict:
            return {
                "repository": REPO,
                "issue_number": 98,
                "branch": "feature/readiness",
                "head": HEAD_A,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
                "body": "must never cross the boundary",
            }

        plan = plan_github_reconciled_readiness(payload, content_bearing)
        self.assertIn(
            "AUTHORITY_UNTRUSTED", indexed(plan)["candidate"]["reasons"]
        )
    def test_nontrusted_input_authority_is_never_promoted(self) -> None:
        payload = graph(
            graph_node("candidate", 98, authority_state="AMBIGUOUS")
        )

        def exact(_repo: str, _issue: int) -> dict:
            return {
                "repository": REPO,
                "issue_number": 98,
                "branch": "feature/readiness",
                "head": HEAD_A,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            }

        reconciled = reconcile_readiness_graph(payload, exact)
        self.assertEqual(
            reconciled["nodes"][0]["authority_state"], "AMBIGUOUS"
        )
        plan = plan_readiness(reconciled)
        self.assertEqual(plan["selected_node_ids"], [])
        self.assertIn(
            "AUTHORITY_AMBIGUOUS",
            indexed(plan)["candidate"]["reasons"],
        )

    def test_malformed_fact_and_reader_are_rejected_fail_closed(self) -> None:
        payload = graph(graph_node("candidate", 98))
        malformed_cases = [
            {},
            {
                "repository": REPO,
                "issue_number": True,
                "branch": "feature/readiness",
                "head": HEAD_A,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            },
            {
                "repository": REPO,
                "issue_number": 98,
                "branch": "feature/readiness",
                "head": "short",
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
            },
        ]
        for fact in malformed_cases:
            with self.subTest(fact=fact):
                plan = plan_github_reconciled_readiness(
                    payload, lambda _repo, _issue, fact=fact: fact
                )
                self.assertIn(
                    "AUTHORITY_UNTRUSTED",
                    indexed(plan)["candidate"]["reasons"],
                )

        with self.assertRaises(ValidationError):
            reconcile_readiness_graph(payload, None)  # type: ignore[arg-type]

    def test_cli_exposes_read_only_github_plan(self) -> None:
        args = build_parser().parse_args(
            ["readiness", "github-plan", "--graph", "graph.json"]
        )
        self.assertEqual(args.graph, "graph.json")
        self.assertEqual(args.func.__name__, "cmd_readiness_github_plan")


if __name__ == "__main__":
    unittest.main()
