from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.provenance import ValidationError
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    render_readiness_packet_active_body,
)

REPO = "datarelay-labs/datarelay-atlas"
ISSUE = 106
BRANCH = "feature/candidate"
HEAD = "a" * 40


def packet_body() -> str:
    return (
        "PACKET_VERSION=2\n"
        f"TARGET_REPO={REPO}\n"
        "WORKSTREAM=readiness-candidate\n"
        "STATUS=PAUSED\n"
        "QUEUE_STATE=QUEUED\n"
        f"BRANCH={BRANCH}\n"
        "TASK_KIND=DEVELOPMENT\n"
        "OWNER_INTENT=Activate only after readiness authorization.\n"
        f"LAST_VERIFIED_HEAD={HEAD}\n"
        "AFTER_ISSUE=103\n"
        "\n"
        "## Goal\n\n"
        "Preserve this body exactly except lifecycle metadata.  \n"
        "\n"
        "## Blockers\n\n"
        "NONE\n\n"
    )


def graph_payload(*, max_wip: int = 1) -> dict:
    return {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": [
            {
                "node_id": "candidate",
                "issue_number": ISSUE,
                "repository": REPO,
                "branch": BRANCH,
                "head": HEAD,
                "packet_status": "PAUSED",
                "queue_state": "QUEUED",
                "dependencies": [],
                "resources": [],
                "authority_state": "TRUSTED",
                "owner_gate": False,
                "human_required": False,
                "priority": 1,
            }
        ],
    }


def write_graph(payload: dict) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


class FakeGitHubRunner:
    def __init__(self) -> None:
        self.body = packet_body()
        self.updated_at = "2026-09-28T00:00:00Z"
        self.permission = "write"
        self.state = "OPEN"
        self.title = "[AI Work] readiness candidate"
        self.view_count = 0
        self.permission_count = 0
        self.edit_count = 0
        self.mutate_on_view: int | None = None
        self.activate_on_view: int | None = None
        self.close_on_view: int | None = None
        self.downgrade_on_permission: int | None = None
        self.corrupt_edit = False

    def __call__(
        self, argv: list[str], _cwd: str
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "issue", "view"]:
            self.view_count += 1
            if self.mutate_on_view == self.view_count:
                self.body += "\n<!-- concurrent change -->\n"
                self.updated_at = "2026-09-28T00:00:01Z"
            if self.activate_on_view == self.view_count:
                self.body = self.body.replace(
                    "STATUS=PAUSED", "STATUS=ACTIVE", 1
                )
            if self.close_on_view == self.view_count:
                self.state = "CLOSED"
            payload = {
                "number": ISSUE,
                "title": self.title,
                "state": self.state,
                "body": self.body,
                "updatedAt": self.updated_at,
                "author": {"login": "trusted-author"},
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )
        if argv[:2] == ["gh", "api"]:
            self.permission_count += 1
            permission = self.permission
            if self.downgrade_on_permission == self.permission_count:
                permission = "read"
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"permission": permission}),
                stderr="",
            )
        if argv[:3] == ["gh", "issue", "edit"]:
            self.edit_count += 1
            body_path = Path(argv[argv.index("--body-file") + 1])
            requested = body_path.read_text(encoding="utf-8")
            self.body = packet_body() if self.corrupt_edit else requested
            self.updated_at = "2026-09-28T00:00:02Z"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)


class ReadinessActivationTests(unittest.TestCase):
    def test_renderer_changes_only_status_and_queue_lines(self) -> None:
        before = packet_body()
        after = render_readiness_packet_active_body(
            before,
            repository=REPO,
            branch=BRANCH,
            head=HEAD,
        )
        expected = before.replace("STATUS=PAUSED", "STATUS=ACTIVE", 1).replace(
            "QUEUE_STATE=QUEUED", "QUEUE_STATE=NONE", 1
        )
        self.assertEqual(after, expected)

    def test_renderer_rejects_nonqueued_or_identity_drift(self) -> None:
        cases = [
            packet_body().replace("STATUS=PAUSED", "STATUS=ACTIVE", 1),
            packet_body().replace("QUEUE_STATE=QUEUED", "QUEUE_STATE=NONE", 1),
            packet_body().replace(BRANCH, "feature/other", 1),
            packet_body().replace(HEAD, "b" * 40, 1),
            packet_body().replace("PACKET_VERSION=2\n", "", 1),
            packet_body().replace(
                "WORKSTREAM=readiness-candidate",
                "WORKSTREAM=invalid workstream",
                1,
            ),
        ]
        for body in cases:
            with self.subTest(body=body[:120]):
                with self.assertRaises(ValidationError):
                    render_readiness_packet_active_body(
                        body,
                        repository=REPO,
                        branch=BRANCH,
                        head=HEAD,
                    )

    def test_denied_authorization_performs_zero_github_io(self) -> None:
        runner = FakeGitHubRunner()
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload(max_wip=2))
        try:
            result = adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["action"], "denied")
        self.assertEqual(result["authorization"]["decision"], "DENY")
        self.assertEqual(runner.view_count, 0)
        self.assertEqual(runner.permission_count, 0)
        self.assertEqual(runner.edit_count, 0)

    def test_authorized_activation_is_content_free_and_confirmed(self) -> None:
        runner = FakeGitHubRunner()
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            result = adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(result["action"], "activated")
        self.assertEqual(result["selected_node"]["repository"], REPO)
        self.assertEqual(result["selected_node"]["issue_number"], ISSUE)
        self.assertRegex(result["plan_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(runner.edit_count, 1)
        self.assertIn("STATUS=ACTIVE", runner.body)
        self.assertIn("QUEUE_STATE=NONE", runner.body)
        encoded = json.dumps(result)
        for forbidden in ("body", "OWNER_INTENT", "prompt", "transcript"):
            self.assertNotIn(forbidden, encoded)

    def test_concurrent_body_change_blocks_edit(self) -> None:
        runner = FakeGitHubRunner()
        # Two authorization reads, one fresh fact, one mutation payload,
        # then the CAS re-read.
        runner.mutate_on_view = 5
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "changed during mutation"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_permission_downgrade_at_cas_blocks_edit(self) -> None:
        runner = FakeGitHubRunner()
        # Two authorization permission reads, one fresh fact, one payload
        # trust check, then the CAS trust recheck.
        runner.downgrade_on_permission = 5
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "WORK_PACKET_AUTHOR_UNTRUSTED"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_unconfirmed_write_fails_closed(self) -> None:
        runner = FakeGitHubRunner()
        runner.corrupt_edit = True
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "activation was not confirmed"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 1)


if __name__ == "__main__":
    unittest.main()
