"""Provider-neutral Decision Plane shadow/replay ledger.

The Decision Plane in this slice is observation-only. Model choices are validated
and measured but never replace the current deterministic execution choice.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from fnmatch import fnmatch
import json
from pathlib import Path
import re
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "decision_plane_shadow_ledger"
RECORD_KIND = "decision_plane_observation"
FILENAME = "decision-plane-shadow.json"
DECISION_CLASSES = frozenset({"OPTIONAL_CONTEXT_SELECTION", "FOCUSED_CHECK_SELECTION"})
MODES = frozenset({"SHADOW", "REPLAY"})
OUTCOMES = frozenset({"UNKNOWN", "VERIFIED_SUCCESS", "VERIFIED_FAILURE"})
_MAX_BYTES = 1024 * 1024
_MAX_RECORDS = 1000
_MAX_CANDIDATES = 128
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")

def _reject(message: str) -> None:
    raise ValidationError(message)


def _utc(value: object) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("decision plane timestamp must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("decision plane timestamp must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("decision plane timestamp must be UTC")
    return value


def _identity(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER if provider else _ID
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"decision plane {label} is invalid")
    return value


def _ids(value: object, *, label: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_CANDIDATES:
        _reject(f"decision plane {label} must be a bounded list")
    if not allow_empty and not value:
        _reject(f"decision plane {label} must not be empty")
    normalized = [_identity(item, label=label) for item in value]
    if len(normalized) != len(set(normalized)):
        _reject(f"decision plane {label} must be unique")
    return sorted(normalized)


def _signed_int(value: object, *, label: str, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -limit <= value <= limit:
        _reject(f"decision plane {label} is invalid")
    return value


def _confidence(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)", value):
        _reject("decision plane confidence must be a 0-1 decimal string")
    return value

def _repo_relative_file(root: Path, value: object) -> str:
    path = _identity(value, label="context candidate")
    relative = Path(path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValidationError("decision plane context candidate path is unsafe")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or candidate.is_symlink() or not candidate.is_file():
        raise ValidationError("decision plane context candidate is not a repository file")
    return relative.as_posix()


def build_optional_context_candidates(
    repo_root: Path,
    optional_paths: list[str],
) -> dict[str, object]:
    """Build a bounded optional-context candidate set with deterministic mandatory context."""
    root = Path(repo_root).resolve()
    required = []
    for path in ("AGENTS.md", ".engineering/project.yaml"):
        if (root / path).is_file() and not (root / path).is_symlink():
            required.append(path)
    if not required:
        raise ValidationError("decision plane mandatory repository context is unavailable")
    if not isinstance(optional_paths, list) or len(optional_paths) > _MAX_CANDIDATES:
        raise ValidationError("decision plane optional context candidate list is invalid")
    optional = [_repo_relative_file(root, item) for item in optional_paths]
    candidates = sorted(set([*required, *optional]))
    if len(candidates) > _MAX_CANDIDATES:
        raise ValidationError("decision plane context candidate limit exceeded")
    return {
        "decision_class": "OPTIONAL_CONTEXT_SELECTION",
        "candidate_ids": candidates,
        "required_candidate_ids": sorted(required),
        "terminal_required_ids": [],
        "authority": "CANDIDATE_PREPARATION_ONLY",
    }


def _load_tests_metadata(repo_root: Path) -> dict[str, Any]:
    if yaml is None:
        raise ValidationError("PyYAML is required for Decision Plane check candidates")
    path = Path(repo_root) / ".engineering" / "tests.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError("Decision Plane tests metadata is unavailable") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Decision Plane tests metadata is invalid")
    return payload


def build_focused_check_candidates(
    repo_root: Path,
    changed_paths: list[str],
) -> dict[str, object]:
    """Derive affected focused-check candidates while keeping terminal gates separate."""
    if not isinstance(changed_paths, list) or not changed_paths or len(changed_paths) > 256:
        raise ValidationError("decision plane changed_paths must contain 1-256 paths")
    normalized_paths = []
    for item in changed_paths:
        value = _identity(item, label="changed path")
        path = Path(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValidationError("decision plane changed path is unsafe")
        normalized_paths.append(path.as_posix())

    metadata = _load_tests_metadata(Path(repo_root))
    path_rules = metadata.get("paths")
    scenarios = metadata.get("scenarios")
    if not isinstance(path_rules, dict) or not isinstance(scenarios, list):
        raise ValidationError("Decision Plane tests metadata shape is invalid")

    affected_domains: set[str] = set()
    for changed in normalized_paths:
        for pattern, rule in path_rules.items():
            if not isinstance(pattern, str) or not isinstance(rule, dict):
                raise ValidationError("Decision Plane path-domain rule is invalid")
            domains = rule.get("domains")
            if not isinstance(domains, list) or any(not isinstance(item, str) for item in domains):
                raise ValidationError("Decision Plane path-domain rule is invalid")
            if fnmatch(changed, pattern):
                affected_domains.update(domains)

    candidates: list[dict[str, object]] = []
    for raw in scenarios:
        if not isinstance(raw, dict):
            raise ValidationError("Decision Plane scenario metadata is invalid")
        scenario_id = _identity(raw.get("id"), label="scenario id")
        domains = raw.get("domains")
        triggers = raw.get("triggers")
        level = raw.get("level")
        release_gate = raw.get("release_gate")
        if (
            not isinstance(domains, list)
            or any(not isinstance(item, str) for item in domains)
            or not isinstance(triggers, list)
            or any(not isinstance(item, str) for item in triggers)
            or not isinstance(level, str)
            or type(release_gate) is not bool
        ):
            raise ValidationError("Decision Plane scenario metadata is invalid")
        if "affected" not in triggers or not (set(domains) & affected_domains):
            continue
        candidates.append(
            {
                "id": scenario_id,
                "level": level,
                "domains": sorted(set(domains)),
                "release_gate": release_gate,
            }
        )
    candidates.sort(key=lambda item: str(item["id"]))
    if len(candidates) > _MAX_CANDIDATES:
        raise ValidationError("Decision Plane focused-check candidate limit exceeded")
    candidate_ids = [str(item["id"]) for item in candidates]
    return {
        "decision_class": "FOCUSED_CHECK_SELECTION",
        "changed_paths": sorted(set(normalized_paths)),
        "affected_domains": sorted(affected_domains),
        "candidate_ids": candidate_ids,
        "required_candidate_ids": [],
        "terminal_required_ids": [
            str(item["id"]) for item in candidates if item["release_gate"] is True
        ],
        "candidates": candidates,
        "authority": "CANDIDATE_PREPARATION_ONLY",
    }


def validate_decision_observation(payload: object) -> dict[str, Any]:
    keys = {
        "schema_version", "kind", "record_id", "mode", "decision_class",
        "observed_at", "candidate_ids", "required_candidate_ids",
        "current_choice_ids", "model_choice_ids", "model_provider",
        "model_name", "model_profile", "confidence", "current_outcome",
        "model_outcome", "cost_delta_milliunits", "wall_time_delta_ms",
        "frontier_call_delta", "retry_delta"
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        _reject("decision plane observation schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        _reject("decision plane observation schema_version is unsupported")
    if payload.get("kind") != RECORD_KIND:
        _reject("decision plane observation kind is invalid")

    record_id = _identity(payload.get("record_id"), label="record_id")
    mode = payload.get("mode")
    if mode not in MODES:
        _reject("decision plane mode is unsupported")
    decision_class = payload.get("decision_class")
    if decision_class not in DECISION_CLASSES:
        _reject("decision plane decision_class is unsupported")
    observed_at = _utc(payload.get("observed_at"))

    candidates = _ids(payload.get("candidate_ids"), label="candidate_ids")
    required = _ids(payload.get("required_candidate_ids"), label="required_candidate_ids", allow_empty=True)
    current = _ids(payload.get("current_choice_ids"), label="current_choice_ids")
    model = _ids(payload.get("model_choice_ids"), label="model_choice_ids", allow_empty=True)

    candidate_set = set(candidates)
    required_set = set(required)
    if not required_set.issubset(candidate_set):
        _reject("decision plane required candidates are outside candidate set")
    if not set(current).issubset(candidate_set) or not required_set.issubset(set(current)):
        _reject("decision plane current choice violates deterministic candidate constraints")

    reasons: list[str] = []
    model_set = set(model)
    if not model:
        reasons.append("MODEL_CHOICE_EMPTY")
    else:
        if not model_set.issubset(candidate_set):
            reasons.append("MODEL_CHOICE_OUTSIDE_CANDIDATES")
        if not required_set.issubset(model_set):
            reasons.append("MODEL_CHOICE_MISSING_REQUIRED")
    validation = "VALID" if not reasons else "INVALID"

    provider = _identity(payload.get("model_provider"), label="model_provider", provider=True)
    model_name = _identity(payload.get("model_name"), label="model_name")
    model_profile = _identity(payload.get("model_profile"), label="model_profile")
    confidence = _confidence(payload.get("confidence"))
    current_outcome = payload.get("current_outcome")
    model_outcome = payload.get("model_outcome")
    if current_outcome not in OUTCOMES or model_outcome not in OUTCOMES:
        _reject("decision plane outcome is invalid")
    if mode == "SHADOW" and model_outcome != "UNKNOWN":
        _reject("shadow observation cannot claim model execution outcome")
    if validation == "INVALID" and model_outcome != "UNKNOWN":
        _reject("invalid model choice cannot claim an execution outcome")

    cost_delta = _signed_int(payload.get("cost_delta_milliunits"), label="cost_delta_milliunits", limit=1_000_000_000)
    wall_delta = _signed_int(payload.get("wall_time_delta_ms"), label="wall_time_delta_ms", limit=86_400_000)
    frontier_delta = _signed_int(payload.get("frontier_call_delta"), label="frontier_call_delta", limit=1_000_000)
    retry_delta = _signed_int(payload.get("retry_delta"), label="retry_delta", limit=1_000_000)
    if mode == "SHADOW" and any(value != 0 for value in (cost_delta, wall_delta, frontier_delta, retry_delta)):
        _reject("shadow observation cannot claim replay cost/time deltas")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECORD_KIND,
        "record_id": record_id,
        "mode": mode,
        "decision_class": decision_class,
        "observed_at": observed_at,
        "candidate_ids": candidates,
        "required_candidate_ids": required,
        "current_choice_ids": current,
        "model_choice_ids": model,
        "model_provider": provider,
        "model_name": model_name,
        "model_profile": model_profile,
        "confidence": confidence,
        "validation": validation,
        "validation_reasons": reasons,
        "effective_choice_ids": current,
        "current_outcome": current_outcome,
        "model_outcome": model_outcome,
        "cost_delta_milliunits": cost_delta,
        "wall_time_delta_ms": wall_delta,
        "frontier_call_delta": frontier_delta,
        "retry_delta": retry_delta,
        "execution_authority": "SHADOW_ONLY",
    }
    assert_content_free(normalized)
    return normalized

def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "rollout_state": "SHADOW",
        "activation_authority": "NO_ACTIVATION_AUTHORITY",
        "records": [],
    }


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_ledger()
    if path.is_symlink() or not path.is_file():
        raise ValidationError("decision plane ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("decision plane ledger is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject("decision plane ledger exceeds bounded input size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("decision plane ledger is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "rollout_state", "activation_authority", "records"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("rollout_state") != "SHADOW"
        or payload.get("activation_authority") != "NO_ACTIVATION_AUTHORITY"
        or not isinstance(payload.get("records"), list)
        or len(payload["records"]) > _MAX_RECORDS
    ):
        _reject("decision plane ledger schema is invalid")

    records = [validate_decision_observation(_record_input(item)) for item in payload["records"]]
    ids = [item["record_id"] for item in records]
    if len(ids) != len(set(ids)):
        _reject("decision plane ledger record ids must be unique")
    return {**_empty_ledger(), "records": sorted(records, key=lambda item: (item["observed_at"], item["record_id"]))}


def _record_input(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject("decision plane observation schema is invalid")
    # Stored records contain derived validation/effective-choice fields; strip them
    # before revalidating the original observation semantics.
    derived = {"validation", "validation_reasons", "effective_choice_ids", "execution_authority"}
    return {key: item for key, item in value.items() if key not in derived}

def append_decision_observation(data_root: Path, observation: object) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("decision plane data root is not a directory")
    record = validate_decision_observation(observation)
    path = root / FILENAME
    with data_root_write_lock(root):
        ledger = _load_ledger(path)
        if len(ledger["records"]) >= _MAX_RECORDS:
            raise ValidationError("decision plane ledger record limit reached")
        if any(item["record_id"] == record["record_id"] for item in ledger["records"]):
            raise ValidationError("decision plane record_id already exists")
        ledger["records"].append(record)
        ledger["records"].sort(key=lambda item: (item["observed_at"], item["record_id"]))
        atomic_write_text(path, json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    return decision_plane_dashboard(root)


def _rate(successes: int, total: int) -> str | None:
    if total == 0:
        return None
    return f"{(successes * 10000) // total / 100:.2f}"


def _class_replay_assessment(
    decision_class: str,
    records: list[dict[str, Any]],
) -> dict[str, object]:
    verified = [
        item for item in records
        if item["decision_class"] == decision_class
        and item["mode"] == "REPLAY"
        and item["validation"] == "VALID"
        and item["current_outcome"] != "UNKNOWN"
        and item["model_outcome"] != "UNKNOWN"
    ]
    current_success = sum(item["current_outcome"] == "VERIFIED_SUCCESS" for item in verified)
    model_success = sum(item["model_outcome"] == "VERIFIED_SUCCESS" for item in verified)
    false_routing = sum(
        item["current_outcome"] == "VERIFIED_SUCCESS"
        and item["model_outcome"] == "VERIFIED_FAILURE"
        for item in verified
    )
    cost_delta = sum(item["cost_delta_milliunits"] for item in verified)
    wall_delta = sum(item["wall_time_delta_ms"] for item in verified)
    frontier_delta = sum(item["frontier_call_delta"] for item in verified)
    retry_delta = sum(item["retry_delta"] for item in verified)
    confidences = [
        Decimal(item["confidence"])
        for item in verified
        if item["confidence"] is not None
    ]
    enough = len(verified) >= 5
    noninferior = model_success >= current_success if verified else False
    improvement = cost_delta < 0 or wall_delta < 0 or frontier_delta < 0
    retry_safe = retry_delta <= 0
    assessment = (
        "INSUFFICIENT_EVIDENCE"
        if not enough
        else (
            "REPLAY_PASS"
            if noninferior and improvement and retry_safe
            else "REPLAY_FAIL"
        )
    )
    return {
        "decision_class": decision_class,
        "assessment": assessment,
        "verified_replay_count": len(verified),
        "current_success_count": current_success,
        "model_success_count": model_success,
        "current_success_rate_percent": _rate(current_success, len(verified)),
        "model_success_rate_percent": _rate(model_success, len(verified)),
        "false_routing_count": false_routing,
        "cost_delta_milliunits": cost_delta,
        "wall_time_delta_ms": wall_delta,
        "frontier_call_delta": frontier_delta,
        "retry_delta": retry_delta,
        "confidence_observation_count": len(confidences),
        "confidence_min": str(min(confidences)) if confidences else None,
        "confidence_max": str(max(confidences)) if confidences else None,
        "criteria": {
            "minimum_verified_replay_records": 5,
            "verified_success_noninferiority": noninferior,
            "cost_or_time_or_frontier_improvement": improvement,
            "retry_delta_nonpositive": retry_safe,
        },
    }


def decision_plane_dashboard(data_root: Path) -> dict[str, object]:
    ledger = _load_ledger(Path(data_root) / FILENAME)
    records = ledger["records"]
    replay = [item for item in records if item["mode"] == "REPLAY"]
    valid = [item for item in records if item["validation"] == "VALID"]
    verified_replay = [
        item for item in replay
        if item["validation"] == "VALID"
        and item["current_outcome"] != "UNKNOWN"
        and item["model_outcome"] != "UNKNOWN"
    ]
    current_success = sum(item["current_outcome"] == "VERIFIED_SUCCESS" for item in verified_replay)
    model_success = sum(item["model_outcome"] == "VERIFIED_SUCCESS" for item in verified_replay)
    assessments = {
        decision_class: _class_replay_assessment(decision_class, records)
        for decision_class in sorted(DECISION_CLASSES)
    }
    values = [item["assessment"] for item in assessments.values()]
    replay_gate = (
        "REPLAY_PASS"
        if "REPLAY_PASS" in values
        else (
            "REPLAY_FAIL"
            if "REPLAY_FAIL" in values
            else "INSUFFICIENT_EVIDENCE"
        )
    )
    return {
        "state": "OBSERVED" if records else "UNKNOWN",
        "rollout_state": "SHADOW",
        "activation_authority": "NO_ACTIVATION_AUTHORITY",
        "record_count": len(records),
        "valid_choice_count": len(valid),
        "invalid_choice_count": len(records) - len(valid),
        "shadow_count": sum(item["mode"] == "SHADOW" for item in records),
        "replay_count": len(replay),
        "verified_replay_count": len(verified_replay),
        "current_success_rate_percent": _rate(current_success, len(verified_replay)),
        "model_success_rate_percent": _rate(model_success, len(verified_replay)),
        "total_cost_delta_milliunits": sum(item["cost_delta_milliunits"] for item in verified_replay),
        "total_wall_time_delta_ms": sum(item["wall_time_delta_ms"] for item in verified_replay),
        "total_frontier_call_delta": sum(item["frontier_call_delta"] for item in verified_replay),
        "total_retry_delta": sum(item["retry_delta"] for item in verified_replay),
        "replay_gate": replay_gate,
        "class_assessments": assessments,
        "records": records,
    }
