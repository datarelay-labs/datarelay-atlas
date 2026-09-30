"""Seal exact-state provider transition authorization into a one-shot request.

The request is transport-free. It neither invokes a provider nor mutates
provider/session state. A future effect executor must consume this request only
after revalidating the same authorization and current-route state.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from atlas.provider_broker import _route_id as _broker_route_id
from atlas.provider_capability import CAPABILITY_NAMES
from atlas.provider_transition_authorization import (
    AUTHORIZED_AUTHORITY,
    provider_transition_effect_state_digest,
    validate_provider_transition_effect_authorization,
    validate_provider_transition_effect_state,
)
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "provider_transition_effect_request"
AUTHORITY = "SEALED_EFFECT_REQUEST_ONLY"
_MAX_EFFECT_EPOCH = 2**31 - 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "authority",
        "authorization_digest",
        "current_state_digest",
        "replay_key",
        "from_route_id",
        "to_route_id",
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
        _reject("provider transition effect request epoch is invalid")
    return value


def provider_transition_effect_request_digest(payload: object) -> str:
    return _canonical_digest(payload, label="provider transition effect request")


def _replay_key(
    *,
    authorization_digest: str,
    current_state_digest: str,
    from_route_id: str,
    to_route_id: str,
    state_revision: str,
    effect_epoch: int,
) -> str:
    basis = {
        "kind": "provider_transition_effect_replay_identity",
        "authorization_digest": authorization_digest,
        "current_state_digest": current_state_digest,
        "from_route_id": from_route_id,
        "to_route_id": to_route_id,
        "state_revision": state_revision,
        "effect_epoch": effect_epoch,
    }
    return _canonical_digest(basis, label="provider transition replay identity")


def seal_provider_transition_effect_request(
    authorization: object,
    current_state: object,
    *,
    expected_authorization_digest: str,
    expected_transition_plan_digest: str,
    expected_current_state_digest: str,
) -> dict[str, Any]:
    """Seal one transport-free effect request from still-current authority."""

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

    auth = validate_provider_transition_effect_authorization(
        authorization,
        expected_authorization_digest=trusted_auth,
        expected_transition_plan_digest=trusted_plan,
        expected_current_state_digest=trusted_state,
    )
    if auth["decision"] != "AUTHORIZED" or auth["authority"] != AUTHORIZED_AUTHORITY:
        _reject("provider transition authorization does not permit an effect request")

    state = validate_provider_transition_effect_state(
        current_state,
        expected_state_digest=trusted_state,
    )

    if (
        state["route_id"] != auth["current_route_id"]
        or state["route_id"] != auth["plan_from_route_id"]
        or state["state_revision"] != auth["state_revision"]
        or state["effect_epoch"] != auth["effect_epoch"]
        or provider_transition_effect_state_digest(state) != auth["current_state_digest"]
    ):
        _reject("provider transition current state changed before request sealing")

    target = auth["plan_to_route_id"]
    if not isinstance(target, str) or target == state["route_id"]:
        _reject("provider transition authorization target is invalid")

    replay_key = _replay_key(
        authorization_digest=trusted_auth,
        current_state_digest=trusted_state,
        from_route_id=state["route_id"],
        to_route_id=target,
        state_revision=state["state_revision"],
        effect_epoch=state["effect_epoch"],
    )
    request = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "authorization_digest": trusted_auth,
        "current_state_digest": trusted_state,
        "replay_key": replay_key,
        "from_route_id": state["route_id"],
        "to_route_id": target,
        "state_revision": state["state_revision"],
        "effect_epoch": state["effect_epoch"],
        "strategy": auth["strategy"],
        "required_capability": auth["required_capability"],
        "attempt": auth["attempt"],
        "max_attempts": auth["max_attempts"],
    }
    return validate_provider_transition_effect_request(
        request,
        expected_request_digest=provider_transition_effect_request_digest(request),
        expected_authorization_digest=trusted_auth,
        expected_current_state_digest=trusted_state,
    )


def validate_provider_transition_effect_request(
    payload: object,
    *,
    expected_request_digest: str,
    expected_authorization_digest: str,
    expected_current_state_digest: str,
) -> dict[str, Any]:
    """Validate one sealed request against trusted request/auth/state identities."""

    trusted_request = _trusted_digest(
        expected_request_digest,
        label="expected provider transition effect-request digest",
    )
    trusted_auth = _trusted_digest(
        expected_authorization_digest,
        label="expected provider transition authorization digest",
    )
    trusted_state = _trusted_digest(
        expected_current_state_digest,
        label="expected provider transition current-state digest",
    )

    if not isinstance(payload, dict) or set(payload) != _KEYS:
        _reject("provider transition effect request schema is invalid")
    if provider_transition_effect_request_digest(payload) != trusted_request:
        _reject("provider transition effect request digest does not match trusted identity")

    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        _reject("provider transition effect request schema_version is unsupported")
    if payload.get("kind") != KIND:
        _reject("provider transition effect request kind is invalid")
    if payload.get("authority") != AUTHORITY:
        _reject("provider transition effect request authority is invalid")
    if payload.get("authorization_digest") != trusted_auth:
        _reject("provider transition effect request authorization digest is inconsistent")
    if payload.get("current_state_digest") != trusted_state:
        _reject("provider transition effect request state digest is inconsistent")

    replay_key = _trusted_digest(
        payload.get("replay_key"),
        label="provider transition effect replay key",
    )
    from_route = _broker_route_id(payload.get("from_route_id"))
    to_route = _broker_route_id(payload.get("to_route_id"))
    if from_route == to_route:
        _reject("provider transition effect request target equals current route")

    revision = payload.get("state_revision")
    if not isinstance(revision, str) or _DIGEST_RE.fullmatch(revision) is None:
        _reject("provider transition effect request state_revision is invalid")
    epoch = _effect_epoch(payload.get("effect_epoch"))

    expected_replay = _replay_key(
        authorization_digest=trusted_auth,
        current_state_digest=trusted_state,
        from_route_id=from_route,
        to_route_id=to_route,
        state_revision=revision,
        effect_epoch=epoch,
    )
    if replay_key != expected_replay:
        _reject("provider transition effect request replay key is inconsistent")

    strategy = payload.get("strategy")
    if strategy not in {"CAPABILITY_FIRST", "STEWARDSHIP"}:
        _reject("provider transition effect request strategy is invalid")
    capability = payload.get("required_capability")
    if capability not in CAPABILITY_NAMES:
        _reject("provider transition effect request capability is invalid")

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
        _reject("provider transition effect request attempt metadata is invalid")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "authorization_digest": trusted_auth,
        "current_state_digest": trusted_state,
        "replay_key": replay_key,
        "from_route_id": from_route,
        "to_route_id": to_route,
        "state_revision": revision,
        "effect_epoch": epoch,
        "strategy": strategy,
        "required_capability": capability,
        "attempt": attempt,
        "max_attempts": maximum,
    }
