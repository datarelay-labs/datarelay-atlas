"""Exact-plan authorization for bounded provider-neutral multi-node dispatch.

This module authorizes a set of already-admitted assignments. It never spawns a
worker, mutates GitHub, invokes a provider, retries an effect, or declares PASS.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from atlas.concurrency_admission import (
    SNAPSHOT_FILENAME,
    plan_concurrency_admission,
    validate_concurrency_snapshot,
)
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
REQUEST_KIND = "concurrency_dispatch_authorization_request"
AUTHORIZATION_KIND = "concurrency_dispatch_authorization"
FILENAME = "concurrency-dispatch-authorization.json"
AUTHORITY = "DISPATCH_AUTHORIZATION_ONLY"
DISPATCH_EFFECT_AUTHORITY = "NONE"
_MIN_ASSIGNMENTS = 2
_MAX_BYTES = 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#\-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("concurrency authorization is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _reject(f"concurrency authorization {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency authorization {label} is invalid")
    return value


def _utc(value: object, *, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject(f"concurrency authorization {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"concurrency authorization {label} must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"concurrency authorization {label} must be UTC")
    return value, parsed


def _load_json(path: Path, *, label: str) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        _reject(f"concurrency authorization {label} path is unsafe")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"concurrency authorization {label} is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject(f"concurrency authorization {label} exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"concurrency authorization {label} is invalid JSON") from exc
    assert_content_free(payload)
    return payload


def validate_concurrency_authorization_request(payload: object) -> dict[str, str]:
    expected = {
        "schema_version",
        "kind",
        "authorization_id",
        "expected_plan_digest",
        "evaluated_at",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency authorization request schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != REQUEST_KIND
    ):
        _reject("concurrency authorization request version/kind is invalid")
    evaluated_at, _ = _utc(payload.get("evaluated_at"), label="evaluated_at")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "authorization_id": _identity(
            payload.get("authorization_id"), label="authorization_id"
        ),
        "expected_plan_digest": _digest(
            payload.get("expected_plan_digest"), label="expected_plan_digest"
        ),
        "evaluated_at": evaluated_at,
    }
    assert_content_free(normalized)
    return normalized


def _load_snapshot(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / SNAPSHOT_FILENAME
    if not path.exists():
        _reject("concurrency authorization snapshot is not loaded")
    return validate_concurrency_snapshot(_load_json(path, label="admission snapshot"))


def _revalidate_assignments(
    snapshot: dict[str, object],
    plan: dict[str, object],
    *,
    evaluated_at: datetime,
) -> list[dict[str, object]]:
    if plan.get("graph_state") != "READY":
        _reject("concurrency authorization requires READY graph state")
    assignments = plan.get("assignments")
    blocked = plan.get("blocked_selected_nodes")
    selected = plan.get("graph_selected_node_ids")
    if (
        not isinstance(assignments, list)
        or not isinstance(blocked, list)
        or not isinstance(selected, list)
    ):
        _reject("concurrency authorization plan shape is invalid")
    if blocked:
        _reject("concurrency authorization rejects blocked selected nodes")
    if len(assignments) < _MIN_ASSIGNMENTS:
        _reject("concurrency authorization requires at least two assignments")
    if len(assignments) != len(selected):
        _reject("concurrency authorization requires every selected node assigned")
    if len(assignments) > int(plan["max_parallel_admission"]):
        _reject("concurrency authorization exceeds max parallel admission")
    if int(plan["active_count"]) + len(assignments) > int(plan["global_max_wip"]):
        _reject("concurrency authorization exceeds global max WIP")

    node_ids = [str(item["node_id"]) for item in assignments]
    slot_ids = [str(item["slot_id"]) for item in assignments]
    worker_ids = [str(item["worker_id"]) for item in assignments]
    if len(node_ids) != len(set(node_ids)) or set(node_ids) != set(str(x) for x in selected):
        _reject("concurrency authorization assignment nodes are invalid")
    if len(slot_ids) != len(set(slot_ids)):
        _reject("concurrency authorization assignment slots must be unique")
    if len(worker_ids) != len(set(worker_ids)):
        _reject("concurrency authorization assignment workers must be unique")

    max_age = timedelta(
        seconds=int(snapshot["policy"]["max_slot_evidence_age_seconds"])
    )
    _, snapshot_time = _utc(snapshot["observed_at"], label="snapshot observed_at")
    if snapshot_time > evaluated_at:
        _reject("concurrency authorization snapshot is from the future")
    if evaluated_at - snapshot_time > max_age:
        _reject("concurrency authorization snapshot evidence is stale")

    slot_by_id = {
        str(slot["slot_id"]): slot for slot in snapshot["execution_slots"]
    }
    ineligible = {
        str(item["slot_id"]) for item in plan.get("ineligible_slots", [])
    }
    for assignment in assignments:
        slot_id = str(assignment["slot_id"])
        slot = slot_by_id.get(slot_id)
        if slot is None or slot_id in ineligible:
            _reject("concurrency authorization assignment uses an ineligible slot")
        _, slot_time = _utc(slot["observed_at"], label="slot observed_at")
        if slot_time > evaluated_at:
            _reject("concurrency authorization slot evidence is from the future")
        if evaluated_at - slot_time > max_age:
            _reject("concurrency authorization slot evidence is stale")
        if (
            assignment["worker_id"] != slot["worker_id"]
            or assignment["provider"] != slot["provider"]
            or assignment["runtime"] != slot["runtime"]
            or assignment["route_id"] != slot["route_id"]
            or assignment["slot_evidence_ref"] != slot["evidence_ref"]
        ):
            _reject("concurrency authorization slot attribution drifted")

    limits = {
        str(item["repository"]): int(item["max_wip"])
        for item in snapshot["policy"]["project_limits"]
    }
    active_by_repo = {repo: 0 for repo in limits}
    resources_by_node: dict[str, set[str]] = {}
    for node in snapshot["graph"]["nodes"]:
        repository = str(node["repository"])
        if node["packet_status"] == "ACTIVE":
            active_by_repo[repository] += 1
        resources_by_node[str(node["node_id"])] = set(
            str(value) for value in node["resources"]
        )
    admitted_by_repo = {repo: 0 for repo in limits}
    for assignment in assignments:
        repository = str(assignment["repository"])
        admitted_by_repo[repository] += 1
    for repository, limit in limits.items():
        if active_by_repo[repository] + admitted_by_repo[repository] > limit:
            _reject("concurrency authorization exceeds project WIP limit")

    for index, node_id in enumerate(node_ids):
        for other in node_ids[index + 1 :]:
            if resources_by_node[node_id] & resources_by_node[other]:
                _reject("concurrency authorization assignments have a resource conflict")
    return [dict(item) for item in assignments]


def build_concurrency_dispatch_authorization(
    data_root: Path,
    request: object,
) -> dict[str, object]:
    normalized_request = validate_concurrency_authorization_request(request)
    snapshot = _load_snapshot(Path(data_root))
    plan = plan_concurrency_admission(snapshot)
    expected = normalized_request["expected_plan_digest"]
    if plan["plan_digest"] != expected:
        _reject("concurrency authorization plan digest mismatch")
    _, evaluated = _utc(normalized_request["evaluated_at"], label="evaluated_at")
    assignments = _revalidate_assignments(
        snapshot,
        plan,
        evaluated_at=evaluated,
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": AUTHORIZATION_KIND,
        "authorization_id": normalized_request["authorization_id"],
        "authority": AUTHORITY,
        "dispatch_effect_authority": DISPATCH_EFFECT_AUTHORITY,
        "evaluated_at": normalized_request["evaluated_at"],
        "plan_digest": plan["plan_digest"],
        "plan_observed_at": plan["observed_at"],
        "graph_state": plan["graph_state"],
        "assignment_count": len(assignments),
        "assignments": assignments,
    }
    authorization_digest = _canonical_digest(body)
    result = {**body, "authorization_digest": authorization_digest}
    assert_content_free(result)
    return result




def _validate_assignment(value: object) -> dict[str, object]:
    expected = {
        "node_id", "repository", "issue_number", "branch", "head", "slot_id",
        "worker_id", "provider", "runtime", "route_id", "slot_evidence_ref",
    }
    if not isinstance(value, dict) or set(value) != expected:
        _reject("concurrency dispatch authorization assignment schema is invalid")
    repository = value.get("repository")
    issue_number = value.get("issue_number")
    head = value.get("head")
    provider = value.get("provider")
    if not isinstance(repository, str) or _REPO.fullmatch(repository) is None:
        _reject("concurrency dispatch authorization repository is invalid")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        _reject("concurrency dispatch authorization issue_number is invalid")
    if not isinstance(head, str) or _SHA40.fullmatch(head) is None:
        _reject("concurrency dispatch authorization head is invalid")
    if not isinstance(provider, str) or _PROVIDER.fullmatch(provider) is None:
        _reject("concurrency dispatch authorization provider is invalid")
    route_id = value.get("route_id")
    if route_id is not None:
        _identity(route_id, label="route_id")
    return {
        "node_id": _identity(value.get("node_id"), label="node_id"),
        "repository": repository,
        "issue_number": issue_number,
        "branch": _identity(value.get("branch"), label="branch"),
        "head": head,
        "slot_id": _identity(value.get("slot_id"), label="slot_id"),
        "worker_id": _identity(value.get("worker_id"), label="worker_id"),
        "provider": provider,
        "runtime": _identity(value.get("runtime"), label="runtime"),
        "route_id": route_id,
        "slot_evidence_ref": _identity(
            value.get("slot_evidence_ref"), label="slot_evidence_ref"
        ),
    }


def validate_concurrency_dispatch_authorization(payload: object) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "authorization_id", "authority",
        "dispatch_effect_authority", "evaluated_at", "plan_digest",
        "plan_observed_at", "graph_state", "assignment_count", "assignments",
        "authorization_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency dispatch authorization schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != AUTHORIZATION_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("dispatch_effect_authority") != DISPATCH_EFFECT_AUTHORITY
        or payload.get("graph_state") != "READY"
    ):
        _reject("concurrency dispatch authorization authority/state is invalid")
    _identity(payload.get("authorization_id"), label="authorization_id")
    _utc(payload.get("evaluated_at"), label="evaluated_at")
    _utc(payload.get("plan_observed_at"), label="plan_observed_at")
    _digest(payload.get("plan_digest"), label="plan_digest")
    assignments = payload.get("assignments")
    count = payload.get("assignment_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < _MIN_ASSIGNMENTS
        or not isinstance(assignments, list)
        or len(assignments) != count
    ):
        _reject("concurrency dispatch authorization assignments are invalid")
    normalized_assignments = [_validate_assignment(item) for item in assignments]
    node_ids = [str(item["node_id"]) for item in normalized_assignments]
    slot_ids = [str(item["slot_id"]) for item in normalized_assignments]
    worker_ids = [str(item["worker_id"]) for item in normalized_assignments]
    if (
        len(node_ids) != len(set(node_ids))
        or len(slot_ids) != len(set(slot_ids))
        or len(worker_ids) != len(set(worker_ids))
    ):
        _reject("concurrency dispatch authorization assignment identities must be unique")
    digest = _digest(
        payload.get("authorization_digest"), label="authorization_digest"
    )
    body = {key: value for key, value in payload.items() if key != "authorization_digest"}
    if _canonical_digest(body) != digest:
        _reject("concurrency dispatch authorization digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def publish_concurrency_dispatch_authorization(
    data_root: Path,
    request: object,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("concurrency authorization data root is not a directory")
    authorization = build_concurrency_dispatch_authorization(root, request)
    with data_root_write_lock(root):
        atomic_write_text(
            root / FILENAME,
            json.dumps(authorization, indent=2, sort_keys=True) + "\n",
        )
    return concurrency_dispatch_authorization_dashboard(root)


def _load_authorization(data_root: Path) -> dict[str, object] | None:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return None
    return validate_concurrency_dispatch_authorization(
        _load_json(path, label="dispatch authorization")
    )


def concurrency_dispatch_authorization_dashboard(
    data_root: Path,
) -> dict[str, object]:
    root = Path(data_root)
    authorization = _load_authorization(root)
    if authorization is None:
        return {
            "state": "UNKNOWN",
            "detail": "no concurrency dispatch authorization loaded",
            "authority": AUTHORITY,
            "dispatch_effect_authority": DISPATCH_EFFECT_AUTHORITY,
            "binding_state": "UNKNOWN",
            "authorization": None,
        }
    try:
        snapshot = _load_snapshot(root)
        plan = plan_concurrency_admission(snapshot)
    except ValidationError:
        return {
            "state": "OBSERVED",
            "detail": "authorization exists but current admission evidence is unavailable",
            "authority": AUTHORITY,
            "dispatch_effect_authority": DISPATCH_EFFECT_AUTHORITY,
            "binding_state": "STALE",
            "authorization": authorization,
        }
    current = (
        plan["plan_digest"] == authorization["plan_digest"]
        and plan["assignments"] == authorization["assignments"]
        and len(plan["assignments"]) == authorization["assignment_count"]
    )
    return {
        "state": "OBSERVED",
        "detail": (
            "authorization is bound to the current admission plan; no dispatch effect authority"
            if current
            else "admission plan changed after authorization; authorization is stale"
        ),
        "authority": AUTHORITY,
        "dispatch_effect_authority": DISPATCH_EFFECT_AUTHORITY,
        "binding_state": "CURRENT" if current else "STALE",
        "authorization": authorization,
    }
