"""Provider-neutral single-worker activation with an explicit legacy Cursor adapter.

This module composes the existing readiness activation primitive with the
existing exact-head persistent dispatcher.  It deliberately has no retry loop:
once a packet is activated, an uncertain or failed dispatch remains explicit
operator recovery state rather than replayable authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from atlas.provenance import ValidationError
from atlas.work_controller import (
    RESUME_PROMPT,
    DispatchRequest,
    DispatchResult,
    DispatchSpawnCleanupUncertainError,
    DispatchSpawnedButUnobservedError,
    GitHubWorkPacketAdapter,
    PtyPersistCursorDispatcher,
    ResourcePreflightBlocked,
    WORKSTREAM_RE,
    redact_absolute_paths,
)

_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_EXECUTION_PROFILE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CHATGPT_CHAT = "CHATGPT_CHAT"
_EXECUTION_RESULT_SCHEMA_VERSION = 1


def _selected_node(value: object) -> dict[str, Any]:
    expected = {"node_id", "repository", "issue_number", "branch", "head"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationError("readiness dispatch selected node is invalid")
    node_id = value["node_id"]
    repository = value["repository"]
    issue_number = value["issue_number"]
    branch = value["branch"]
    head = value["head"]
    if not isinstance(node_id, str) or not node_id or len(node_id) > 255:
        raise ValidationError("readiness dispatch node_id is invalid")
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
    ):
        raise ValidationError("readiness dispatch repository is invalid")
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        raise ValidationError("readiness dispatch issue_number is invalid")
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise ValidationError("readiness dispatch branch is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in branch):
        raise ValidationError("readiness dispatch branch is invalid")
    if not isinstance(head, str) or not _HEAD_RE.fullmatch(head):
        raise ValidationError("readiness dispatch head is invalid")
    return {
        "node_id": node_id,
        "repository": repository,
        "issue_number": issue_number,
        "branch": branch,
        "head": head,
    }


def _human_required(
    reason: str,
    *,
    selected_node: dict[str, Any] | None = None,
    plan_digest: str | None = None,
    session_hint: str | None = None,
    preflight_result: str | None = None,
    preflight_reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": "human_required",
        "reason": reason,
        "plan_digest": plan_digest,
        "selected_node": selected_node,
    }
    if session_hint:
        result["session_hint"] = session_hint
    if preflight_result:
        result["resource_preflight_result"] = preflight_result
    if preflight_reason:
        result["resource_preflight_reason"] = redact_absolute_paths(
            preflight_reason
        )[:300]
    return result


def _execution_human_required(
    reason: str,
    *,
    selected_node: dict[str, Any] | None = None,
    plan_digest: str | None = None,
    authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _human_required(
        reason,
        selected_node=selected_node,
        plan_digest=plan_digest,
    )
    result.update(
        {
            "schema_version": _EXECUTION_RESULT_SCHEMA_VERSION,
            "kind": "provider_neutral_execution_result",
            "result": "HUMAN_REQUIRED",
        }
    )
    if authorization is not None:
        result["authorization"] = authorization
    return result


def _execution_authorization_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def activate_and_authorize_single_worker_handoff(
    *,
    graph_path: Path,
    workstream: str,
    worktree_path: str,
    packet_adapter: GitHubWorkPacketAdapter,
    implementer_profile: str = _CHATGPT_CHAT,
) -> dict[str, Any]:
    """Activate one packet and authorize an external provider-neutral handoff.

    This path never launches a process/session and accepts no dispatcher.
    CHATGPT_CHAT is represented as a resumable external handoff, not as a
    claim that Atlas can spawn or control a ChatGPT conversation.
    """
    expected_workstream = (workstream or "").strip()
    if not WORKSTREAM_RE.fullmatch(expected_workstream):
        raise ValidationError("readiness handoff workstream is invalid")
    target_worktree_raw = (worktree_path or "").strip()
    if not target_worktree_raw:
        raise ValidationError("readiness handoff worktree_path is required")
    resolved_worktree = Path(target_worktree_raw).resolve()
    if not resolved_worktree.is_dir():
        raise ValidationError(
            "readiness handoff worktree_path is not a directory"
        )
    target_worktree = str(resolved_worktree)
    if not isinstance(packet_adapter, GitHubWorkPacketAdapter):
        raise ValidationError(
            "readiness handoff requires GitHubWorkPacketAdapter"
        )
    requested_profile = (implementer_profile or "").strip()
    if not _EXECUTION_PROFILE_RE.fullmatch(requested_profile):
        raise ValidationError("readiness handoff implementer profile is invalid")
    if requested_profile != _CHATGPT_CHAT:
        return _execution_human_required("EXECUTION_ADAPTER_NOT_CONFIGURED")

    try:
        activation = packet_adapter.activate_authorized_readiness_packet(
            Path(graph_path)
        )
    except ValidationError:
        return _execution_human_required("ACTIVATION_FAILED")

    if not isinstance(activation, dict):
        return _execution_human_required("ACTIVATION_RESULT_INVALID")
    action = activation.get("action")
    if action == "denied":
        authorization = activation.get("authorization")
        if not isinstance(authorization, dict):
            return _execution_human_required("ACTIVATION_RESULT_INVALID")
        return _execution_human_required(
            "ACTIVATION_DENIED",
            authorization=authorization,
        )
    if action != "activated":
        return _execution_human_required("ACTIVATION_RESULT_INVALID")

    plan_digest = activation.get("plan_digest")
    if (
        not isinstance(plan_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", plan_digest)
    ):
        return _execution_human_required("ACTIVATION_RESULT_INVALID")
    try:
        selected = _selected_node(activation.get("selected_node"))
    except ValidationError:
        return _execution_human_required(
            "ACTIVATION_RESULT_INVALID",
            plan_digest=plan_digest,
        )

    try:
        fresh = packet_adapter.reread_trusted_active_execution_packet(
            selected["repository"],
            selected["issue_number"],
        )
    except ValidationError:
        return _execution_human_required(
            "ACTIVE_EXECUTION_PACKET_RECHECK_FAILED",
            selected_node=selected,
            plan_digest=plan_digest,
        )
    if not isinstance(fresh, dict):
        return _execution_human_required(
            "ACTIVE_EXECUTION_PACKET_RECHECK_FAILED",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    expected_active = {
        "repository": selected["repository"],
        "issue_number": selected["issue_number"],
        "branch": selected["branch"],
        "workstream": expected_workstream,
        "head": selected["head"],
        "status": "ACTIVE",
        "queue_state": "NONE",
    }
    if any(fresh.get(key) != value for key, value in expected_active.items()):
        return _execution_human_required(
            "ACTIVE_PACKET_DRIFT",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    try:
        packet_adapter.require_unique_active_readiness_packet(
            selected["repository"],
            issue_number=selected["issue_number"],
            branch=selected["branch"],
        )
    except ValidationError:
        return _execution_human_required(
            "ACTIVE_PACKET_UNIQUENESS_FAILED",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    if fresh.get("implementer") != requested_profile:
        return _execution_human_required(
            "IMPLEMENTER_PROFILE_MISMATCH",
            selected_node=selected,
            plan_digest=plan_digest,
        )
    change_risk = fresh.get("change_risk")
    intent_revision = fresh.get("intent_revision")
    author_permission = fresh.get("author_permission")
    if (
        change_risk not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        or isinstance(intent_revision, bool)
        or not isinstance(intent_revision, int)
        or intent_revision < 1
        or author_permission not in {"write", "maintain", "admin"}
    ):
        return _execution_human_required(
            "EXECUTION_PACKET_METADATA_INVALID",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    resume_payload = {
        "repository": selected["repository"],
        "issue_number": selected["issue_number"],
        "workstream": expected_workstream,
        "worktree_path": target_worktree,
        "branch": selected["branch"],
        "head": selected["head"],
        "intent_revision": intent_revision,
        "change_risk": change_risk,
        "implementer": requested_profile,
        "author_permission": author_permission,
    }
    authorization_basis = {
        "schema_version": 1,
        "kind": "provider_neutral_execution_authorization",
        "plan_digest": plan_digest,
        "execution_profile": requested_profile,
        "provider_attribution": "CHATGPT",
        "adapter": "EXTERNAL_HANDOFF",
        "resume_payload": resume_payload,
    }
    authorization_digest = _execution_authorization_digest(
        authorization_basis
    )

    return {
        "schema_version": _EXECUTION_RESULT_SCHEMA_VERSION,
        "kind": "provider_neutral_execution_result",
        "result": "AUTHORIZED_HANDOFF",
        "action": "authorized_handoff",
        "plan_digest": plan_digest,
        "authorization_digest": authorization_digest,
        "selected_node": selected,
        "execution_profile": requested_profile,
        "provider_attribution": "CHATGPT",
        "adapter": "EXTERNAL_HANDOFF",
        "spawned": False,
        "resume_payload": {
            **resume_payload,
            "authorization_digest": authorization_digest,
        },
    }


def activate_and_dispatch_single_worker(
    *,
    graph_path: Path,
    workstream: str,
    worktree_path: str,
    packet_adapter: GitHubWorkPacketAdapter,
    dispatcher: PtyPersistCursorDispatcher,
) -> dict[str, Any]:
    """Activate one graph-selected packet and dispatch exactly once.

    There is intentionally no automatic retry.  The canonical packet becomes
    ACTIVE before dispatch; any subsequent failure therefore remains visible
    and cannot replay the PAUSED+QUEUED authorization.
    """
    expected_workstream = (workstream or "").strip()
    if not WORKSTREAM_RE.fullmatch(expected_workstream):
        raise ValidationError("readiness dispatch workstream is invalid")
    target_worktree = (worktree_path or "").strip()
    if not target_worktree:
        raise ValidationError("readiness dispatch worktree_path is required")
    resolved_worktree = Path(target_worktree).resolve()
    if not resolved_worktree.is_dir():
        raise ValidationError("readiness dispatch worktree_path is not a directory")
    target_worktree = str(resolved_worktree)
    if not isinstance(packet_adapter, GitHubWorkPacketAdapter):
        raise ValidationError(
            "readiness dispatch requires GitHubWorkPacketAdapter"
        )
    if not isinstance(dispatcher, PtyPersistCursorDispatcher):
        raise ValidationError(
            "readiness dispatch requires PtyPersistCursorDispatcher"
        )

    try:
        activation = packet_adapter.activate_authorized_readiness_packet(
            Path(graph_path)
        )
    except ValidationError:
        return _human_required("ACTIVATION_FAILED")

    if not isinstance(activation, dict):
        return _human_required("ACTIVATION_RESULT_INVALID")
    action = activation.get("action")
    if action == "denied":
        authorization = activation.get("authorization")
        if not isinstance(authorization, dict):
            return _human_required("ACTIVATION_RESULT_INVALID")
        return {
            "action": "denied",
            "authorization": authorization,
        }
    if action != "activated":
        return _human_required("ACTIVATION_RESULT_INVALID")

    plan_digest = activation.get("plan_digest")
    if (
        not isinstance(plan_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", plan_digest)
    ):
        return _human_required("ACTIVATION_RESULT_INVALID")
    try:
        selected = _selected_node(activation.get("selected_node"))
    except ValidationError:
        return _human_required(
            "ACTIVATION_RESULT_INVALID",
            plan_digest=plan_digest,
        )

    try:
        fresh = packet_adapter.reread_trusted_active_execution_packet(
            selected["repository"],
            selected["issue_number"],
        )
    except ValidationError:
        return _human_required(
            "ACTIVE_PACKET_RECHECK_FAILED",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    expected_active = {
        "repository": selected["repository"],
        "issue_number": selected["issue_number"],
        "branch": selected["branch"],
        "workstream": expected_workstream,
        "head": selected["head"],
        "status": "ACTIVE",
        "queue_state": "NONE",
    }
    if not isinstance(fresh, dict) or any(
        fresh.get(key) != value for key, value in expected_active.items()
    ):
        return _human_required(
            "ACTIVE_PACKET_DRIFT",
            selected_node=selected,
            plan_digest=plan_digest,
        )
    if fresh.get("implementer") != "CURSOR":
        return _human_required(
            "IMPLEMENTER_PROFILE_MISMATCH",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    try:
        packet_adapter.require_unique_active_readiness_packet(
            selected["repository"],
            issue_number=selected["issue_number"],
            branch=selected["branch"],
        )
    except ValidationError:
        return _human_required(
            "ACTIVE_PACKET_UNIQUENESS_FAILED",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    request = DispatchRequest(
        workstream=expected_workstream,
        worktree_path=target_worktree,
        branch=selected["branch"],
        issue_number=selected["issue_number"],
        attempt=1,
        repository=selected["repository"],
        expected_head=selected["head"],
        resume_prompt=RESUME_PROMPT,
    )
    try:
        dispatched = dispatcher.start_resume(request)
    except ResourcePreflightBlocked as exc:
        return _human_required(
            "RESOURCE_PREFLIGHT_BLOCKED",
            selected_node=selected,
            plan_digest=plan_digest,
            preflight_result=exc.preflight_result,
            preflight_reason=exc.preflight_reason,
        )
    except DispatchSpawnCleanupUncertainError as exc:
        return _human_required(
            "SPAWN_CLEANUP_UNCERTAIN",
            selected_node=selected,
            plan_digest=plan_digest,
            session_hint=exc.session_hint,
        )
    except DispatchSpawnedButUnobservedError as exc:
        return _human_required(
            "SPAWNED_BUT_UNOBSERVED",
            selected_node=selected,
            plan_digest=plan_digest,
            session_hint=exc.session_hint,
        )
    except ValidationError:
        return _human_required(
            "DISPATCH_BLOCKED",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    if not isinstance(dispatched, DispatchResult) or not dispatched.session_id.strip():
        return _human_required(
            "DISPATCH_RESULT_INVALID",
            selected_node=selected,
            plan_digest=plan_digest,
        )

    result: dict[str, Any] = {
        "schema_version": _EXECUTION_RESULT_SCHEMA_VERSION,
        "kind": "provider_neutral_execution_result",
        "result": "DISPATCHED",
        "action": "dispatched",
        "plan_digest": plan_digest,
        "selected_node": selected,
        "execution_profile": "CURSOR",
        "provider_attribution": "CURSOR",
        "adapter": "PTY_PERSIST_CURSOR",
        "spawned": True,
        "session_id": dispatched.session_id.strip(),
        "resume_prompt": RESUME_PROMPT,
    }
    if dispatched.resource_preflight_result:
        result["resource_preflight_result"] = dispatched.resource_preflight_result
        result["resource_preflight_reason"] = redact_absolute_paths(
            dispatched.resource_preflight_reason or ""
        )[:300]
    return result
