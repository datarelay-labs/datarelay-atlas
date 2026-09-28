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
PREDECESSOR = 103
BRANCH = "feature/candidate"
HEAD = "a" * 40
PREDECESSOR_HEAD = "b" * 40


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
        f"AFTER_ISSUE={PREDECESSOR}\n"
        "\n"
        "## Goal\n\n"
        "Preserve this body exactly except lifecycle metadata.  \n"
        "\n"
        "## Blockers\n\n"
        "NONE\n\n"
    )


def predecessor_body() -> str:
    return (
        "PACKET_VERSION=2\n"
        f"TARGET_REPO={REPO}\n"
        "WORKSTREAM=readiness-candidate\n"
        "STATUS=COMPLETE\n"
        "QUEUE_STATE=NONE\n"
        f"BRANCH={BRANCH}\n"
        "TASK_KIND=DEVELOPMENT\n"
        "OWNER_INTENT=Completed readiness predecessor.\n"
        f"LAST_VERIFIED_HEAD={PREDECESSOR_HEAD}\n"
        "\n"
        "## Goal\n\n"
        "Completed predecessor.\n"
    )


def graph_payload(
    *,
    max_wip: int = 1,
    include_predecessor: bool = True,
) -> dict:
    candidate = {
        "node_id": "candidate",
        "issue_number": ISSUE,
        "repository": REPO,
        "branch": BRANCH,
        "head": HEAD,
        "packet_status": "PAUSED",
        "queue_state": "QUEUED",
        "dependencies": (
            [{"node_id": "predecessor", "relation": "REQUIRES_COMPLETE"}]
            if include_predecessor
            else []
        ),
        "resources": [],
        "authority_state": "TRUSTED",
        "owner_gate": False,
        "human_required": False,
        "priority": 1,
    }
    predecessor = {
        "node_id": "predecessor",
        "issue_number": PREDECESSOR,
        "repository": REPO,
        "branch": BRANCH,
        "head": PREDECESSOR_HEAD,
        "packet_status": "COMPLETE",
        "queue_state": "NONE",
        "dependencies": [],
        "resources": [],
        "authority_state": "TRUSTED",
        "owner_gate": False,
        "human_required": False,
        "priority": 0,
    }
    return {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": [candidate, predecessor] if include_predecessor else [candidate],
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
        self.candidate_view_count = 0
        self.predecessor_view_count = 0
        self.predecessor_pause_on_view: int | None = None
        self.permission_count = 0
        self.edit_count = 0
        self.open_issues: list[dict] = []
        self.issue_list_count = 0
        self.active_on_list: int | None = None
        self.mutate_on_view: int | None = None
        self.activate_on_view: int | None = None
        self.close_on_view: int | None = None
        self.wrong_number_on_view: int | None = None
        self.downgrade_on_permission: int | None = None
        self.corrupt_edit = False

    def __call__(
        self, argv: list[str], _cwd: str
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "issue", "view"]:
            self.view_count += 1
            requested = int(argv[3])
            if requested == PREDECESSOR:
                self.predecessor_view_count += 1
                body = predecessor_body()
                state = "CLOSED"
                if self.predecessor_pause_on_view == self.predecessor_view_count:
                    body = body.replace("STATUS=COMPLETE", "STATUS=PAUSED", 1)
                    state = "OPEN"
                payload = {
                    "number": PREDECESSOR,
                    "title": "[AI Work] readiness predecessor",
                    "state": state,
                    "body": body,
                    "updatedAt": "2026-09-28T00:00:00Z",
                    "author": {"login": "predecessor-author"},
                }
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            self.candidate_view_count += 1
            current = self.candidate_view_count
            if self.mutate_on_view == current:
                self.body += "\n<!-- concurrent change -->\n"
                self.updated_at = "2026-09-28T00:00:01Z"
            if self.activate_on_view == current:
                self.body = self.body.replace(
                    "STATUS=PAUSED", "STATUS=ACTIVE", 1
                )
            if self.close_on_view == current:
                self.state = "CLOSED"
            payload = {
                "number": (
                    ISSUE + 1
                    if self.wrong_number_on_view == current
                    else ISSUE
                ),
                "title": self.title,
                "state": self.state,
                "body": self.body,
                "updatedAt": self.updated_at,
                "author": {"login": "trusted-author"},
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )
        if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
            self.issue_list_count += 1
            issues = list(self.open_issues)
            issues.append(
                {
                    "number": ISSUE,
                    "title": self.title,
                    "state": "open",
                    "body": self.body,
                    "user": {"login": "trusted-author"},
                }
            )
            if self.active_on_list == self.issue_list_count:
                issues.append(
                    {
                        "number": 201,
                        "title": "[AI Work] late active packet",
                        "state": "open",
                        "body": (
                            packet_body()
                            .replace("STATUS=PAUSED", "STATUS=ACTIVE", 1)
                            .replace("QUEUE_STATE=QUEUED", "QUEUE_STATE=NONE", 1)
                            .replace(BRANCH, "feature/late-active", 1)
                        ),
                        "user": {"login": "late-active-author"},
                    }
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps([issues]),
                stderr="",
            )
        if argv[:2] == ["gh", "api"]:
            path = argv[2] if len(argv) > 2 else ""
            if "trusted-author/permission" in path:
                self.permission_count += 1
                permission = self.permission
                if self.downgrade_on_permission == self.permission_count:
                    permission = "read"
            else:
                permission = "write"
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
            packet_body().replace("PACKET_VERSION=2", "PACKET_VERSION=1", 1),
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

    def test_denied_after_github_reconciliation_still_performs_zero_writes(self) -> None:
        runner = FakeGitHubRunner()
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        payload = graph_payload()
        payload["nodes"][0]["owner_gate"] = True
        path = write_graph(payload)
        try:
            result = adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["action"], "denied")
        self.assertEqual(result["authorization"]["decision"], "DENY")
        self.assertGreaterEqual(runner.view_count, 2)
        self.assertGreaterEqual(runner.permission_count, 2)
        self.assertEqual(runner.edit_count, 0)

    def test_lifecycle_drift_after_authorization_blocks_before_edit(self) -> None:
        runner = FakeGitHubRunner()
        # Double authorization reads are views 1-2. The mandatory fresh
        # selected-packet fact is view 3.
        runner.activate_on_view = 3
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "changed before activation"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_issue_closure_at_cas_boundary_blocks_edit(self) -> None:
        runner = FakeGitHubRunner()
        # Views 1-2 authorize, 3 fresh-fact, 4 payload, 5 CAS recheck.
        runner.close_on_view = 5
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "is not OPEN"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_issue_identity_drift_at_cas_boundary_blocks_edit(self) -> None:
        runner = FakeGitHubRunner()
        # Views 1-2 authorize, 3 fresh-fact, 4 payload, 5 CAS recheck.
        runner.wrong_number_on_view = 5
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "identity changed during mutation"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_graph_omitting_canonical_predecessor_blocks_write(self) -> None:
        runner = FakeGitHubRunner()
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload(include_predecessor=False))
        try:
            with self.assertRaisesRegex(
                ValidationError, "omits canonical predecessor"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_repository_active_packet_omitted_from_graph_blocks_write(self) -> None:
        runner = FakeGitHubRunner()
        active_body = (
            packet_body()
            .replace("STATUS=PAUSED", "STATUS=ACTIVE", 1)
            .replace("QUEUE_STATE=QUEUED", "QUEUE_STATE=NONE", 1)
            .replace(BRANCH, "feature/already-active", 1)
        )
        runner.open_issues = [
            {
                "number": 200,
                "title": "[AI Work] existing active packet",
                "state": "open",
                "body": active_body,
                "user": {"login": "active-author"},
            }
        ]
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "already has trusted ACTIVE"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_predecessor_changing_at_pre_edit_recheck_blocks_write(self) -> None:
        runner = FakeGitHubRunner()
        # Initial double authorization observes predecessor views 1-2 and the
        # first effect-boundary predecessor read is view 3. Repeated
        # authorization uses 4-5; the final pre-edit predecessor read is 6.
        runner.predecessor_pause_on_view = 6
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "canonical predecessor changed before edit"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_active_packet_appearing_at_pre_edit_recheck_blocks_write(self) -> None:
        runner = FakeGitHubRunner()
        runner.active_on_list = 2
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "became ACTIVE before edit"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.issue_list_count, 2)
        self.assertEqual(runner.edit_count, 0)

    def test_malformed_active_issue_number_fails_closed(self) -> None:
        runner = FakeGitHubRunner()
        active_body = (
            packet_body()
            .replace("STATUS=PAUSED", "STATUS=ACTIVE", 1)
            .replace("QUEUE_STATE=QUEUED", "QUEUE_STATE=NONE", 1)
        )
        runner.open_issues = [
            {
                "number": True,
                "title": "[AI Work] malformed active packet",
                "state": "open",
                "body": active_body,
                "user": {"login": "active-author"},
            }
        ]
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "candidate issue number invalid"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 0)

    def test_post_write_active_conflict_fails_closed(self) -> None:
        runner = FakeGitHubRunner()
        runner.active_on_list = 3
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "post-write ACTIVE occupancy is not unique"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 1)

    def test_post_write_predecessor_drift_fails_closed(self) -> None:
        runner = FakeGitHubRunner()
        runner.predecessor_pause_on_view = 7
        adapter = GitHubWorkPacketAdapter(command_runner=runner)
        path = write_graph(graph_payload())
        try:
            with self.assertRaisesRegex(
                ValidationError, "predecessor changed after edit"
            ):
                adapter.activate_authorized_readiness_packet(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(runner.edit_count, 1)

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
