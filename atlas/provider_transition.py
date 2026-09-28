"""Deterministic read-only provider failover transition planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlas.provider_broker import (
    AUTHORITY,
    STRATEGIES,
    _route_id as _broker_route_id,
    _utc_instant,
    plan_provider_routes,
    validate_provider_broker_plan,
    validate_provider_route_candidate,
)
from atlas.provider_capability import CAPABILITY_NAMES
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "provider_route_transition_plan"
DECISIONS = frozenset({"TRANSITION_RECOMMENDED", "HUMAN_REQUIRED"})
FAILURE_REASONS = frozenset(
    {
        "QUOTA_EXHAUSTED",
        "RATE_LIMITED",
        "HEALTH_UNAVAILABLE",
        "RUNTIME_UNAVAILABLE",
        "OPERATOR_REQUEST",
    }
)
DECISION_REASONS = frozenset(
    {
        "ELIGIBLE_FALLBACK",
        "ATTEMPT_LIMIT_REACHED",
        "NO_REMAINING_ROUTES",
        "NO_ELIGIBLE_FALLBACK",
    }
)
_MAX_ROUTES = 32
_MAX_INPUT_BYTES = 1024 * 1024
_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "authority",
        "decision",
        "decision_reason",
        "strategy",
        "required_capability",
        "failure_reason",
        "from_route_id",
        "to_route_id",
        "prior_failed_route_ids",
        "failed_route_ids",
        "attempt",
        "max_attempts",
        "remaining_plan",
    }
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _positive_int(value: object, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > _MAX_ROUTES
    ):
        _reject(f"{label} must be an integer from 1 to {_MAX_ROUTES}")
    return value


def _known_route_id(value: object, known: set[str], *, label: str) -> str:
    if not isinstance(value, str) or value not in known:
        _reject(f"{label} is not a known route")
    return value


def _normalize_candidates(candidates: object) -> list[dict[str, Any]]:
    if (
        not isinstance(candidates, list)
        or not candidates
        or len(candidates) > _MAX_ROUTES
    ):
        _reject(f"provider transition candidates must contain 1-{_MAX_ROUTES} routes")
    normalized = [validate_provider_route_candidate(item) for item in candidates]
    route_ids = [item["route_id"] for item in normalized]
    if len(set(route_ids)) != len(route_ids):
        _reject("provider transition route_id values must be unique")
    return normalized


def _normalize_failed(value: object, known: set[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) >= _MAX_ROUTES:
        _reject("prior_failed_route_ids is invalid")
    normalized = [
        _known_route_id(item, known, label="prior failed route")
        for item in value
    ]
    if len(set(normalized)) != len(normalized):
        _reject("prior failed route ids must be unique")
    return sorted(normalized)


def _remaining_plan(
    candidates: list[dict[str, Any]],
    failed: set[str],
    *,
    required_capability: str,
    strategy: str,
    evaluated_at: str,
    max_evidence_age_seconds: int,
) -> dict[str, Any] | None:
    remaining = [
        item for item in candidates if item["route_id"] not in failed
    ]
    if not remaining:
        return None
    return plan_provider_routes(
        remaining,
        required_capability=required_capability,
        strategy=strategy,
        evaluated_at=evaluated_at,
        max_evidence_age_seconds=max_evidence_age_seconds,
    )


def plan_provider_transition(
    candidates: object,
    *,
    required_capability: str,
    current_route_id: str,
    failure_reason: str,
    prior_failed_route_ids: object = None,
    strategy: str = "CAPABILITY_FIRST",
    max_attempts: int = 3,
    evaluated_at: str,
    max_evidence_age_seconds: int,
) -> dict[str, Any]:
    """Plan one attributable failover without granting execution authority."""
    normalized = _normalize_candidates(candidates)
    known = {item["route_id"] for item in normalized}
    current = _known_route_id(current_route_id, known, label="current route")
    if required_capability not in CAPABILITY_NAMES:
        _reject("provider transition required_capability is unsupported")
    if strategy not in STRATEGIES:
        _reject("provider transition strategy is unsupported")
    if failure_reason not in FAILURE_REASONS:
        _reject("provider transition failure_reason is unsupported")
    maximum = _positive_int(max_attempts, label="max_attempts")
    prior_failed = _normalize_failed(prior_failed_route_ids, known)
    if len(prior_failed) >= maximum:
        _reject("prior failures already reached max_attempts")
    if current in set(prior_failed):
        _reject("current route is already failed")

    current_plan = _remaining_plan(
        normalized,
        set(prior_failed),
        required_capability=required_capability,
        strategy=strategy,
        evaluated_at=evaluated_at,
        max_evidence_age_seconds=max_evidence_age_seconds,
    )
    if current_plan is None or current_plan["selected_route_id"] != current:
        _reject("current route is not the selected eligible route")
    failed = sorted([*prior_failed, current])
    attempt = len(failed)
    remaining_plan = _remaining_plan(
        normalized,
        set(failed),
        required_capability=required_capability,
        strategy=strategy,
        evaluated_at=evaluated_at,
        max_evidence_age_seconds=max_evidence_age_seconds,
    )

    if attempt >= maximum:
        decision = "HUMAN_REQUIRED"
        decision_reason = "ATTEMPT_LIMIT_REACHED"
        to_route = None
    elif remaining_plan is None:
        decision = "HUMAN_REQUIRED"
        decision_reason = "NO_REMAINING_ROUTES"
        to_route = None
    elif remaining_plan["selected_route_id"] is None:
        decision = "HUMAN_REQUIRED"
        decision_reason = "NO_ELIGIBLE_FALLBACK"
        to_route = None
    else:
        decision = "TRANSITION_RECOMMENDED"
        decision_reason = "ELIGIBLE_FALLBACK"
        to_route = remaining_plan["selected_route_id"]

    return validate_provider_transition_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "authority": AUTHORITY,
            "decision": decision,
            "decision_reason": decision_reason,
            "strategy": strategy,
            "required_capability": required_capability,
            "failure_reason": failure_reason,
            "from_route_id": current,
            "to_route_id": to_route,
            "prior_failed_route_ids": prior_failed,
            "failed_route_ids": failed,
            "attempt": attempt,
            "max_attempts": maximum,
            "remaining_plan": remaining_plan,
        },
        consumed_at=evaluated_at,
    )


def validate_provider_transition_plan(
    payload: object,
    *,
    consumed_at: str,
) -> dict[str, Any]:
    """Validate one content-free advisory transition plan.

    ``consumed_at`` is forwarded to the embedded broker plan. This validator
    does not read a wall clock.
    """
    _utc_instant(consumed_at, label="consumed_at")
    if not isinstance(payload, dict) or set(payload) != _KEYS:
        _reject("provider transition plan schema is invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _reject("provider transition schema_version is unsupported")
    if payload.get("kind") != KIND:
        _reject("provider transition kind is invalid")
    if payload.get("authority") != AUTHORITY:
        _reject("provider transition authority is invalid")
    decision = payload.get("decision")
    if decision not in DECISIONS:
        _reject("provider transition decision is invalid")
    decision_reason = payload.get("decision_reason")
    if decision_reason not in DECISION_REASONS:
        _reject("provider transition decision_reason is invalid")
    strategy = payload.get("strategy")
    if strategy not in STRATEGIES:
        _reject("provider transition strategy is unsupported")
    required = payload.get("required_capability")
    if required not in CAPABILITY_NAMES:
        _reject("provider transition required_capability is unsupported")
    failure_reason = payload.get("failure_reason")
    if failure_reason not in FAILURE_REASONS:
        _reject("provider transition failure_reason is unsupported")

    prior_raw = payload.get("prior_failed_route_ids")
    if not isinstance(prior_raw, list) or len(prior_raw) >= _MAX_ROUTES:
        _reject("provider transition prior_failed_route_ids is invalid")
    prior = [_broker_route_id(item) for item in prior_raw]
    if len(set(prior)) != len(prior) or prior != sorted(prior):
        _reject("provider transition prior_failed_route_ids is invalid")

    from_route = _broker_route_id(payload.get("from_route_id"))
    if from_route in set(prior):
        _reject("provider transition from_route_id is already failed")

    failed_raw = payload.get("failed_route_ids")
    if (
        not isinstance(failed_raw, list)
        or not failed_raw
        or len(failed_raw) > _MAX_ROUTES
    ):
        _reject("provider transition failed_route_ids is invalid")
    failed = [_broker_route_id(item) for item in failed_raw]
    expected_failed = sorted([*prior, from_route])
    if (
        len(set(failed)) != len(failed)
        or failed != expected_failed
    ):
        _reject("provider transition failed_route_ids is inconsistent")
    known = set(failed)

    attempt = _positive_int(payload.get("attempt"), label="attempt")
    maximum = _positive_int(payload.get("max_attempts"), label="max_attempts")
    if attempt != len(failed):
        _reject("provider transition attempt does not match failed routes")
    if attempt > maximum:
        _reject("provider transition attempt exceeds max_attempts")

    remaining_raw = payload.get("remaining_plan")
    remaining = (
        None
        if remaining_raw is None
        else validate_provider_broker_plan(
            remaining_raw,
            consumed_at=consumed_at,
        )
    )
    if remaining is not None:
        if remaining["strategy"] != strategy:
            _reject("provider transition remaining plan strategy is inconsistent")
        if remaining["required_capability"] != required:
            _reject(
                "provider transition remaining plan capability is inconsistent"
            )
        remaining_ids = {
            item["route_id"]
            for item in (
                remaining["eligible_routes"]
                + remaining["ineligible_routes"]
            )
        }
        if remaining_ids & known:
            _reject("provider transition remaining plan contains a failed route")
    to_raw = payload.get("to_route_id")
    to_route = None if to_raw is None else _broker_route_id(to_raw)
    if to_route is not None and to_route in known:
        _reject("provider transition cannot select a failed route")

    if decision == "TRANSITION_RECOMMENDED":
        if decision_reason != "ELIGIBLE_FALLBACK":
            _reject("provider transition recommendation reason is inconsistent")
        if attempt >= maximum:
            _reject("provider transition recommendation exceeds attempt limit")
        if remaining is None or remaining["selected_route_id"] is None:
            _reject("provider transition recommendation lacks eligible fallback")
        if to_route != remaining["selected_route_id"]:
            _reject("provider transition to_route_id is inconsistent")
    else:
        if to_route is not None:
            _reject("HUMAN_REQUIRED cannot select a route")
        if decision_reason == "ATTEMPT_LIMIT_REACHED":
            if attempt < maximum:
                _reject("attempt-limit decision is premature")
        elif decision_reason == "NO_REMAINING_ROUTES":
            if attempt >= maximum or remaining is not None:
                _reject("no-remaining-routes decision is inconsistent")
        elif decision_reason == "NO_ELIGIBLE_FALLBACK":
            if (
                attempt >= maximum
                or remaining is None
                or remaining["selected_route_id"] is not None
            ):
                _reject("no-eligible-fallback decision is inconsistent")
        else:
            _reject("HUMAN_REQUIRED decision reason is inconsistent")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "decision": decision,
        "decision_reason": decision_reason,
        "strategy": strategy,
        "required_capability": required,
        "failure_reason": failure_reason,
        "from_route_id": from_route,
        "to_route_id": to_route,
        "prior_failed_route_ids": list(prior),
        "failed_route_ids": list(failed),
        "attempt": attempt,
        "max_attempts": maximum,
        "remaining_plan": remaining,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("provider transition input contains a duplicate JSON key")
        result[key] = value
    return result


def load_provider_transition_candidates(path: Path) -> object:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(_MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValidationError("provider transition candidates file is not readable") from exc
    if len(raw) > _MAX_INPUT_BYTES:
        _reject("provider transition candidates exceed bounded input size")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                "provider transition candidates contain a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "provider transition candidates are not valid UTF-8 JSON"
        ) from exc
