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


def publish_provider_dashboard_snapshot(
    data_root: Path,
    *,
    candidate_paths: list[Path],
    observed_at: str,
    required_capability: str,
    strategy: str,
    max_evidence_age_seconds: int,
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

    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "observed_at": _utc(observed_at),
        "required_capability": required_capability,
        "strategy": strategy,
        "max_evidence_age_seconds": max_evidence_age_seconds,
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


def provider_dashboard(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return {
            "state": "UNKNOWN",
            "detail": "no provider capacity snapshot loaded",
            "snapshot_path": FILENAME,
            "observed_at": None,
            "required_capability": None,
            "strategy": None,
            "authority": "ADVISORY_ONLY",
            "routes": [],
            "plan": None,
        }
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
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 32:
        _reject("provider dashboard candidates must contain 1-32 routes")
    normalized = [validate_provider_route_candidate(item) for item in candidates]
    route_ids = [item["route_id"] for item in normalized]
    if len(route_ids) != len(set(route_ids)):
        _reject("provider dashboard route ids must be unique")

    plan = plan_provider_routes(
        normalized,
        required_capability=capability,
        strategy=strategy,
        evaluated_at=observed_at,
        max_evidence_age_seconds=max_age,
    )
    return {
        "state": "OBSERVED",
        "detail": f"{len(normalized)} validated route candidate(s); broker plan recomputed read-only",
        "snapshot_path": FILENAME,
        "observed_at": observed_at,
        "required_capability": capability,
        "strategy": strategy,
        "authority": plan["authority"],
        "routes": [_route_view(item) for item in sorted(normalized, key=lambda row: row["route_id"])],
        "plan": plan,
    }
