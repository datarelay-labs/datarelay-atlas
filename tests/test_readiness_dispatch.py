from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from atlas.provenance import ValidationError
from atlas.readiness_dispatch import activate_and_dispatch_single_worker
from atlas.work_controller import (
    DispatchRequest,
    DispatchResult,
    DispatchSpawnCleanupUncertainError,
    DispatchSpawnedButUnobservedError,
    GitHubWorkPacketAdapter,
    RecordingCursorDispatcher,
    ResourcePreflightBlocked,
)

REPO = "datarelay-labs/datarelay-atlas"
ISSUE = 109
BRANCH = "feature/readiness-authorized-worker-dispatch"
WORKSTREAM = "readiness-authorized-worker-dispatch"
HEAD = "a" * 40
DIGEST = "b" * 64
WORKTREE = "/tmp"


def selected_node() -> dict:
    return {
        "node_id": f"{REPO}#{ISSUE}",
        "repository": REPO,
        "issue_number": ISSUE,
        "branch": BRANCH,
        "head": HEAD,
    }


def activated() -> dict:
    return {
        "action": "activated",
        "plan_digest": DIGEST,
        "selected_node": selected_node(),
    }


def active_fact(*, workstream: str = WORKSTREAM) -> dict:
    return {
        "repository": REPO,
        "issue_number": ISSUE,
        "branch": BRANCH,
        "workstream": workstream,
        "head": HEAD,
        "audit_base": "",
        "status": "ACTIVE",
        "updated_at": "2026-09-28T00:00:00Z",
    }


class FakePacketAdapter:
    def __init__(
        self,
        *,
        activation: object | None = None,
        fresh: object | None = None,
        activation_error: bool = False,
        fresh_error: bool = False,
    ) -> None:
        self.activation = activated() if activation is None else activation
        self.fresh = active_fact() if fresh is None else fresh
        self.activation_error = activation_error
        self.fresh_error = fresh_error
        self.activation_calls = 0
        self.fresh_calls = 0

    def activate_authorized_readiness_packet(self, _path: Path) -> object:
        self.activation_calls += 1
        if self.activation_error:
            raise ValidationError("activation failed")
        return self.activation

    def reread_trusted_active_readiness_packet(
        self, repository: str, issue_number: int
    ) -> object:
        self.fresh_calls += 1
        if self.fresh_error:
            raise ValidationError("fresh read failed")
        self.last_identity = (repository, issue_number)
        return self.fresh


class RaisingDispatcher:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[DispatchRequest] = []

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self.calls.append(request)
        raise self.error


class InvalidDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def start_resume(self, _request: DispatchRequest) -> object:
        self.calls += 1
        return {"session_id": "not-a-dispatch-result"}


