"""Canonical configured provider-route catalog and candidate materialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlas.provider_broker import (
    validate_provider_route_candidate,
    validate_provider_route_id,
)
from atlas.provider_capability import descriptor_for_adapter
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
CATALOG_KIND = "provider_route_catalog"
AUTHORITY = "CONFIGURATION_ONLY"
ROUTE_STATES = frozenset({"ENABLED", "DISABLED"})
CONFIGURABLE_ADAPTERS = frozenset(
    {
        "CodexAuditProvider",
        "BoundedResponsesAuditProvider",
    }
)
_MAX_ROUTES = 32
_MAX_INPUT_BYTES = 1024 * 1024
_CATALOG_KEYS = frozenset({"schema_version", "kind", "authority", "routes"})
_ROUTE_KEYS = frozenset({"route_id", "adapter", "state"})


def _reject(message: str) -> None:
    raise ValidationError(message)


def _validate_route_id(value: object) -> str:
    return validate_provider_route_id(value)


def _route_entry(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != _ROUTE_KEYS:
        _reject("provider route catalog entry schema is invalid")
    route_id = _validate_route_id(raw.get("route_id"))
    adapter = raw.get("adapter")
    if not isinstance(adapter, str) or adapter not in CONFIGURABLE_ADAPTERS:
        _reject("provider route catalog adapter is not configurable")
    # Resolve now so the catalog cannot name an adapter that has lost its
    # canonical capability registration.
    descriptor_for_adapter(adapter)
    state = raw.get("state")
    if state not in ROUTE_STATES:
        _reject("provider route catalog state is invalid")
    return {
        "route_id": route_id,
        "adapter": adapter,
        "state": state,
    }


def validate_provider_route_catalog(payload: object) -> dict[str, Any]:
    """Validate and deterministically normalize a configured route catalog."""
    if not isinstance(payload, dict) or set(payload) != _CATALOG_KEYS:
        _reject("provider route catalog schema is invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _reject("provider route catalog schema_version is unsupported")
    if payload.get("kind") != CATALOG_KIND:
        _reject("provider route catalog kind is invalid")
    if payload.get("authority") != AUTHORITY:
        _reject("provider route catalog cannot grant execution authority")
    routes_raw = payload.get("routes")
    if (
        not isinstance(routes_raw, list)
        or not routes_raw
        or len(routes_raw) > _MAX_ROUTES
    ):
        _reject(f"provider route catalog must contain 1-{_MAX_ROUTES} routes")
    routes = [_route_entry(item) for item in routes_raw]
    route_ids = [item["route_id"] for item in routes]
    if len(route_ids) != len(set(route_ids)):
        _reject("provider route catalog route ids must be unique")
    adapters = [item["adapter"] for item in routes]
    if len(adapters) != len(set(adapters)):
        _reject(
            "provider route catalog adapters must be unique until route profiles exist"
        )
    routes.sort(key=lambda item: item["route_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": CATALOG_KIND,
        "authority": AUTHORITY,
        "routes": routes,
    }


def configured_route_descriptor(
    catalog: object,
    *,
    route_id: str,
) -> dict[str, Any]:
    """Resolve one enabled configured route to its canonical descriptor."""
    normalized = validate_provider_route_catalog(catalog)
    target = _validate_route_id(route_id)
    for route in normalized["routes"]:
        if route["route_id"] != target:
            continue
        if route["state"] != "ENABLED":
            _reject("provider route is disabled")
        return descriptor_for_adapter(route["adapter"])
    _reject("provider route is not configured")


def materialize_provider_route_candidate(
    catalog: object,
    *,
    route_id: str,
    capacity_input: object,
    gates: object,
    ranks: object,
) -> dict[str, Any]:
    """Materialize a broker candidate using only the registered adapter descriptor."""
    descriptor = configured_route_descriptor(catalog, route_id=route_id)
    return validate_provider_route_candidate(
        {
            "schema_version": 1,
            "kind": "provider_route_candidate",
            "route_id": _validate_route_id(route_id),
            "capability_descriptor": descriptor,
            "capacity_input": capacity_input,
            "gates": gates,
            "ranks": ranks,
        }
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("provider route catalog contains a duplicate JSON key")
        result[key] = value
    return result


def load_provider_route_catalog(path: Path) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(_MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValidationError("provider route catalog is not readable") from exc
    if len(raw) > _MAX_INPUT_BYTES:
        _reject("provider route catalog exceeds bounded input size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                "provider route catalog contains a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "provider route catalog is not valid UTF-8 JSON"
        ) from exc
    return validate_provider_route_catalog(payload)
