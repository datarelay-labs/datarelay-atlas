"""Bounded Decision Plane canary admission over verified replay evidence.

Admission is a derived, non-executing gate. It never activates a model choice,
changes terminal deterministic gates, grants permissions, or promotes rollout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.decision_plane import DECISION_CLASSES, decision_canary_readiness
from atlas.provenance import PROJECT_ID_RE, ValidationError, validate_source_path

SCHEMA_VERSION = 1
REQUEST_KIND = "decision_plane_canary_request"
ADMISSION_KIND = "decision_plane_canary_admission"
FILENAME = "decision-plane-canary.json"
AUTHORITY = "CANARY_ADMISSION_ONLY"
ACTIVATION_AUTHORITY = "NO_ACTIVATION_AUTHORITY"
EXECUTION_AUTHORITY = "NONE"
FALLBACK = "CURRENT_DECISION"
DECISIONS = frozenset({"CANARY_ELIGIBLE", "CANARY_NOT_ELIGIBLE"})
_MAX_BYTES = 256 * 1024
_MAX_PATH_PREFIXES = 32
_MAX_TASK_KINDS = 16
_MAX_CANARY_DECISIONS = 100
_MAX_HORIZON = timedelta(days=7)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _reject(f"decision plane canary {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _reject(f"decision plane canary {label} is invalid")
    return value


def _timestamp(value: object, *, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        _reject(f"decision plane canary {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"decision plane canary {label} must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"decision plane canary {label} must be UTC")
    return value, parsed


def _scope(raw: object) -> dict[str, object]:
    expected = {"project_id", "path_prefixes", "task_kinds"}
    if not isinstance(raw, dict) or set(raw) != expected:
        _reject("decision plane canary scope is invalid")
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        _reject("decision plane canary project_id is invalid")

    prefixes = raw.get("path_prefixes")
    if not isinstance(prefixes, list) or not prefixes or len(prefixes) > _MAX_PATH_PREFIXES:
        _reject("decision plane canary path_prefixes must contain 1-32 paths")
    normalized_prefixes: list[str] = []
    for value in prefixes:
        if not isinstance(value, str):
            _reject("decision plane canary path_prefix is invalid")
        normalized = value.rstrip("/")
        try:
            validate_source_path(normalized)
        except ValidationError as exc:
            raise ValidationError("decision plane canary path_prefix is invalid") from exc
        normalized_prefixes.append(normalized)
    if len(normalized_prefixes) != len(set(normalized_prefixes)):
        _reject("decision plane canary path_prefixes must be unique")

    task_kinds = raw.get("task_kinds")
    if not isinstance(task_kinds, list) or not task_kinds or len(task_kinds) > _MAX_TASK_KINDS:
        _reject("decision plane canary task_kinds must contain 1-16 values")
    normalized_task_kinds = [_identity(value, label="task_kind") for value in task_kinds]
    if len(normalized_task_kinds) != len(set(normalized_task_kinds)):
        _reject("decision plane canary task_kinds must be unique")

    return {
        "project_id": project_id,
        "path_prefixes": sorted(normalized_prefixes),
        "task_kinds": sorted(normalized_task_kinds),
    }


def validate_decision_canary_request(payload: object) -> dict[str, Any]:
    expected = {
        "schema_version", "kind", "request_id", "decision_class",
        "expected_evidence_digest", "scope", "max_canary_decisions",
        "evaluated_at", "expires_at",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("decision plane canary request schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != REQUEST_KIND
    ):
        _reject("decision plane canary request version/kind is invalid")
    decision_class = payload.get("decision_class")
    if decision_class not in DECISION_CLASSES:
        _reject("decision plane canary decision_class is unsupported")
    max_decisions = payload.get("max_canary_decisions")
    if (
        isinstance(max_decisions, bool)
        or not isinstance(max_decisions, int)
        or not 1 <= max_decisions <= _MAX_CANARY_DECISIONS
    ):
        _reject("decision plane canary max_canary_decisions must be 1-100")
    evaluated_at, evaluated = _timestamp(payload.get("evaluated_at"), label="evaluated_at")
    expires_at, expires = _timestamp(payload.get("expires_at"), label="expires_at")
    if expires - evaluated > _MAX_HORIZON:
        _reject("decision plane canary expiry horizon exceeds 7 days")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "request_id": _identity(payload.get("request_id"), label="request_id"),
        "decision_class": decision_class,
        "expected_evidence_digest": _digest(
            payload.get("expected_evidence_digest"), label="expected_evidence_digest"
        ),
        "scope": _scope(payload.get("scope")),
        "max_canary_decisions": max_decisions,
        "evaluated_at": evaluated_at,
        "expires_at": expires_at,
    }
    assert_content_free(normalized)
    return normalized


def build_decision_canary_admission(data_root: Path, request: object) -> dict[str, object]:
    normalized = validate_decision_canary_request(request)
    readiness = decision_canary_readiness(Path(data_root))
    classes = {str(item["decision_class"]): item for item in readiness["classes"]}
    class_readiness = classes[str(normalized["decision_class"])]
    _, evaluated = _timestamp(normalized["evaluated_at"], label="evaluated_at")
    _, expires = _timestamp(normalized["expires_at"], label="expires_at")
    reasons: list[str] = []
    current_digest = str(readiness["evidence_digest"])
    if normalized["expected_evidence_digest"] != current_digest:
        reasons.append("EVIDENCE_DIGEST_MISMATCH")
    if class_readiness["replay_assessment"] != "REPLAY_PASS":
        reasons.append("REPLAY_NOT_PASS")
    if int(class_readiness["false_routing_count"]) != 0:
        reasons.append("FALSE_ROUTING_OBSERVED")
    if expires <= evaluated:
        reasons.append("REQUEST_EXPIRED")
    decision = "CANARY_ELIGIBLE" if not reasons else "CANARY_NOT_ELIGIBLE"
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": ADMISSION_KIND,
        "request_id": normalized["request_id"],
        "decision_class": normalized["decision_class"],
        "decision": decision,
        "reasons": reasons,
        "authority": AUTHORITY,
        "activation_authority": ACTIVATION_AUTHORITY,
        "execution_authority": EXECUTION_AUTHORITY,
        "fallback": FALLBACK,
        "current_rollout_state": "SHADOW",
        "requested_rollout_state": "CANARY",
        "scope": normalized["scope"],
        "max_canary_decisions": normalized["max_canary_decisions"],
        "evaluated_at": normalized["evaluated_at"],
        "expires_at": normalized["expires_at"],
        "expected_evidence_digest": normalized["expected_evidence_digest"],
        "current_evidence_digest": current_digest,
        "replay_assessment": class_readiness["replay_assessment"],
        "verified_replay_count": class_readiness["verified_replay_count"],
        "false_routing_count": class_readiness["false_routing_count"],
    }
    assert_content_free(result)
    return result


def validate_decision_canary_admission(payload: object) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "request_id", "decision_class", "decision",
        "reasons", "authority", "activation_authority", "execution_authority",
        "fallback", "current_rollout_state", "requested_rollout_state", "scope",
        "max_canary_decisions", "evaluated_at", "expires_at",
        "expected_evidence_digest", "current_evidence_digest", "replay_assessment",
        "verified_replay_count", "false_routing_count",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("decision plane canary admission schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != ADMISSION_KIND
        or payload.get("decision") not in DECISIONS
        or payload.get("authority") != AUTHORITY
        or payload.get("activation_authority") != ACTIVATION_AUTHORITY
        or payload.get("execution_authority") != EXECUTION_AUTHORITY
        or payload.get("fallback") != FALLBACK
        or payload.get("current_rollout_state") != "SHADOW"
        or payload.get("requested_rollout_state") != "CANARY"
        or payload.get("decision_class") not in DECISION_CLASSES
    ):
        _reject("decision plane canary admission authority/state is invalid")
    reasons = payload.get("reasons")
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) or _ID_RE.fullmatch(item) is None for item in reasons
    ):
        _reject("decision plane canary admission reasons are invalid")
    max_decisions = payload.get("max_canary_decisions")
    if (
        isinstance(max_decisions, bool)
        or not isinstance(max_decisions, int)
        or not 1 <= max_decisions <= _MAX_CANARY_DECISIONS
    ):
        _reject("decision plane canary admission max decisions is invalid")
    for name in ("verified_replay_count", "false_routing_count"):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _reject(f"decision plane canary admission {name} is invalid")
    _identity(payload.get("request_id"), label="request_id")
    _scope(payload.get("scope"))
    _digest(payload.get("expected_evidence_digest"), label="expected_evidence_digest")
    _digest(payload.get("current_evidence_digest"), label="current_evidence_digest")
    evaluated_at, evaluated = _timestamp(payload.get("evaluated_at"), label="evaluated_at")
    expires_at, expires = _timestamp(payload.get("expires_at"), label="expires_at")
    if expires - evaluated > _MAX_HORIZON:
        _reject("decision plane canary admission expiry horizon exceeds 7 days")
    replay = payload.get("replay_assessment")
    if replay not in {"REPLAY_PASS", "REPLAY_FAIL", "INSUFFICIENT_EVIDENCE"}:
        _reject("decision plane canary admission replay_assessment is invalid")

    expected_reasons: list[str] = []
    if payload["expected_evidence_digest"] != payload["current_evidence_digest"]:
        expected_reasons.append("EVIDENCE_DIGEST_MISMATCH")
    if replay != "REPLAY_PASS":
        expected_reasons.append("REPLAY_NOT_PASS")
    if int(payload["false_routing_count"]) != 0:
        expected_reasons.append("FALSE_ROUTING_OBSERVED")
    if expires <= evaluated:
        expected_reasons.append("REQUEST_EXPIRED")
    expected_decision = (
        "CANARY_ELIGIBLE" if not expected_reasons else "CANARY_NOT_ELIGIBLE"
    )
    if list(reasons) != expected_reasons or payload["decision"] != expected_decision:
        _reject("decision plane canary admission derived decision mismatch")

    normalized = dict(payload)
    normalized["evaluated_at"] = evaluated_at
    normalized["expires_at"] = expires_at
    normalized["scope"] = _scope(payload["scope"])
    normalized["reasons"] = list(reasons)
    assert_content_free(normalized)
    return normalized


def publish_decision_canary_admission(data_root: Path, request: object) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("decision plane canary data root is not a directory")
    admission = build_decision_canary_admission(root, request)
    text = json.dumps(admission, indent=2, sort_keys=True) + "\n"
    with data_root_write_lock(root):
        atomic_write_text(root / FILENAME, text)
    return decision_canary_dashboard(root)


def _load_admission(path: Path) -> dict[str, object] | None:
    if path.is_symlink():
        _reject("decision plane canary snapshot path is unsafe")
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        _reject("decision plane canary snapshot path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("decision plane canary snapshot is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject("decision plane canary snapshot exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("decision plane canary snapshot is invalid JSON") from exc
    return validate_decision_canary_admission(payload)


def decision_canary_dashboard(data_root: Path) -> dict[str, object]:
    root = Path(data_root)
    admission = _load_admission(root / FILENAME)
    if admission is None:
        return {
            "state": "UNKNOWN",
            "detail": "no Decision Plane canary admission snapshot loaded",
            "snapshot_path": FILENAME,
            "authority": AUTHORITY,
            "activation_authority": ACTIVATION_AUTHORITY,
            "execution_authority": EXECUTION_AUTHORITY,
            "binding_state": "UNKNOWN",
            "effective_decision": "CANARY_NOT_ELIGIBLE",
            "admission": None,
        }
    readiness = decision_canary_readiness(root)
    current_digest = str(readiness["evidence_digest"])
    current_class = next(
        item
        for item in readiness["classes"]
        if item["decision_class"] == admission["decision_class"]
    )
    current = (
        admission["current_evidence_digest"] == current_digest
        and admission["expected_evidence_digest"] == current_digest
        and admission["replay_assessment"] == current_class["replay_assessment"]
        and admission["verified_replay_count"] == current_class["verified_replay_count"]
        and admission["false_routing_count"] == current_class["false_routing_count"]
    )
    effective = admission["decision"] if current else "CANARY_NOT_ELIGIBLE"
    detail = (
        "admission is bound to the current replay evidence; no activation authority"
        if current
        else "replay evidence changed after admission publication; admission is stale"
    )
    return {
        "state": "OBSERVED",
        "detail": detail,
        "snapshot_path": FILENAME,
        "authority": AUTHORITY,
        "activation_authority": ACTIVATION_AUTHORITY,
        "execution_authority": EXECUTION_AUTHORITY,
        "binding_state": "CURRENT" if current else "STALE",
        "effective_decision": effective,
        "admission": admission,
    }
