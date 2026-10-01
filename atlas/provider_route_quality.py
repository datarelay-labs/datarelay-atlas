"""Verified provider-route outcome evidence and deterministic aggregation.

This is evidence only. It never changes provider eligibility, broker ranking,
selection, failover, or effect authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import REPO_RE, ValidationError

OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_KIND = "provider_route_outcome_observation"
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_KIND = "provider_route_quality_snapshot"
AUTHORITY = "EVIDENCE_ONLY"
BROKER_INFLUENCE = "NONE"
FILENAME = "provider-route-quality-snapshot.json"
_MAX_OBSERVATION_BYTES = 64 * 1024
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_MAX_OBSERVATIONS = 512
_MAX_COUNT = 1_000_000
_MAX_WALL_MS = 31_536_000_000
_MAX_COST_MILLIUNITS = 10**15
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#-]{0,255}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_RESULTS = frozenset({"PASS", "REWORK", "HUMAN_REQUIRED", "FAILED"})
_COST_STATES = frozenset({"UNKNOWN", "OBSERVED"})
_OBS_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "observation_id",
        "route",
        "work_packet",
        "outcome",
        "cost",
        "verification",
    }
)
_ROUTE_KEYS = frozenset({"route_id", "provider", "runtime", "usage_mode", "adapter"})
_PACKET_KEYS = frozenset({"repository", "issue_number", "subject_head", "task_kind"})
_OUTCOME_KEYS = frozenset(
    {
        "verified_result",
        "first_pass",
        "audit_finding_count",
        "rework_count",
        "owner_intervention_count",
        "wall_time_ms",
        "quota_interruption_count",
    }
)
_VERIFICATION_KEYS = frozenset({"source_ref", "source_digest", "observed_at"})


def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            _reject("provider route outcome contains a duplicate JSON key")
        out[key] = value
    return out


def _load_json(path: Path, *, label: str, max_bytes: int) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        _reject(f"{label} file is unsafe")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"{label} file is unreadable") from exc
    if len(raw) > max_bytes:
        _reject(f"{label} file exceeds bounded size")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(f"{label} contains a non-finite number"),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} file is invalid JSON") from exc
    assert_content_free(value)
    return value


def _id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _reject(f"{label} is invalid")
    return value


def _provider(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER_RE.fullmatch(value) is None:
        _reject("provider route outcome provider is invalid")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        _reject("provider route outcome observed_at must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("provider route outcome observed_at must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("provider route outcome observed_at must be UTC")
    return value


def _count(value: object, *, label: str, maximum: int = _MAX_COUNT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        _reject(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _reject(f"{label} is invalid")
    return value


def _cost(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        _reject("provider route outcome cost is invalid")
    state = raw.get("status")
    if state not in _COST_STATES:
        _reject("provider route outcome cost status is invalid")
    if state == "UNKNOWN":
        if set(raw) != {"status"}:
            _reject("UNKNOWN provider route cost cannot carry values")
        return {"status": "UNKNOWN"}
    if set(raw) != {"status", "milliunits", "source_ref", "source_digest"}:
        _reject("OBSERVED provider route cost is invalid")
    return {
        "status": "OBSERVED",
        "milliunits": _count(
            raw.get("milliunits"),
            label="provider route outcome cost milliunits",
            maximum=_MAX_COST_MILLIUNITS,
        ),
        "source_ref": _id(raw.get("source_ref"), label="provider route cost source_ref"),
        "source_digest": _digest(
            raw.get("source_digest"), label="provider route cost source_digest"
        ),
    }


def validate_provider_route_outcome(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _OBS_KEYS:
        _reject("provider route outcome schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != OBSERVATION_SCHEMA_VERSION
        or payload.get("kind") != OBSERVATION_KIND
    ):
        _reject("provider route outcome version/kind is invalid")

    route = payload.get("route")
    if not isinstance(route, dict) or set(route) != _ROUTE_KEYS:
        _reject("provider route outcome route identity is invalid")
    normalized_route = {
        "route_id": _id(route.get("route_id"), label="route_id"),
        "provider": _provider(route.get("provider")),
        "runtime": _id(route.get("runtime"), label="runtime"),
        "usage_mode": _id(route.get("usage_mode"), label="usage_mode"),
        "adapter": _id(route.get("adapter"), label="adapter"),
    }

    packet = payload.get("work_packet")
    if not isinstance(packet, dict) or set(packet) != _PACKET_KEYS:
        _reject("provider route outcome work packet is invalid")
    repository = packet.get("repository")
    if not isinstance(repository, str) or REPO_RE.fullmatch(repository) is None:
        _reject("provider route outcome repository is invalid")
    issue_number = packet.get("issue_number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or not 1 <= issue_number <= 2_147_483_647:
        _reject("provider route outcome issue_number is invalid")
    subject_head = packet.get("subject_head")
    if not isinstance(subject_head, str) or _HEAD_RE.fullmatch(subject_head) is None:
        _reject("provider route outcome subject_head is invalid")
    normalized_packet = {
        "repository": repository,
        "issue_number": issue_number,
        "subject_head": subject_head,
        "task_kind": _id(packet.get("task_kind"), label="task_kind"),
    }

    outcome = payload.get("outcome")
    if not isinstance(outcome, dict) or set(outcome) != _OUTCOME_KEYS:
        _reject("provider route outcome result is invalid")
    result = outcome.get("verified_result")
    first_pass = outcome.get("first_pass")
    if result not in _RESULTS or not isinstance(first_pass, bool):
        _reject("provider route outcome verified result is invalid")
    audit_findings = _count(outcome.get("audit_finding_count"), label="audit_finding_count")
    reworks = _count(outcome.get("rework_count"), label="rework_count")
    owner_interventions = _count(
        outcome.get("owner_intervention_count"), label="owner_intervention_count"
    )
    wall_ms = _count(outcome.get("wall_time_ms"), label="wall_time_ms", maximum=_MAX_WALL_MS)
    quota_interruptions = _count(
        outcome.get("quota_interruption_count"), label="quota_interruption_count"
    )
    if first_pass and (result != "PASS" or reworks != 0):
        _reject("provider route outcome first_pass is inconsistent")
    if result == "REWORK" and reworks < 1:
        _reject("REWORK provider route outcome requires rework_count")

    verification = payload.get("verification")
    if not isinstance(verification, dict) or set(verification) != _VERIFICATION_KEYS:
        _reject("provider route outcome verification is invalid")
    normalized_verification = {
        "source_ref": _id(
            verification.get("source_ref"), label="verification source_ref"
        ),
        "source_digest": _digest(
            verification.get("source_digest"), label="verification source_digest"
        ),
        "observed_at": _timestamp(verification.get("observed_at")),
    }

    normalized = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "kind": OBSERVATION_KIND,
        "observation_id": _id(payload.get("observation_id"), label="observation_id"),
        "route": normalized_route,
        "work_packet": normalized_packet,
        "outcome": {
            "verified_result": result,
            "first_pass": first_pass,
            "audit_finding_count": audit_findings,
            "rework_count": reworks,
            "owner_intervention_count": owner_interventions,
            "wall_time_ms": wall_ms,
            "quota_interruption_count": quota_interruptions,
        },
        "cost": _cost(payload.get("cost")),
        "verification": normalized_verification,
    }
    assert_content_free(normalized)
    return normalized


def _route_identity_key(route: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        route["route_id"],
        route["provider"],
        route["runtime"],
        route["usage_mode"],
        route["adapter"],
    )


def aggregate_provider_route_outcomes(
    observations: list[dict[str, Any]],
) -> list[dict[str, object]]:
    if not isinstance(observations, list) or len(observations) > _MAX_OBSERVATIONS:
        _reject("provider route outcome observations are invalid")
    normalized = [validate_provider_route_outcome(item) for item in observations]
    ids = [item["observation_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        _reject("provider route outcome observation_id values must be unique")
    evidence_keys = [
        (
            item["route"]["route_id"],
            item["work_packet"]["repository"],
            item["work_packet"]["issue_number"],
            item["work_packet"]["subject_head"],
            item["verification"]["source_digest"],
        )
        for item in normalized
    ]
    if len(evidence_keys) != len(set(evidence_keys)):
        _reject("provider route outcome verification evidence must be unique")

    route_by_id: dict[str, tuple[str, str, str, str, str]] = {}
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for item in normalized:
        key = _route_identity_key(item["route"])
        route_id = key[0]
        prior = route_by_id.get(route_id)
        if prior is not None and prior != key:
            _reject("provider route outcome route_id has conflicting identity")
        route_by_id[route_id] = key
        groups.setdefault(key, []).append(item)

    result: list[dict[str, object]] = []
    for key in sorted(groups):
        rows = groups[key]
        sample_count = len(rows)
        pass_count = sum(row["outcome"]["verified_result"] == "PASS" for row in rows)
        first_pass_count = sum(bool(row["outcome"]["first_pass"]) for row in rows)
        wall_total = sum(int(row["outcome"]["wall_time_ms"]) for row in rows)
        observed_costs = [
            int(row["cost"]["milliunits"])
            for row in rows
            if row["cost"]["status"] == "OBSERVED"
        ]
        result.append(
            {
                "route_id": key[0],
                "provider": key[1],
                "runtime": key[2],
                "usage_mode": key[3],
                "adapter": key[4],
                "sample_count": sample_count,
                "verified_pass_count": pass_count,
                "verified_pass_rate_basis_points": pass_count * 10_000 // sample_count,
                "first_pass_count": first_pass_count,
                "first_pass_rate_basis_points": first_pass_count * 10_000 // sample_count,
                "audit_finding_total": sum(
                    int(row["outcome"]["audit_finding_count"]) for row in rows
                ),
                "rework_total": sum(int(row["outcome"]["rework_count"]) for row in rows),
                "owner_intervention_total": sum(
                    int(row["outcome"]["owner_intervention_count"]) for row in rows
                ),
                "quota_interruption_total": sum(
                    int(row["outcome"]["quota_interruption_count"]) for row in rows
                ),
                "wall_time_total_ms": wall_total,
                "wall_time_average_ms": wall_total // sample_count,
                "observed_cost_count": len(observed_costs),
                "observed_cost_total_milliunits": sum(observed_costs),
                "unknown_cost_count": sample_count - len(observed_costs),
            }
        )
    return result


def _snapshot_digest(observations: list[dict[str, Any]]) -> str:
    raw = json.dumps(observations, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_provider_route_quality_snapshot(
    observations: list[object],
) -> dict[str, object]:
    if not isinstance(observations, list) or not observations or len(observations) > _MAX_OBSERVATIONS:
        _reject("provider route quality snapshot requires 1-512 observations")
    normalized = sorted(
        (validate_provider_route_outcome(item) for item in observations),
        key=lambda item: item["observation_id"],
    )
    aggregates = aggregate_provider_route_outcomes(normalized)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": SNAPSHOT_KIND,
        "authority": AUTHORITY,
        "broker_influence": BROKER_INFLUENCE,
        "observation_digest": _snapshot_digest(normalized),
        "observations": normalized,
        "routes": aggregates,
    }


def validate_provider_route_quality_snapshot(payload: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "broker_influence",
        "observation_digest",
        "observations",
        "routes",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("provider route quality snapshot schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or payload.get("kind") != SNAPSHOT_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("broker_influence") != BROKER_INFLUENCE
    ):
        _reject("provider route quality snapshot version/authority is invalid")
    observations = payload.get("observations")
    if not isinstance(observations, list):
        _reject("provider route quality observations are invalid")
    rebuilt = build_provider_route_quality_snapshot(observations)
    if payload != rebuilt:
        _reject("provider route quality snapshot derived state mismatch")
    return rebuilt


def publish_provider_route_quality_snapshot(
    data_root: Path,
    observation_paths: list[Path],
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("provider route quality data root is not a directory")
    if (
        not isinstance(observation_paths, list)
        or not observation_paths
        or len(observation_paths) > _MAX_OBSERVATIONS
    ):
        _reject("provider route quality requires 1-512 observation files")
    observations = [
        _load_json(
            Path(path),
            label="provider route outcome",
            max_bytes=_MAX_OBSERVATION_BYTES,
        )
        for path in observation_paths
    ]
    snapshot = build_provider_route_quality_snapshot(observations)
    text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    with data_root_write_lock(root):
        atomic_write_text(root / FILENAME, text)
    return provider_route_quality_dashboard(root)


def provider_route_quality_dashboard(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return {
            "state": "UNKNOWN",
            "detail": "no provider route quality snapshot loaded",
            "authority": AUTHORITY,
            "broker_influence": BROKER_INFLUENCE,
            "snapshot_path": FILENAME,
            "observation_digest": None,
            "observation_count": 0,
            "routes": [],
        }
    snapshot = validate_provider_route_quality_snapshot(
        _load_json(
            path,
            label="provider route quality snapshot",
            max_bytes=_MAX_SNAPSHOT_BYTES,
        )
    )
    return {
        "state": "OBSERVED",
        "detail": (
            f'{len(snapshot["observations"])} verified route outcome observation(s); '
            "evidence only, broker behavior unchanged"
        ),
        "authority": AUTHORITY,
        "broker_influence": BROKER_INFLUENCE,
        "snapshot_path": FILENAME,
        "observation_digest": snapshot["observation_digest"],
        "observation_count": len(snapshot["observations"]),
        "routes": snapshot["routes"],
    }
