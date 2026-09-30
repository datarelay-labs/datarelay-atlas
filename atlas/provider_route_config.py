"""Approved provider route configuration and strict evidence binding.

Configuration is operator-owned static intent only. It never grants provider
execution, trust, budget, quota, or transition authority.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from atlas.provider_broker import GATE_STATES, validate_provider_route_candidate
from atlas.provider_capability import (
    CAPABILITY_NAMES,
    descriptor_for_adapter,
    validate_provider_capability_descriptor,
)
from atlas.provider_capacity import validate_provider_capacity_input
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "approved_provider_route_set"
AUTHORITY = "CONFIGURATION_ONLY"
CONFIGURABLE_LIVE_ADAPTERS = frozenset(
    {
        "CodexAuditProvider",
        "BoundedResponsesAuditProvider",
    }
)

_ROUTE_SET_KEYS = frozenset({"schema_version", "kind", "authority", "routes"})
_ROUTE_KEYS = frozenset(
    {
        "route_id",
        "enabled",
        "provider",
        "runtime",
        "usage_mode",
        "adapter",
        "allowed_capabilities",
        "ranks",
    }
)
_RANK_KEYS = frozenset({"capability_preference", "stewardship_preference"})
_GATE_KEYS = frozenset(
    {"policy", "trust", "budget", "usage_mode", "blast_radius", "wip"}
)
_ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_SECRET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)
_MAX_ROUTES = 32
_MAX_RANK = 1_000_000
_MAX_INPUT_BYTES = 1024 * 1024


def _reject(message: str) -> None:
    raise ValidationError(message)


def _identity(
    value: object,
    *,
    label: str,
    provider: bool = False,
    route: bool = False,
) -> str:
    pattern = _ROUTE_RE if route else (_PROVIDER_RE if provider else _IDENTITY_RE)
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or _SECRET_RE.search(value) is not None
    ):
        _reject(f"provider route config {label} is invalid")
    return value


def _rank(value: object, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_RANK
    ):
        _reject(f"provider route config {label} must be 0-{_MAX_RANK}")
    return value


def _route(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _ROUTE_KEYS:
        _reject("provider route config route schema is invalid")

    route_id = _identity(payload.get("route_id"), label="route_id", route=True)
    enabled = payload.get("enabled")
    if type(enabled) is not bool:
        _reject("provider route config enabled must be boolean")
    provider = _identity(
        payload.get("provider"),
        label="provider",
        provider=True,
    )
    runtime = _identity(payload.get("runtime"), label="runtime")
    usage_mode = _identity(payload.get("usage_mode"), label="usage_mode")
    adapter = _identity(payload.get("adapter"), label="adapter")

    raw_capabilities = payload.get("allowed_capabilities")
    if (
        not isinstance(raw_capabilities, list)
        or not raw_capabilities
        or len(raw_capabilities) > len(CAPABILITY_NAMES)
    ):
        _reject("provider route config allowed_capabilities is invalid")
    capabilities: list[str] = []
    for item in raw_capabilities:
        if not isinstance(item, str) or item not in CAPABILITY_NAMES:
            _reject("provider route config capability is unsupported")
        capabilities.append(item)
    if len(capabilities) != len(set(capabilities)):
        _reject("provider route config capabilities must be unique")
    capabilities.sort()

    ranks = payload.get("ranks")
    if not isinstance(ranks, dict) or set(ranks) != _RANK_KEYS:
        _reject("provider route config ranks schema is invalid")
    normalized_ranks = {
        "capability_preference": _rank(
            ranks.get("capability_preference"),
            label="capability_preference",
        ),
        "stewardship_preference": _rank(
            ranks.get("stewardship_preference"),
            label="stewardship_preference",
        ),
    }

    return {
        "route_id": route_id,
        "enabled": enabled,
        "provider": provider,
        "runtime": runtime,
        "usage_mode": usage_mode,
        "adapter": adapter,
        "allowed_capabilities": capabilities,
        "ranks": normalized_ranks,
    }


def validate_provider_route_set(payload: object) -> dict[str, Any]:
    """Validate and normalize one bounded static approved route set."""
    if not isinstance(payload, dict) or set(payload) != _ROUTE_SET_KEYS:
        _reject("provider route set schema is invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _reject("provider route set schema_version is unsupported")
    if payload.get("kind") != KIND:
        _reject("provider route set kind is invalid")
    if payload.get("authority") != AUTHORITY:
        _reject("provider route set authority is invalid")

    raw_routes = payload.get("routes")
    if (
        not isinstance(raw_routes, list)
        or not raw_routes
        or len(raw_routes) > _MAX_ROUTES
    ):
        _reject(f"provider route set must contain 1-{_MAX_ROUTES} routes")
    routes = [_route(item) for item in raw_routes]
    route_ids = [item["route_id"] for item in routes]
    if len(route_ids) != len(set(route_ids)):
        _reject("provider route set route ids must be unique")
    routes.sort(key=lambda item: item["route_id"])

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "routes": routes,
    }


def _configured_route(route_set: dict[str, Any], route_id: str) -> dict[str, Any]:
    for route in route_set["routes"]:
        if route["route_id"] == route_id:
            return route
    _reject("provider route config route_id is not configured")


def _supported_capability(descriptor: dict[str, Any], required: str) -> bool:
    for item in descriptor["capabilities"]:
        if item["name"] == required:
            return item["status"] == "SUPPORTED"
    return False


def bind_configured_provider_route(
    route_set: object,
    *,
    route_id: str,
    capability_descriptor: object,
    capacity_input: object,
    gates: object,
    required_capability: str,
    capacity_attribution: object | None = None,
) -> dict[str, Any]:
    """Bind one configured route to existing dynamic evidence contracts."""
    normalized_set = validate_provider_route_set(route_set)
    configured_id = _identity(route_id, label="route_id", route=True)
    route = _configured_route(normalized_set, configured_id)
    if route["enabled"] is not True:
        _reject("provider route config route is disabled")

    if required_capability not in CAPABILITY_NAMES:
        _reject("provider route config required capability is unsupported")
    if required_capability not in route["allowed_capabilities"]:
        _reject("provider route config capability is not approved")

    descriptor = validate_provider_capability_descriptor(capability_descriptor)
    expected_identity = (
        route["provider"],
        route["runtime"],
        route["usage_mode"],
        route["adapter"],
    )
    actual_identity = (
        descriptor["provider"],
        descriptor["runtime"],
        descriptor["usage_mode"],
        descriptor["adapter"],
    )
    if actual_identity != expected_identity:
        _reject("provider route config descriptor identity mismatch")
    supported_capabilities = {
        item["name"]
        for item in descriptor["capabilities"]
        if item["status"] == "SUPPORTED"
    }
    if not supported_capabilities.issubset(set(route["allowed_capabilities"])):
        _reject(
            "provider route config descriptor exposes an unapproved supported capability"
        )
    if not _supported_capability(descriptor, required_capability):
        _reject("provider route config required capability is not supported")

    capacity = validate_provider_capacity_input(capacity_input)
    if capacity["provider"] != route["provider"]:
        _reject("provider route config capacity provider mismatch")

    if not isinstance(gates, dict) or set(gates) != _GATE_KEYS:
        _reject("provider route config gates schema is invalid")
    normalized_gates: dict[str, str] = {}
    for name in sorted(_GATE_KEYS):
        state = gates.get(name)
        if state not in GATE_STATES:
            _reject(f"provider route config {name} gate is invalid")
        normalized_gates[name] = state

    candidate = {
        "schema_version": 1,
        "kind": "provider_route_candidate",
        "route_id": route["route_id"],
        "capability_descriptor": descriptor,
        "capacity_input": capacity,
        "gates": normalized_gates,
        "ranks": dict(route["ranks"]),
    }
    if capacity_attribution is not None:
        candidate["capacity_attribution"] = capacity_attribution
    if capacity_operational is not None:
        candidate["capacity_operational"] = capacity_operational
    return validate_provider_route_candidate(candidate)


def materialize_registered_provider_route_candidate(
    route_set: object,
    *,
    route_id: str,
    capacity_input: object,
    gates: object,
    required_capability: str,
    capacity_attribution: object | None = None,
    capacity_operational: object | None = None,
) -> dict[str, Any]:
    """Materialize one configured live route from its registered adapter."""
    normalized_set = validate_provider_route_set(route_set)
    configured_id = _identity(route_id, label="route_id", route=True)
    route = _configured_route(normalized_set, configured_id)
    if route["enabled"] is not True:
        _reject("provider route config route is disabled")
    adapter = route["adapter"]
    if adapter not in CONFIGURABLE_LIVE_ADAPTERS:
        _reject("provider route config adapter is not live-configurable")
    descriptor = descriptor_for_adapter(adapter)
    return bind_configured_provider_route(
        normalized_set,
        route_id=configured_id,
        capability_descriptor=descriptor,
        capacity_input=capacity_input,
        gates=gates,
        required_capability=required_capability,
        capacity_attribution=capacity_attribution,
        capacity_operational=capacity_operational,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("provider route set contains a duplicate JSON key")
        result[key] = value
    return result


def load_provider_route_set(path: Path) -> dict[str, Any]:
    """Load one bounded UTF-8 JSON route set without external effects."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(_MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValidationError("provider route set file is not readable") from exc
    if len(raw) > _MAX_INPUT_BYTES:
        _reject("provider route set exceeds bounded input size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                "provider route set contains a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "provider route set is not valid UTF-8 JSON"
        ) from exc
    return validate_provider_route_set(payload)
