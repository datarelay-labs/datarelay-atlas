"""Read-only provider capacity dashboard projection."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provider_broker import (
    STRATEGIES,
    plan_provider_routes,
    validate_provider_route_candidate,
)
from atlas.provider_capability import CAPABILITY_NAMES
from atlas.provider_route_config import (
    bind_configured_provider_route,
    load_provider_route_set,
    validate_provider_route_set,
)
from atlas.provider_transition import FAILURE_REASONS, plan_provider_transition
from atlas.provider_route_quality import provider_route_quality_dashboard
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "provider_capacity_dashboard_snapshot"
FILENAME = "provider-capacity-snapshot.json"
_MAX_BYTES = 1024 * 1024
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)

def _reject(message: str) -> None:
    raise ValidationError(message)


def _utc(value: object) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("provider dashboard observed_at must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("provider dashboard observed_at must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("provider dashboard observed_at must be a UTC timestamp")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("provider dashboard snapshot contains a duplicate JSON key")
        result[key] = value
    return result


def _load(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("provider dashboard snapshot is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject("provider dashboard snapshot exceeds bounded input size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                "provider dashboard snapshot contains a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("provider dashboard snapshot is invalid JSON") from exc
    assert_content_free(payload)
    return payload


def _fact_text(fact: object) -> str:
    if not isinstance(fact, dict):
        return "UNKNOWN"
    if fact.get("status") != "OBSERVED":
        return "UNKNOWN"
    if "value" in fact and "unit" in fact:
        return f'{fact["value"]} {fact["unit"]}'
    if "metric" in fact and "value_ms" in fact:
        return f'{fact["metric"]} · {fact.get("boundary", "UNKNOWN")} · {fact["value_ms"]} ms'
    if "mode" in fact and "window_seconds" in fact:
        return f'{fact["mode"]} · {fact["window_seconds"]}s'
    if "value" in fact:
        return str(fact["value"])
    if "value_ms" in fact:
        return f'{fact["value_ms"]} ms'
    if "state" in fact:
        return str(fact["state"])
    if "mode" in fact:
        return str(fact["mode"])
    return "OBSERVED"

def _route_view(candidate: dict[str, Any]) -> dict[str, object]:
    descriptor = candidate["capability_descriptor"]
    capacity = candidate["capacity_input"]
    attribution = candidate.get("capacity_attribution")
    operational = candidate.get("capacity_operational")
    signals = capacity["signals"]
    facts = attribution["facts"] if isinstance(attribution, dict) else {}
    operational_facts = operational["facts"] if isinstance(operational, dict) else {}

    return {
        "route_id": candidate["route_id"],
        "provider": descriptor["provider"],
        "runtime": descriptor["runtime"],
        "usage_mode": descriptor["usage_mode"],
        "adapter": descriptor["adapter"],
        "capabilities": [
            {"name": item["name"], "status": item["status"]}
            for item in descriptor["capabilities"]
        ],
        "gates": dict(candidate["gates"]),
        "ranks": dict(candidate["ranks"]),
        "capacity": {
            name: {
                "status": signals[name]["status"],
                "display": _fact_text(signals[name]),
            }
            for name in sorted(signals)
        },
        "attribution": {
            name: {
                "status": facts.get(name, {}).get("status", "UNKNOWN"),
                "display": _fact_text(facts.get(name)),
            }
            for name in ("execution_surface", "allowance_domain", "shared_allowance", "charging_mode")
        },
        "operational": {
            name: {
                "status": operational_facts.get(name, {}).get("status", "UNKNOWN"),
                "display": _fact_text(operational_facts.get(name)),
            }
            for name in ("reset_semantics", "health", "latency")
        },
        "capacity_evidence": dict(capacity["evidence"]),
        "attribution_evidence": (
            dict(attribution["evidence"]) if isinstance(attribution, dict) else None
        ),
        "operational_evidence": (
            dict(operational["evidence"]) if isinstance(operational, dict) else None
        ),
    }


def _bind_candidates_to_route_set(
    route_set: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    required_capability: str,
    require_exact: bool,
) -> list[dict[str, Any]]:
    rebound: list[dict[str, Any]] = []
    for candidate in candidates:
        normalized = bind_configured_provider_route(
            route_set,
            route_id=candidate["route_id"],
            capability_descriptor=candidate["capability_descriptor"],
            capacity_input=candidate["capacity_input"],
            gates=candidate["gates"],
            required_capability=required_capability,
            capacity_attribution=candidate.get("capacity_attribution"),
            capacity_operational=candidate.get("capacity_operational"),
        )
        if require_exact and normalized != candidate:
            raise ValidationError(
                "provider dashboard candidate does not match approved route configuration"
            )
        rebound.append(normalized)
    return rebound


def publish_provider_dashboard_snapshot(
    data_root: Path,
    *,
    candidate_paths: list[Path],
    observed_at: str,
    required_capability: str,
    strategy: str,
    max_evidence_age_seconds: int,
    route_set_path: Path | None = None,
) -> dict[str, object]:
    """Validate candidate files and publish one derived local dashboard snapshot."""
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("provider dashboard data root is not a directory")
    if (
        not isinstance(candidate_paths, list)
        or not candidate_paths
        or len(candidate_paths) > 32
    ):
        raise ValidationError("provider dashboard requires 1-32 candidate files")

    candidates = []
    for raw_path in candidate_paths:
        path = Path(raw_path)
        if path.is_symlink() or not path.is_file():
            raise ValidationError("provider dashboard candidate file is unsafe")
        candidates.append(validate_provider_route_candidate(_load(path)))

    route_ids = [item["route_id"] for item in candidates]
    if len(route_ids) != len(set(route_ids)):
        raise ValidationError("provider dashboard route ids must be unique")

    route_set = (
        load_provider_route_set(Path(route_set_path))
        if route_set_path is not None
        else None
    )
    if route_set is not None:
        candidates = _bind_candidates_to_route_set(
            route_set,
            candidates,
            required_capability=required_capability,
            require_exact=False,
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "observed_at": _utc(observed_at),
        "required_capability": required_capability,
        "strategy": strategy,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "route_set": route_set,
        "candidates": candidates,
    }
    assert_content_free(payload)
    # Reuse the broker itself as the final policy/candidate validator before publish.
    plan_provider_routes(
        candidates,
        required_capability=required_capability,
        strategy=strategy,
        evaluated_at=observed_at,
        max_evidence_age_seconds=max_evidence_age_seconds,
    )
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with data_root_write_lock(root):
        atomic_write_text(root / FILENAME, text)
    return provider_dashboard(root)


def _validated_snapshot_inputs(data_root: Path) -> dict[str, object] | None:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError("provider dashboard snapshot path is unsafe")

    payload = _load(path)
    expected = {
        "schema_version",
        "kind",
        "observed_at",
        "required_capability",
        "strategy",
        "max_evidence_age_seconds",
        "route_set",
        "candidates",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("provider dashboard snapshot schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(
        payload.get("schema_version"), bool
    ):
        _reject("provider dashboard snapshot schema_version is unsupported")
    if payload.get("kind") != KIND:
        _reject("provider dashboard snapshot kind is invalid")

    observed_at = _utc(payload.get("observed_at"))
    capability = payload.get("required_capability")
    if capability not in CAPABILITY_NAMES:
        _reject("provider dashboard required_capability is unsupported")
    strategy = payload.get("strategy")
    if strategy not in STRATEGIES:
        _reject("provider dashboard strategy is unsupported")
    max_age = payload.get("max_evidence_age_seconds")
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 0:
        _reject("provider dashboard max evidence age is invalid")
    route_set_raw = payload.get("route_set")
    route_set = (
        None
        if route_set_raw is None
        else validate_provider_route_set(route_set_raw)
    )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 32:
        _reject("provider dashboard candidates must contain 1-32 routes")

    normalized = [validate_provider_route_candidate(item) for item in candidates]
    if route_set is not None:
        normalized = _bind_candidates_to_route_set(
            route_set,
            normalized,
            required_capability=str(capability),
            require_exact=True,
        )
    route_ids = [item["route_id"] for item in normalized]
    if len(route_ids) != len(set(route_ids)):
        _reject("provider dashboard route ids must be unique")
    return {
        "observed_at": observed_at,
        "required_capability": capability,
        "strategy": strategy,
        "max_evidence_age_seconds": max_age,
        "route_set": route_set,
        "candidates": normalized,
    }


def provider_transition_preview(
    data_root: Path,
    *,
    current_route_id: str,
    failure_reason: str,
    prior_failed_route_ids: list[str] | None = None,
    max_attempts: int = 3,
) -> dict[str, object]:
    """Plan one read-only failover using the current validated dashboard snapshot."""
    snapshot = _validated_snapshot_inputs(data_root)
    if snapshot is None:
        raise ValidationError("provider dashboard snapshot is not loaded")
    if failure_reason not in FAILURE_REASONS:
        raise ValidationError("provider transition failure_reason is unsupported")
    return plan_provider_transition(
        snapshot["candidates"],
        required_capability=str(snapshot["required_capability"]),
        current_route_id=current_route_id,
        failure_reason=failure_reason,
        prior_failed_route_ids=prior_failed_route_ids or [],
        strategy=str(snapshot["strategy"]),
        max_attempts=max_attempts,
        evaluated_at=str(snapshot["observed_at"]),
        max_evidence_age_seconds=int(snapshot["max_evidence_age_seconds"]),
    )


def provider_dashboard(data_root: Path) -> dict[str, object]:
    snapshot = _validated_snapshot_inputs(data_root)
    if snapshot is None:
        return {
            "state": "UNKNOWN",
            "detail": "no provider capacity snapshot loaded",
            "snapshot_path": FILENAME,
            "observed_at": None,
            "required_capability": None,
            "strategy": None,
            "authority": "ADVISORY_ONLY",
            "configured_route_authority": "CONFIGURATION_ONLY",
            "configured_routes": [],
            "transition_failure_reasons": sorted(FAILURE_REASONS),
            "routes": [],
            "strategy_plans": {},
            "plan": None,
            "route_quality": provider_route_quality_dashboard(data_root),
        }
    observed_at = str(snapshot["observed_at"])
    capability = str(snapshot["required_capability"])
    strategy = str(snapshot["strategy"])
    max_age = int(snapshot["max_evidence_age_seconds"])
    normalized = list(snapshot["candidates"])
    route_set = snapshot["route_set"]
    configured_routes = (
        [dict(item) for item in route_set["routes"]]
        if isinstance(route_set, dict)
        else []
    )

    strategy_plans = {
        strategy_name: plan_provider_routes(
            normalized,
            required_capability=capability,
            strategy=strategy_name,
            evaluated_at=observed_at,
            max_evidence_age_seconds=max_age,
        )
        for strategy_name in sorted(STRATEGIES)
    }
    plan = strategy_plans[strategy]
    return {
        "state": "OBSERVED",
        "detail": f"{len(normalized)} validated route candidate(s); broker plan recomputed read-only",
        "snapshot_path": FILENAME,
        "observed_at": observed_at,
        "required_capability": capability,
        "strategy": strategy,
        "authority": plan["authority"],
        "configured_route_authority": (
            route_set["authority"] if isinstance(route_set, dict) else "UNKNOWN"
        ),
        "configured_routes": configured_routes,
        "transition_failure_reasons": sorted(FAILURE_REASONS),
        "routes": [_route_view(item) for item in sorted(normalized, key=lambda row: row["route_id"])],
        "strategy_plans": strategy_plans,
        "plan": plan,
        "route_quality": provider_route_quality_dashboard(data_root),
    }