class ReadinessAuthorizedDispatchTests(unittest.TestCase):
    def test_denied_activation_dispatches_nothing(self) -> None:
        adapter = FakePacketAdapter(
            activation={
                "action": "denied",
                "authorization": {
                    "decision": "DENY",
                    "reasons": ["GRAPH_NOT_READY"],
                },
            }
        )
        dispatcher = RecordingCursorDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "denied")
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.fresh_calls, 0)
        self.assertEqual(dispatcher.requests, [])

    def test_activation_failure_is_human_required_without_dispatch(self) -> None:
        adapter = FakePacketAdapter(activation_error=True)
        dispatcher = RecordingCursorDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["reason"], "ACTIVATION_FAILED")
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.fresh_calls, 0)
        self.assertEqual(dispatcher.requests, [])

    def test_confirmed_activation_dispatches_exactly_once(self) -> None:
        adapter = FakePacketAdapter()
        dispatcher = RecordingCursorDispatcher(session_prefix="ready")

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "dispatched")
        self.assertEqual(result["session_id"], "ready-1")
        self.assertEqual(result["resume_prompt"], "/work-resume")
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.fresh_calls, 1)
        self.assertEqual(len(dispatcher.requests), 1)
        request = dispatcher.requests[0]
        self.assertEqual(request.workstream, WORKSTREAM)
        self.assertEqual(request.worktree_path, WORKTREE)
        self.assertEqual(request.repository, REPO)
        self.assertEqual(request.issue_number, ISSUE)
        self.assertEqual(request.branch, BRANCH)
        self.assertEqual(request.expected_head, HEAD)
        self.assertEqual(request.attempt, 1)
        self.assertEqual(request.resume_prompt, "/work-resume")
        encoded = json.dumps(result)
        for forbidden in ("body", "OWNER_INTENT", "prompt_text", "transcript"):
            self.assertNotIn(forbidden, encoded)

    def test_active_packet_drift_or_recheck_failure_never_dispatches(self) -> None:
        for adapter, expected_reason in (
            (
                FakePacketAdapter(
                    fresh=active_fact(workstream="different-workstream")
                ),
                "ACTIVE_PACKET_DRIFT",
            ),
            (
                FakePacketAdapter(fresh_error=True),
                "ACTIVE_PACKET_RECHECK_FAILED",
            ),
        ):
            with self.subTest(reason=expected_reason):
                dispatcher = RecordingCursorDispatcher()
                result = activate_and_dispatch_single_worker(
                    graph_path=Path("/tmp/graph.json"),
                    workstream=WORKSTREAM,
                    worktree_path=WORKTREE,
                    packet_adapter=adapter,  # type: ignore[arg-type]
                    dispatcher=dispatcher,
                )
                self.assertEqual(result["action"], "human_required")
                self.assertEqual(result["reason"], expected_reason)
                self.assertEqual(dispatcher.requests, [])

    def test_dispatch_failures_are_terminal_for_this_invocation(self) -> None:
        cases = [
            (
                ResourcePreflightBlocked(
                    "blocked",
                    result="BLOCK",
                    reason="capacity unavailable",
                    exit_code=3,
                ),
                "RESOURCE_PREFLIGHT_BLOCKED",
            ),
            (
                DispatchSpawnedButUnobservedError(
                    "unobserved",
                    session_hint="proc:123",
                    command=["agent", "persist"],
                ),
                "SPAWNED_BUT_UNOBSERVED",
            ),
            (
                DispatchSpawnCleanupUncertainError(
                    "uncertain",
                    session_hint="proc:124",
                    command=["agent", "persist"],
                    cleanup_error="permission denied",
                ),
                "SPAWN_CLEANUP_UNCERTAIN",
            ),
            (ValidationError("identity drift"), "DISPATCH_BLOCKED"),
        ]
        for error, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                adapter = FakePacketAdapter()
                dispatcher = RaisingDispatcher(error)
                result = activate_and_dispatch_single_worker(
                    graph_path=Path("/tmp/graph.json"),
                    workstream=WORKSTREAM,
                    worktree_path=WORKTREE,
                    packet_adapter=adapter,  # type: ignore[arg-type]
                    dispatcher=dispatcher,
                )
                self.assertEqual(result["action"], "human_required")
                self.assertEqual(result["reason"], expected_reason)
                self.assertEqual(adapter.activation_calls, 1)
                self.assertEqual(adapter.fresh_calls, 1)
                self.assertEqual(len(dispatcher.calls), 1)

    def test_invalid_dispatch_result_does_not_retry(self) -> None:
        adapter = FakePacketAdapter()
        dispatcher = InvalidDispatcher()
        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )
        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["reason"], "DISPATCH_RESULT_INVALID")
        self.assertEqual(dispatcher.calls, 1)

    def test_invalid_local_inputs_fail_before_activation(self) -> None:
        adapter = FakePacketAdapter()
        dispatcher = RecordingCursorDispatcher()
        with self.assertRaises(ValidationError):
            activate_and_dispatch_single_worker(
                graph_path=Path("/tmp/graph.json"),
                workstream="invalid workstream",
                worktree_path=WORKTREE,
                packet_adapter=adapter,  # type: ignore[arg-type]
                dispatcher=dispatcher,
            )
        with self.assertRaises(ValidationError):
            activate_and_dispatch_single_worker(
                graph_path=Path("/tmp/graph.json"),
                workstream=WORKSTREAM,
                worktree_path="",
                packet_adapter=adapter,  # type: ignore[arg-type]
                dispatcher=dispatcher,
            )
        with self.assertRaisesRegex(ValidationError, "not a directory"):
            activate_and_dispatch_single_worker(
                graph_path=Path("/tmp/graph.json"),
                workstream=WORKSTREAM,
                worktree_path="/tmp/atlas-definitely-missing-worktree",
                packet_adapter=adapter,  # type: ignore[arg-type]
                dispatcher=dispatcher,
            )
        self.assertEqual(adapter.activation_calls, 0)
        self.assertEqual(dispatcher.requests, [])


