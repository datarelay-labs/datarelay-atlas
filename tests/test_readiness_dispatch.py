from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from atlas.provenance import ValidationError
from atlas.readiness_dispatch import (
    activate_and_authorize_single_worker_handoff,
    activate_and_dispatch_single_worker,
)
from atlas.work_controller import (
    DispatchRequest,
    DispatchResult,
    DispatchSpawnCleanupUncertainError,
    DispatchSpawnedButUnobservedError,
    GitHubWorkPacketAdapter,
    PtyPersistCursorDispatcher,
    ResourcePreflightBlocked,
)

REPO = "datarelay-labs/datarelay-atlas"
ISSUE = 109
PREDECESSOR = 106
BRANCH = "feature/readiness-authorized-worker-dispatch"
WORKSTREAM = "readiness-authorized-worker-dispatch"
HEAD = "a" * 40
PREDECESSOR_HEAD = "c" * 40
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


class FakePacketAdapter(GitHubWorkPacketAdapter):
    def __init__(
        self,
        *,
        activation: object | None = None,
        fresh: object | None = None,
        activation_error: bool = False,
        fresh_error: bool = False,
        uniqueness_error: bool = False,
        implementer: str = "CURSOR",
    ) -> None:
        self.activation = activated() if activation is None else activation
        self.fresh = active_fact() if fresh is None else fresh
        self.activation_error = activation_error
        self.fresh_error = fresh_error
        self.uniqueness_error = uniqueness_error
        self.implementer = implementer
        self.authorization_calls = 0
        self.queued_calls = 0
        self.activation_calls = 0
        self.fresh_calls = 0
        self.uniqueness_calls = 0

    def authorize_readiness_single_effect(self, _path: Path) -> object:
        self.authorization_calls += 1
        if self.activation_error:
            # Activation errors are post-authorization in this fake.
            return {
                "decision": "ALLOW",
                "plan_digest": DIGEST,
                "selected_node": selected_node(),
            }
        if isinstance(self.activation, dict) and self.activation.get("action") == "denied":
            return self.activation.get("authorization")
        if isinstance(self.activation, dict) and self.activation.get("action") == "activated":
            return {
                "decision": "ALLOW",
                "plan_digest": self.activation.get("plan_digest"),
                "selected_node": self.activation.get("selected_node"),
            }
        return self.activation

    def reread_trusted_queued_execution_packet(
        self, repository: str, issue_number: int
    ) -> object:
        self.queued_calls += 1
        base = active_fact()
        base["packet_status"] = "PAUSED"
        base["queue_state"] = "QUEUED"
        base["implementer"] = self.implementer
        base["change_risk"] = getattr(self, "change_risk", "HIGH")
        base["intent_revision"] = getattr(self, "intent_revision", 3)
        base["author_permission"] = getattr(self, "author_permission", "write")
        base["repository"] = repository
        base["issue_number"] = issue_number
        base.pop("status", None)
        return base

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

    def reread_trusted_active_execution_packet(
        self, repository: str, issue_number: int
    ) -> object:
        self.fresh_calls += 1
        if self.fresh_error:
            raise ValidationError("fresh read failed")
        self.last_identity = (repository, issue_number)
        base = dict(self.fresh) if isinstance(self.fresh, dict) else self.fresh
        if not isinstance(base, dict):
            return base
        base.setdefault("queue_state", "NONE")
        base["implementer"] = self.implementer
        base["change_risk"] = getattr(self, "change_risk", "HIGH")
        base["intent_revision"] = getattr(self, "intent_revision", 3)
        base["author_permission"] = getattr(self, "author_permission", "write")
        return base

    def require_unique_active_readiness_packet(
        self,
        repository: str,
        *,
        issue_number: int,
        branch: str,
    ) -> None:
        self.uniqueness_calls += 1
        self.last_unique_identity = (repository, issue_number, branch)
        if self.uniqueness_error:
            raise ValidationError("active packet ambiguity")


class FakePersistentDispatcher(PtyPersistCursorDispatcher):
    def __init__(self, session_prefix: str = "ready") -> None:
        self.requests: list[DispatchRequest] = []
        self.session_prefix = session_prefix
        self._n = 0

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self._n += 1
        self.requests.append(request)
        return DispatchResult(
            session_id=f"{self.session_prefix}-{self._n}",
            command=["agent", "persist", "/work-resume"],
        )


