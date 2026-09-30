"""Exact-state authorization boundary for provider transition effects.

This module never invokes a provider and never mutates provider/session state.
It converts a fresh, trusted ADVISORY_ONLY transition plan plus an independently
trusted current-route state into either a bounded effect authorization or
HUMAN_REQUIRED evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from atlas.provider_broker import _route_id as _broker_route_id
from atlas.provider_capability import CAPABILITY_NAMES
from atlas.provider_transition import validate_provider_transition_plan
from atlas.provenance import ValidationError

STATE_SCHEMA_VERSION = 1
STATE_KIND = "provider_route_effect_state"
AUTH_SCHEMA_VERSION = 1
AUTH_KIND = "provider_transition_effect_authorization"

AUTHORIZED_AUTHORITY = "EFFECT_AUTHORIZATION_ONLY"
NO_AUTHORITY = "NO_EFFECT_AUTHORITY"

DECISIONS = frozenset({"AUTHORIZED", "HUMAN_REQUIRED"})
DECISION_REASONS = frozenset(
    {
        "EXACT_STATE_MATCH",
        "PLAN_REQUIRES_HUMAN",
        "CURRENT_ROUTE_MISMATCH",
    }
)

_MAX_EFFECT_EPOCH = 2**31 - 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "route_id",
        "state_revision",
        "effect_epoch",
    }
)
_AUTH_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "authority",
        "decision",
        "decision_reason",
        "transition_plan_digest",
        "current_state_digest",
        "plan_from_route_id",
        "plan_to_route_id",
        "current_route_id",
        "state_revision",
        "effect_epoch",
        "strategy",
        "required_capability",
        "attempt",
        "max_attempts",
    }
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_digest(payload: object, *, label: str) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _trusted_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _reject(f"{label} is invalid")
    return value


def _effect_epoch(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > _MAX_EFFECT_EPOCH
    ):
        _reject("provider transition effect_epoch is invalid")
    return value


def provider_transition_effect_state_digest(payload: object) -> str:
    return _canonical_digest(payload, label="provider transition effect state")


def provider_transition_effect_authorization_digest(payload: object) -> str:
    return _canonical_digest(
        payload, label="provider transition effect authorization"
    )


def validate_provider_transition_effect_state(
    payload: object,
    *,
    expected_state_digest: str,
) -> dict[str, Any]:
    """Validate a bounded current-route state against trusted external identity."""

    trusted = _trusted_digest(
        expected_state_digest,
        label="expected provider transition current-state digest",
    )
    if not isinstance(payload, dict) or set(payload) != _STATE_KEYS:
        _reject("provider transition effect state schema is invalid")
    if provider_transition_effect_state_digest(payload) != trusted:
        _reject("provider transition effect state digest does not match trusted identity")

    version = payload.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != STATE_SCHEMA_VERSION
    ):
        _reject("provider transition effect state schema_version is unsupported")
    if payload.get("kind") != STATE_KIND:
        _reject("provider transition effect state kind is invalid")

    route_id = _broker_route_id(payload.get("route_id"))
    revision = payload.get("state_revision")
    if not isinstance(revision, str) or _DIGEST_RE.fullmatch(revision) is None:
        _reject("provider transition state_revision is invalid")
    epoch = _effect_epoch(payload.get("effect_epoch"))

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "route_id": route_id,
        "state_revision": revision,
        "effect_epoch": epoch,
    }


def _authorization_result(
    *,
    authority: str,
    decision: str,
    decision_reason: str,
    transition_plan_digest: str,
    current_state_digest: str,
    plan_from_route_id: str,
    plan_to_route_id: str | None,
    current_route_id: str,
    state_revision: str,
    effect_epoch: int,
    strategy: str,
    required_capability: str,
    attempt: int,
    max_attempts: int,
) -> dict[str, Any]:
    return {
        "schema_version": AUTH_SCHEMA_VERSION,
        "kind": AUTH_KIND,
        "authority": authority,
        "decision": decision,
        "decision_reason": decision_reason,
        "transition_plan_digest": transition_plan_digest,
        "current_state_digest": current_state_digest,
        "plan_from_route_id": plan_from_route_id,
        "plan_to_route_id": plan_to_route_id,
        "current_route_id": current_route_id,
        "state_revision": state_revision,
        "effect_epoch": effect_epoch,
        "strategy": strategy,
        "required_capability": required_capability,
        "attempt": attempt,
        "max_attempts": max_attempts,
    }


def authorize_provider_transition_effect(
    transition_plan: object,
    current_state: object,
    *,
    consumed_at: str,
    expected_max_evidence_age_seconds: int,
    expected_transition_plan_digest: str,
    expected_current_state_digest: str,
) -> dict[str, Any]:
    """Authorize one future transition effect without performing that effect."""

    trusted_plan_digest = _trusted_digest(
        expected_transition_plan_digest,
        label="expected provider transition plan digest",
    )
    trusted_state_digest = _trusted_digest(
        expected_current_state_digest,
        label="expected provider transition current-state digest",
    )

    plan = validate_provider_transition_plan(
        transition_plan,
        consumed_at=consumed_at,
        expected_max_evidence_age_seconds=expected_max_evidence_age_seconds,
        expected_transition_plan_digest=trusted_plan_digest,
    )
    state = validate_provider_transition_effect_state(
        current_state,
        expected_state_digest=trusted_state_digest,
    )

    if plan["decision"] != "TRANSITION_RECOMMENDED":
        result = _authorization_result(
            authority=NO_AUTHORITY,
            decision="HUMAN_REQUIRED",
            decision_reason="PLAN_REQUIRES_HUMAN",
            transition_plan_digest=trusted_plan_digest,
            current_state_digest=trusted_state_digest,
            plan_from_route_id=plan["from_route_id"],
            plan_to_route_id=None,
            current_route_id=state["route_id"],
            state_revision=state["state_revision"],
            effect_epoch=state["effect_epoch"],
            strategy=plan["strategy"],
            required_capability=plan["required_capability"],
            attempt=plan["attempt"],
            max_attempts=plan["max_attempts"],
        )
        return validate_provider_transition_effect_authorization(
            result,
            expected_authorization_digest=provider_transition_effect_authorization_digest(
                result
            ),
            expected_transition_plan_digest=trusted_plan_digest,
            expected_current_state_digest=trusted_state_digest,
        )

    target = plan["to_route_id"]
    if not isinstance(target, str):
        _reject("provider transition recommended plan lacks a target route")

    if state["route_id"] != plan["from_route_id"]:
        result = _authorization_result(
            authority=NO_AUTHORITY,
            decision="HUMAN_REQUIRED",
            decision_reason="CURRENT_ROUTE_MISMATCH",
            transition_plan_digest=trusted_plan_digest,
            current_state_digest=trusted_state_digest,
            plan_from_route_id=plan["from_route_id"],
            plan_to_route_id=target,
            current_route_id=state["route_id"],
            state_revision=state["state_revision"],
            effect_epoch=state["effect_epoch"],
            strategy=plan["strategy"],
            required_capability=plan["required_capability"],
            attempt=plan["attempt"],
            max_attempts=plan["max_attempts"],
        )
        return validate_provider_transition_effect_authorization(
            result,
            expected_authorization_digest=provider_transition_effect_authorization_digest(
                result
            ),
            expected_transition_plan_digest=trusted_plan_digest,
            expected_current_state_digest=trusted_state_digest,
        )

    if target == state["route_id"]:
        _reject("provider transition target route cannot equal current route")

    result = _authorization_result(
        authority=AUTHORIZED_AUTHORITY,
        decision="AUTHORIZED",
        decision_reason="EXACT_STATE_MATCH",
        transition_plan_digest=trusted_plan_digest,
        current_state_digest=trusted_state_digest,
        plan_from_route_id=plan["from_route_id"],
        plan_to_route_id=target,
        current_route_id=state["route_id"],
        state_revision=state["state_revision"],
        effect_epoch=state["effect_epoch"],
        strategy=plan["strategy"],
        required_capability=plan["required_capability"],
        attempt=plan["attempt"],
        max_attempts=plan["max_attempts"],
    )
    return validate_provider_transition_effect_authorization(
        result,
        expected_authorization_digest=provider_transition_effect_authorization_digest(
            result
        ),
        expected_transition_plan_digest=trusted_plan_digest,
        expected_current_state_digest=trusted_state_digest,
    )


def validate_provider_transition_effect_authorization(
    payload: object,
    *,
    expected_authorization_digest: str,
    expected_transition_plan_digest: str,
    expected_current_state_digest: str,
) -> dict[str, Any]:
    """Validate one authorization against all three trusted identities."""

    trusted_auth = _trusted_digest(
        expected_authorization_digest,
        label="expected provider transition authorization digest",
    )
    trusted_plan = _trusted_digest(
        expected_transition_plan_digest,
        label="expected provider transition plan digest",
    )
    trusted_state = _trusted_digest(
        expected_current_state_digest,
        label="expected provider transition current-state digest",
    )

    if not isinstance(payload, dict) or set(payload) != _AUTH_KEYS:
        _reject("provider transition effect authorization schema is invalid")
    if provider_transition_effect_authorization_digest(payload) != trusted_auth:
        _reject(
            "provider transition effect authorization digest does not match trusted identity"
        )

    version = payload.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != AUTH_SCHEMA_VERSION
    ):
        _reject("provider transition effect authorization schema_version is unsupported")
    if payload.get("kind") != AUTH_KIND:
        _reject("provider transition effect authorization kind is invalid")

    authority = payload.get("authority")
    decision = payload.get("decision")
    reason = payload.get("decision_reason")
    if decision not in DECISIONS or reason not in DECISION_REASONS:
        _reject("provider transition effect authorization decision is invalid")
    if payload.get("transition_plan_digest") != trusted_plan:
        _reject("provider transition authorization plan digest is inconsistent")
    if payload.get("current_state_digest") != trusted_state:
        _reject("provider transition authorization state digest is inconsistent")

    from_route = _broker_route_id(payload.get("plan_from_route_id"))
    current_route = _broker_route_id(payload.get("current_route_id"))
    to_raw = payload.get("plan_to_route_id")
    to_route = None if to_raw is None else _broker_route_id(to_raw)

    revision = payload.get("state_revision")
    if not isinstance(revision, str) or _DIGEST_RE.fullmatch(revision) is None:
        _reject("provider transition authorization state_revision is invalid")
    epoch = _effect_epoch(payload.get("effect_epoch"))

    strategy = payload.get("strategy")
    if strategy not in {"CAPABILITY_FIRST", "STEWARDSHIP"}:
        _reject("provider transition authorization strategy is invalid")
    capability = payload.get("required_capability")
    if capability not in CAPABILITY_NAMES:
        _reject("provider transition authorization capability is invalid")

    attempt = payload.get("attempt")
    maximum = payload.get("max_attempts")
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or attempt < 1
        or maximum < 1
        or attempt > maximum
        or maximum > 32
    ):
        _reject("provider transition authorization attempt metadata is invalid")

    if decision == "AUTHORIZED":
        if authority != AUTHORIZED_AUTHORITY:
            _reject("AUTHORIZED transition must carry effect authorization")
        if reason != "EXACT_STATE_MATCH":
            _reject("AUTHORIZED transition reason is inconsistent")
        if current_route != from_route:
            _reject("AUTHORIZED transition current route is inconsistent")
        if to_route is None or to_route == current_route:
            _reject("AUTHORIZED transition target route is invalid")
    else:
        if authority != NO_AUTHORITY:
            _reject("HUMAN_REQUIRED transition must carry no effect authority")
        if reason == "EXACT_STATE_MATCH":
            _reject("HUMAN_REQUIRED transition reason is inconsistent")
        if reason == "PLAN_REQUIRES_HUMAN" and to_route is not None:
            _reject("PLAN_REQUIRES_HUMAN cannot select a target route")
        if reason == "CURRENT_ROUTE_MISMATCH":
            if to_route is None or current_route == from_route:
                _reject("CURRENT_ROUTE_MISMATCH evidence is inconsistent")

    return {
        "schema_version": AUTH_SCHEMA_VERSION,
        "kind": AUTH_KIND,
        "authority": authority,
        "decision": decision,
        "decision_reason": reason,
        "transition_plan_digest": trusted_plan,
        "current_state_digest": trusted_state,
        "plan_from_route_id": from_route,
        "plan_to_route_id": to_route,
        "current_route_id": current_route,
        "state_revision": revision,
        "effect_epoch": epoch,
        "strategy": strategy,
        "required_capability": capability,
        "attempt": attempt,
        "max_attempts": maximum,
    }