class ActivePacketDispatchBoundaryTests(unittest.TestCase):
    def _body(
        self,
        *,
        version: str = "2",
        queue_state: str = "NONE",
        workstream: str = WORKSTREAM,
    ) -> str:
        return (
            f"PACKET_VERSION={version}\n"
            f"TARGET_REPO={REPO}\n"
            f"WORKSTREAM={workstream}\n"
            "STATUS=ACTIVE\n"
            f"QUEUE_STATE={queue_state}\n"
            f"BRANCH={BRANCH}\n"
            "TASK_KIND=DEVELOPMENT\n"
            "OWNER_INTENT=Dispatch the authorized worker.\n"
            f"LAST_VERIFIED_HEAD={HEAD}\n"
        )

    def _adapter(
        self,
        *,
        body: str | None = None,
        issue_number: int = ISSUE,
        state: str = "OPEN",
        permission: str = "write",
    ) -> GitHubWorkPacketAdapter:
        packet_body = body if body is not None else self._body()

        def runner(
            argv: list[str], _cwd: str
        ) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "number": issue_number,
                            "title": "[AI Work] readiness dispatch",
                            "state": state,
                            "body": packet_body,
                            "updatedAt": "2026-09-28T00:00:00Z",
                            "author": {"login": "trusted-author"},
                        }
                    ),
                    stderr="",
                )
            if argv[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"permission": permission}),
                    stderr="",
                )
            raise AssertionError(argv)

        return GitHubWorkPacketAdapter(command_runner=runner)

    def test_active_effect_reread_returns_bounded_identity(self) -> None:
        fact = self._adapter().reread_trusted_active_readiness_packet(REPO, ISSUE)
        self.assertEqual(fact["repository"], REPO)
        self.assertEqual(fact["issue_number"], ISSUE)
        self.assertEqual(fact["branch"], BRANCH)
        self.assertEqual(fact["workstream"], WORKSTREAM)
        self.assertEqual(fact["head"], HEAD)
        self.assertEqual(fact["status"], "ACTIVE")
        encoded = json.dumps(fact)
        self.assertNotIn("OWNER_INTENT", encoded)
        self.assertNotIn("body", encoded)

    def test_active_effect_reread_rejects_noncanonical_state(self) -> None:
        cases = [
            (self._adapter(issue_number=ISSUE + 1), "identity"),
            (self._adapter(state="CLOSED"), "not OPEN"),
            (self._adapter(permission="read"), "UNTRUSTED"),
            (self._adapter(body=self._body(version="1")), "PACKET_VERSION"),
            (self._adapter(body=self._body(queue_state="QUEUED")), "QUEUE_STATE"),
            (
                self._adapter(body=self._body(workstream="invalid workstream")),
                "branch, workstream, or head",
            ),
        ]
        for adapter, pattern in cases:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValidationError, pattern):
                    adapter.reread_trusted_active_readiness_packet(REPO, ISSUE)


if __name__ == "__main__":
    unittest.main()