class RaisingDispatcher(PtyPersistCursorDispatcher):
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[DispatchRequest] = []

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self.calls.append(request)
        raise self.error


class InvalidDispatcher(PtyPersistCursorDispatcher):
    def __init__(self) -> None:
        self.calls = 0

    def start_resume(self, _request: DispatchRequest) -> object:
        self.calls += 1
        return {"session_id": "not-a-dispatch-result"}


class SuccessfulPreflightDispatcher(PtyPersistCursorDispatcher):
    def __init__(self) -> None:
        self.calls: list[DispatchRequest] = []

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self.calls.append(request)
        return DispatchResult(
            session_id="ready-preflight",
            command=["agent", "persist", "/work-resume"],
            resource_preflight_result="PASS",
            resource_preflight_reason=(
                "capacity ok for /home/aella/private/worktree "
                "and https://example.invalid/health"
            ),
        )


def chat_git_runner(
    *,
    repository: str = REPO,
    branch: str = BRANCH,
    head: str = HEAD,
    dirty: bool = False,
):
    def run(argv: list[str], cwd: str) -> str:
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return str(Path(cwd).resolve())
        if argv == ["git", "remote", "get-url", "origin"]:
            return f"https://github.com/{repository}.git"
        if argv == ["git", "branch", "--show-current"]:
            return branch
        if argv == ["git", "rev-parse", "HEAD"]:
            return head
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return " M dirty" if dirty else ""
        raise AssertionError(argv)

    return run


class ReadinessAuthorizedHandoffTests(unittest.TestCase):
    def test_chat_handoff_authorizes_without_cursor_dispatcher(self) -> None:
        adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")

        result = activate_and_authorize_single_worker_handoff(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,
            git_runner=chat_git_runner(),
        )

        self.assertEqual(result["result"], "AUTHORIZED_HANDOFF")
        self.assertEqual(result["action"], "authorized_handoff")
        self.assertEqual(result["execution_profile"], "CHATGPT_CHAT")
        self.assertEqual(result["adapter"], "EXTERNAL_HANDOFF")
        self.assertFalse(result["spawned"])
        self.assertRegex(result["authorization_digest"], r"^[0-9a-f]{64}$")
        payload = result["resume_payload"]
        self.assertEqual(payload["repository"], REPO)
        self.assertEqual(payload["issue_number"], ISSUE)
        self.assertEqual(payload["branch"], BRANCH)
        self.assertEqual(payload["head"], HEAD)
        self.assertEqual(payload["workstream"], WORKSTREAM)
        self.assertEqual(payload["worktree_path"], WORKTREE)
        self.assertEqual(payload["implementer"], "CHATGPT_CHAT")
        self.assertEqual(payload["change_risk"], "HIGH")
        self.assertEqual(payload["intent_revision"], 3)
        self.assertEqual(payload["author_permission"], "write")
        self.assertEqual(
            payload["authorization_digest"], result["authorization_digest"]
        )
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.uniqueness_calls, 1)
        encoded = json.dumps(result)
        for forbidden in (
            "session_id",
            "resume_prompt",
            "prompt_text",
            "transcript",
            "OWNER_INTENT",
            "## Goal",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_activation_denial_normalizes_to_human_required(self) -> None:
        adapter = FakePacketAdapter(
            activation={
                "action": "denied",
                "authorization": {
                    "decision": "DENY",
                    "reasons": ["GRAPH_NOT_READY"],
                },
            }
        )

        result = activate_and_authorize_single_worker_handoff(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,
            git_runner=chat_git_runner(),
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["result"], "HUMAN_REQUIRED")
        self.assertEqual(result["reason"], "ACTIVATION_DENIED")
        self.assertEqual(
            result["authorization"]["reasons"], ["GRAPH_NOT_READY"]
        )

    def test_chat_handoff_implementer_mismatch_fails_closed(self) -> None:
        adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")
        adapter.implementer = "CURSOR"

        result = activate_and_authorize_single_worker_handoff(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,
            git_runner=chat_git_runner(),
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["result"], "HUMAN_REQUIRED")
        self.assertEqual(result["reason"], "IMPLEMENTER_PROFILE_MISMATCH")
        self.assertEqual(adapter.activation_calls, 0)

    def test_chat_handoff_wrong_or_dirty_worktree_fails_before_activation(self) -> None:
        cases = (
            (chat_git_runner(repository="datarelay-labs/other"), "wrong repository"),
            (chat_git_runner(branch="feature/other"), "wrong branch"),
            (chat_git_runner(head="f" * 40), "wrong head"),
            (chat_git_runner(dirty=True), "dirty worktree"),
        )
        for runner, label in cases:
            with self.subTest(label=label):
                adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")
                result = activate_and_authorize_single_worker_handoff(
                    graph_path=Path("/tmp/graph.json"),
                    workstream=WORKSTREAM,
                    worktree_path=WORKTREE,
                    packet_adapter=adapter,
                    git_runner=runner,
                )
                self.assertEqual(result["action"], "human_required")
                self.assertEqual(result["reason"], "WORKTREE_IDENTITY_REFUSED")
                self.assertEqual(adapter.activation_calls, 0)

    def test_chat_handoff_invalid_queued_metadata_fails_before_activation(self) -> None:
        for field, value in (
            ("change_risk", "UNKNOWN"),
            ("intent_revision", 0),
            ("author_permission", "read"),
        ):
            with self.subTest(field=field):
                adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")
                setattr(adapter, field, value)
                result = activate_and_authorize_single_worker_handoff(
                    graph_path=Path("/tmp/graph.json"),
                    workstream=WORKSTREAM,
                    worktree_path=WORKTREE,
                    packet_adapter=adapter,
                    git_runner=chat_git_runner(),
                )
                self.assertEqual(result["action"], "human_required")
                self.assertEqual(
                    result["reason"], "EXECUTION_PACKET_METADATA_INVALID"
                )
                self.assertEqual(adapter.activation_calls, 0)

    def test_non_chat_profile_has_no_implicit_adapter(self) -> None:
        adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")

        result = activate_and_authorize_single_worker_handoff(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,
            implementer_profile="CURSOR",
            git_runner=chat_git_runner(),
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["result"], "HUMAN_REQUIRED")
        self.assertEqual(result["reason"], "EXECUTION_ADAPTER_NOT_CONFIGURED")
        self.assertEqual(adapter.activation_calls, 0)

    def test_chat_handoff_packet_metadata_or_recheck_failure_fails_closed(self) -> None:
        cases = []
        for field, value in (
            ("change_risk", "UNKNOWN"),
            ("intent_revision", 0),
            ("author_permission", "read"),
        ):
            adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")
            setattr(adapter, field, value)
            cases.append((adapter, "EXECUTION_PACKET_METADATA_INVALID"))
        cases.append(
            (
                FakePacketAdapter(
                    fresh_error=True,
                    implementer="CHATGPT_CHAT",
                ),
                "ACTIVE_EXECUTION_PACKET_RECHECK_FAILED",
            )
        )

        for adapter, reason in cases:
            with self.subTest(reason=reason):
                result = activate_and_authorize_single_worker_handoff(
                    graph_path=Path("/tmp/graph.json"),
                    workstream=WORKSTREAM,
                    worktree_path=WORKTREE,
                    packet_adapter=adapter,
                    git_runner=chat_git_runner(),
                )
                self.assertEqual(result["action"], "human_required")
                self.assertEqual(result["result"], "HUMAN_REQUIRED")
                self.assertEqual(result["reason"], reason)
                if reason == "EXECUTION_PACKET_METADATA_INVALID":
                    self.assertEqual(adapter.activation_calls, 0)

    def test_chat_handoff_digest_is_deterministic(self) -> None:
        first = activate_and_authorize_single_worker_handoff(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=FakePacketAdapter(implementer="CHATGPT_CHAT"),
            git_runner=chat_git_runner(),
        )
        second = activate_and_authorize_single_worker_handoff(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=FakePacketAdapter(implementer="CHATGPT_CHAT"),
            git_runner=chat_git_runner(),
        )
        self.assertEqual(
            first["authorization_digest"], second["authorization_digest"]
        )
        self.assertEqual(first["resume_payload"], second["resume_payload"])


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
        dispatcher = FakePersistentDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "denied")
        self.assertEqual(adapter.authorization_calls, 1)
        self.assertEqual(adapter.activation_calls, 0)
        self.assertEqual(adapter.queued_calls, 0)
        self.assertEqual(adapter.fresh_calls, 0)
        self.assertEqual(dispatcher.requests, [])

    def test_activation_failure_is_human_required_without_dispatch(self) -> None:
        adapter = FakePacketAdapter(activation_error=True)
        dispatcher = FakePersistentDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["reason"], "ACTIVATION_FAILED")
        self.assertEqual(adapter.authorization_calls, 1)
        self.assertEqual(adapter.queued_calls, 1)
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.fresh_calls, 0)
        self.assertEqual(dispatcher.requests, [])

    def test_confirmed_activation_dispatches_exactly_once(self) -> None:
        adapter = FakePacketAdapter()
        dispatcher = FakePersistentDispatcher(session_prefix="ready")

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "dispatched")
        self.assertEqual(result["result"], "DISPATCHED")
        self.assertEqual(result["execution_profile"], "CURSOR")
        self.assertEqual(result["adapter"], "PTY_PERSIST_CURSOR")
        self.assertTrue(result["spawned"])
        self.assertEqual(result["session_id"], "ready-1")
        self.assertEqual(result["resume_prompt"], "/work-resume")
        self.assertEqual(adapter.authorization_calls, 1)
        self.assertEqual(adapter.queued_calls, 1)
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.fresh_calls, 1)
        self.assertEqual(adapter.uniqueness_calls, 1)
        self.assertEqual(
            adapter.last_unique_identity, (REPO, ISSUE, BRANCH)
        )
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

    def test_chat_packet_cannot_enter_legacy_cursor_adapter(self) -> None:
        adapter = FakePacketAdapter(implementer="CHATGPT_CHAT")
        dispatcher = FakePersistentDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["reason"], "IMPLEMENTER_PROFILE_MISMATCH")
        self.assertEqual(adapter.authorization_calls, 1)
        self.assertEqual(adapter.queued_calls, 1)
        self.assertEqual(adapter.activation_calls, 0)
        self.assertEqual(dispatcher.requests, [])

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
                dispatcher = FakePersistentDispatcher()
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

    def test_active_uniqueness_failure_never_dispatches(self) -> None:
        adapter = FakePacketAdapter(uniqueness_error=True)
        dispatcher = FakePersistentDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "human_required")
        self.assertEqual(result["reason"], "ACTIVE_PACKET_UNIQUENESS_FAILED")
        self.assertEqual(adapter.activation_calls, 1)
        self.assertEqual(adapter.fresh_calls, 1)
        self.assertEqual(adapter.uniqueness_calls, 1)
        self.assertEqual(dispatcher.requests, [])

    def test_success_preflight_reason_redacts_absolute_paths(self) -> None:
        adapter = FakePacketAdapter()
        dispatcher = SuccessfulPreflightDispatcher()

        result = activate_and_dispatch_single_worker(
            graph_path=Path("/tmp/graph.json"),
            workstream=WORKSTREAM,
            worktree_path=WORKTREE,
            packet_adapter=adapter,  # type: ignore[arg-type]
            dispatcher=dispatcher,
        )

        self.assertEqual(result["action"], "dispatched")
        self.assertEqual(result["resource_preflight_result"], "PASS")
        reason = result["resource_preflight_reason"]
        self.assertNotIn("/home/aella/private/worktree", reason)
        self.assertIn("<local-path>", reason)
        self.assertIn("https://example.invalid/health", reason)

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

    def test_dispatch_boundary_requires_canonical_adapter_and_dispatcher(self) -> None:
        adapter = FakePacketAdapter()
        with self.assertRaisesRegex(
            ValidationError, "requires GitHubWorkPacketAdapter"
        ):
            activate_and_dispatch_single_worker(
                graph_path=Path("/tmp/graph.json"),
                workstream=WORKSTREAM,
                worktree_path=WORKTREE,
                packet_adapter=object(),  # type: ignore[arg-type]
                dispatcher=FakePersistentDispatcher(),
            )
        with self.assertRaisesRegex(
            ValidationError, "requires PtyPersistCursorDispatcher"
        ):
            activate_and_dispatch_single_worker(
                graph_path=Path("/tmp/graph.json"),
                workstream=WORKSTREAM,
                worktree_path=WORKTREE,
                packet_adapter=adapter,
                dispatcher=object(),  # type: ignore[arg-type]
            )
        self.assertEqual(adapter.activation_calls, 0)

    def test_invalid_local_inputs_fail_before_activation(self) -> None:
        adapter = FakePacketAdapter()
        dispatcher = FakePersistentDispatcher()
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
        implementer: str = "CHATGPT_CHAT",
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
            f"AFTER_ISSUE={PREDECESSOR}\n"
            f"IMPLEMENTER={implementer}\n"
            "CHANGE_RISK=HIGH\n"
            "INTENT_REVISION=3\n"
        )

    def _adapter(
        self,
        *,
        body: str | None = None,
        issue_number: int = ISSUE,
        state: str = "OPEN",
        permission: str = "write",
        extra_active: bool = False,
        predecessor_status: str = "COMPLETE",
    ) -> GitHubWorkPacketAdapter:
        packet_body = body if body is not None else self._body()

        def runner(
            argv: list[str], _cwd: str
        ) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["gh", "issue", "view"]:
                requested = int(argv[3])
                if requested == PREDECESSOR:
                    predecessor_body = (
                        "PACKET_VERSION=2\n"
                        f"TARGET_REPO={REPO}\n"
                        f"WORKSTREAM={WORKSTREAM}\n"
                        f"STATUS={predecessor_status}\n"
                        "QUEUE_STATE=NONE\n"
                        f"BRANCH={BRANCH}\n"
                        "TASK_KIND=DEVELOPMENT\n"
                        "OWNER_INTENT=Completed predecessor.\n"
                        f"LAST_VERIFIED_HEAD={PREDECESSOR_HEAD}\n"
                    )
                    payload = {
                        "number": PREDECESSOR,
                        "title": "[AI Work] readiness predecessor",
                        "state": (
                            "CLOSED"
                            if predecessor_status == "COMPLETE"
                            else "OPEN"
                        ),
                        "body": predecessor_body,
                        "updatedAt": "2026-09-28T00:00:00Z",
                        "author": {"login": "predecessor-author"},
                    }
                else:
                    payload = {
                        "number": issue_number,
                        "title": "[AI Work] readiness dispatch",
                        "state": state,
                        "body": packet_body,
                        "updatedAt": "2026-09-28T00:00:00Z",
                        "author": {"login": "trusted-author"},
                    }
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
                issues = []
                if state == "OPEN" and "STATUS=ACTIVE" in packet_body:
                    issues.append(
                        {
                            "number": issue_number,
                            "title": "[AI Work] readiness dispatch",
                            "state": "open",
                            "body": packet_body,
                            "user": {"login": "trusted-author"},
                        }
                    )
                if extra_active:
                    issues.append(
                        {
                            "number": ISSUE + 50,
                            "title": "[AI Work] graph omitted active",
                            "state": "open",
                            "body": packet_body.replace(
                                BRANCH, "feature/other-active", 1
                            ),
                            "user": {"login": "other-active-author"},
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
                effective_permission = (
                    permission
                    if "trusted-author/permission" in path
                    else "write"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"permission": effective_permission}),
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

    def test_active_execution_reread_returns_bounded_authority_facts(self) -> None:
        fact = self._adapter().reread_trusted_active_execution_packet(
            REPO, ISSUE
        )
        self.assertEqual(fact["repository"], REPO)
        self.assertEqual(fact["issue_number"], ISSUE)
        self.assertEqual(fact["branch"], BRANCH)
        self.assertEqual(fact["workstream"], WORKSTREAM)
        self.assertEqual(fact["head"], HEAD)
        self.assertEqual(fact["status"], "ACTIVE")
        self.assertEqual(fact["queue_state"], "NONE")
        self.assertEqual(fact["implementer"], "CHATGPT_CHAT")
        self.assertEqual(fact["change_risk"], "HIGH")
        self.assertEqual(fact["intent_revision"], 3)
        self.assertEqual(fact["author_permission"], "write")
        encoded = json.dumps(fact)
        for forbidden in ("OWNER_INTENT", "body", "## Goal", "prompt"):
            self.assertNotIn(forbidden, encoded)

    def test_active_execution_reread_rejects_untrusted_or_invalid_metadata(self) -> None:
        cases = (
            (
                self._adapter(
                    body=self._body().replace(
                        "IMPLEMENTER=CHATGPT_CHAT",
                        "IMPLEMENTER=invalid profile",
                    )
                ),
                "IMPLEMENTER",
            ),
            (
                self._adapter(
                    body=self._body().replace(
                        "CHANGE_RISK=HIGH", "CHANGE_RISK=UNKNOWN"
                    )
                ),
                "CHANGE_RISK",
            ),
            (
                self._adapter(
                    body=self._body().replace(
                        "INTENT_REVISION=3", "INTENT_REVISION=0"
                    )
                ),
                "INTENT_REVISION",
            ),
            (self._adapter(permission="read"), "UNTRUSTED"),
        )
        for adapter, pattern in cases:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValidationError, pattern):
                    adapter.reread_trusted_active_execution_packet(
                        REPO, ISSUE
                    )

    def test_queued_execution_profile_is_bounded_and_explicit(self) -> None:
        body = (
            self._body(implementer="CURSOR")
            .replace("STATUS=ACTIVE", "STATUS=PAUSED", 1)
            .replace("QUEUE_STATE=NONE", "QUEUE_STATE=QUEUED", 1)
        )
        fact = self._adapter(
            body=body
        ).reread_trusted_queued_execution_packet(REPO, ISSUE)

        self.assertEqual(fact["repository"], REPO)
        self.assertEqual(fact["issue_number"], ISSUE)
        self.assertEqual(fact["branch"], BRANCH)
        self.assertEqual(fact["workstream"], WORKSTREAM)
        self.assertEqual(fact["head"], HEAD)
        self.assertEqual(fact["packet_status"], "PAUSED")
        self.assertEqual(fact["queue_state"], "QUEUED")
        self.assertEqual(fact["implementer"], "CURSOR")
        encoded = json.dumps(fact)
        for forbidden in ("OWNER_INTENT", "body", "## Goal", "prompt"):
            self.assertNotIn(forbidden, encoded)

    def test_queued_execution_profile_rejects_missing_or_chat_drift(self) -> None:
        missing = (
            self._body()
            .replace("STATUS=ACTIVE", "STATUS=PAUSED", 1)
            .replace("QUEUE_STATE=NONE", "QUEUE_STATE=QUEUED", 1)
            .replace("IMPLEMENTER=CHATGPT_CHAT\n", "", 1)
        )
        with self.assertRaisesRegex(ValidationError, "IMPLEMENTER"):
            self._adapter(
                body=missing
            ).reread_trusted_queued_execution_packet(REPO, ISSUE)

    def test_dispatch_uniqueness_gate_rejects_graph_omitted_active(self) -> None:
        adapter = self._adapter(extra_active=True)
        with self.assertRaisesRegex(
            ValidationError, "ACTIVE occupancy is not unique"
        ):
            adapter.require_unique_active_readiness_packet(
                REPO,
                issue_number=ISSUE,
                branch=BRANCH,
            )

    def test_active_effect_reread_rejects_predecessor_or_occupancy_drift(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "predecessor is not COMPLETE"
        ):
            self._adapter(
                predecessor_status="PAUSED"
            ).reread_trusted_active_readiness_packet(REPO, ISSUE)

        with self.assertRaisesRegex(
            ValidationError, "ACTIVE occupancy is not unique"
        ):
            self._adapter(
                extra_active=True
            ).reread_trusted_active_readiness_packet(REPO, ISSUE)

        with self.assertRaisesRegex(
            ValidationError, "issue_number is invalid"
        ):
            self._adapter().reread_trusted_active_readiness_packet(
                REPO, True  # type: ignore[arg-type]
            )

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
