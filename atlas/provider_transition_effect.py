"""One-shot provider-neutral transition effect commit boundary."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol

from atlas.provider_broker import _route_id as _broker_route_id
from atlas.provider_transition_authorization import (
    provider_transition_effect_state_digest,
    validate_provider_transition_effect_state,
)
from atlas.provider_transition_effect_request import (
    validate_provider_transition_effect_request,
)
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "provider_transition_effect_receipt"
OUTCOMES = frozenset({"PASS", "HUMAN_REQUIRED"})
REASONS = frozenset(
    {
        "COMMITTED",
        "EFFECT_REFUSED",
        "EFFECT_AMBIGUOUS",
        "EFFECT_ERROR",
        "EFFECT_RESULT_INVALID",
    }
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_EFFECT_EPOCH = 2**31 - 1
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "outcome",
        "reason",
        "effect_request_digest",
        "authorization_digest",
        "old_state_digest",
        "new_state_digest",
        "from_route_id",
        "to_route_id",
        "old_state_revision",
        "new_state_revision",
        "old_effect_epoch",
        "new_effect_epoch",
    }
)


class ProviderTransitionEffectPort(Protocol):
    def commit(self, request: dict[str, Any]) -> object: ...


def _reject(message: str) -> None:
    raise ValidationError(message)


def _trusted_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _reject(f"{label} is invalid")
    return value


def provider_transition_effect_receipt_digest(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "provider transition effect receipt is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _receipt(
    *,
    outcome: str,
    reason: str,
    effect_request_digest: str,
    authorization_digest: str,
    old_state_digest: str,
    from_route_id: str,
    to_route_id: str,
    old_state_revision: str,
    old_effect_epoch: int,
    new_state_digest: str | None = None,
    new_state_revision: str | None = None,
    new_effect_epoch: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "outcome": outcome,
        "reason": reason,
        "effect_request_digest": effect_request_digest,
        "authorization_digest": authorization_digest,
        "old_state_digest": old_state_digest,
        "new_state_digest": new_state_digest,
        "from_route_id": from_route_id,
        "to_route_id": to_route_id,
        "old_state_revision": old_state_revision,
        "new_state_revision": new_state_revision,
        "old_effect_epoch": old_effect_epoch,
        "new_effect_epoch": new_effect_epoch,
    }


def validate_provider_transition_effect_receipt(
    payload: object,
    *,
    expected_receipt_digest: str,
    expected_request_digest: str,
    expected_authorization_digest: str,
) -> dict[str, Any]:
    receipt_digest = _trusted_digest(
        expected_receipt_digest,
        label="expected provider transition effect receipt digest",
    )
    request_digest = _trusted_digest(
        expected_request_digest,
        label="expected provider transition effect-request digest",
    )
    auth_digest = _trusted_digest(
        expected_authorization_digest,
        label="expected provider transition authorization digest",
    )
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS:
        _reject("provider transition effect receipt schema is invalid")
    if provider_transition_effect_receipt_digest(payload) != receipt_digest:
        _reject("provider transition effect receipt digest does not match trusted identity")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
        _reject("provider transition effect receipt identity is invalid")
    if payload.get("effect_request_digest") != request_digest:
        _reject("provider transition receipt request digest is inconsistent")
    if payload.get("authorization_digest") != auth_digest:
        _reject("provider transition receipt authorization digest is inconsistent")

    outcome = payload.get("outcome")
    reason = payload.get("reason")
    if outcome not in OUTCOMES or reason not in REASONS:
        _reject("provider transition effect receipt outcome is invalid")

    _trusted_digest(
        payload.get("old_state_digest"),
        label="provider transition old state digest",
    )
    old_revision = _trusted_digest(
        payload.get("old_state_revision"),
        label="provider transition old state revision",
    )
    old_epoch = payload.get("old_effect_epoch")
    if isinstance(old_epoch, bool) or not isinstance(old_epoch, int) or old_epoch < 1:
        _reject("provider transition old effect epoch is invalid")

    from_route = _broker_route_id(payload.get("from_route_id"))
    to_route = _broker_route_id(payload.get("to_route_id"))
    if from_route == to_route:
        _reject("provider transition receipt target equals current route")

    new_digest = payload.get("new_state_digest")
    new_revision = payload.get("new_state_revision")
    new_epoch = payload.get("new_effect_epoch")
    if outcome == "PASS":
        if reason != "COMMITTED":
            _reject("provider transition PASS receipt is inconsistent")
        _trusted_digest(new_digest, label="provider transition new state digest")
        _trusted_digest(new_revision, label="provider transition new state revision")
        if new_revision == old_revision:
            _reject("provider transition new state revision did not change")
        if (
            isinstance(new_epoch, bool)
            or not isinstance(new_epoch, int)
            or new_epoch != old_epoch + 1
        ):
            _reject("provider transition new effect epoch is inconsistent")
    else:
        if reason == "COMMITTED":
            _reject("HUMAN_REQUIRED receipt cannot claim COMMITTED")
        if any(value is not None for value in (new_digest, new_revision, new_epoch)):
            _reject("HUMAN_REQUIRED receipt cannot claim a new authoritative state")

    return dict(payload)


def commit_provider_transition_effect(
    effect_request: object,
    current_state: object,
    *,
    expected_request_digest: str,
    expected_authorization_digest: str,
    expected_current_state_digest: str,
    effect_port: ProviderTransitionEffectPort,
) -> dict[str, Any]:
    """Validate one sealed request, invoke one effect once, normalize receipt."""

    request_digest = _trusted_digest(
        expected_request_digest,
        label="expected provider transition effect-request digest",
    )
    auth_digest = _trusted_digest(
        expected_authorization_digest,
        label="expected provider transition authorization digest",
    )
    state_digest = _trusted_digest(
        expected_current_state_digest,
        label="expected provider transition current-state digest",
    )
    request = validate_provider_transition_effect_request(
        effect_request,
        expected_request_digest=request_digest,
        expected_authorization_digest=auth_digest,
        expected_current_state_digest=state_digest,
    )
    state = validate_provider_transition_effect_state(
        current_state,
        expected_state_digest=state_digest,
    )

    if (
        request["current_state_digest"] != state_digest
        or request["from_route_id"] != state["route_id"]
        or request["state_revision"] != state["state_revision"]
        or request["effect_epoch"] != state["effect_epoch"]
    ):
        _reject("provider transition current state changed after request sealing")

    target = request["to_route_id"]
    if target == state["route_id"]:
        _reject("provider transition sealed target is invalid")
    if state["effect_epoch"] >= _MAX_EFFECT_EPOCH:
        _reject("provider transition effect epoch cannot advance")

    effect_call = {
        "effect_request_digest": request_digest,
        "replay_key": request["replay_key"],
        "authorization_digest": auth_digest,
        "from_route_id": state["route_id"],
        "to_route_id": target,
        "expected_state_digest": state_digest,
        "expected_state_revision": state["state_revision"],
        "expected_effect_epoch": state["effect_epoch"],
    }
    try:
        raw = effect_port.commit(effect_call)
    except Exception:
        reason = "EFFECT_ERROR"
    else:
        if not isinstance(raw, dict) or set(raw) != {"outcome", "new_state"}:
            reason = "EFFECT_RESULT_INVALID"
        elif raw.get("outcome") == "REFUSED":
            reason = "EFFECT_REFUSED"
        elif raw.get("outcome") == "UNKNOWN":
            reason = "EFFECT_AMBIGUOUS"
        elif raw.get("outcome") != "COMMITTED":
            reason = "EFFECT_RESULT_INVALID"
        else:
            candidate = raw.get("new_state")
            try:
                candidate_digest = provider_transition_effect_state_digest(candidate)
                new_state = validate_provider_transition_effect_state(
                    candidate,
                    expected_state_digest=candidate_digest,
                )
                if (
                    new_state["route_id"] != target
                    or new_state["state_revision"] == state["state_revision"]
                    or new_state["effect_epoch"] != state["effect_epoch"] + 1
                ):
                    raise ValidationError(
                        "provider transition committed state is inconsistent"
                    )
            except ValidationError:
                reason = "EFFECT_RESULT_INVALID"
            else:
                result = _receipt(
                    outcome="PASS",
                    reason="COMMITTED",
                    effect_request_digest=request_digest,
                    authorization_digest=auth_digest,
                    old_state_digest=state_digest,
                    new_state_digest=candidate_digest,
                    from_route_id=state["route_id"],
                    to_route_id=target,
                    old_state_revision=state["state_revision"],
                    new_state_revision=new_state["state_revision"],
                    old_effect_epoch=state["effect_epoch"],
                    new_effect_epoch=new_state["effect_epoch"],
                )
                return validate_provider_transition_effect_receipt(
                    result,
                    expected_receipt_digest=provider_transition_effect_receipt_digest(
                        result
                    ),
                    expected_request_digest=request_digest,
                    expected_authorization_digest=auth_digest,
                )

    result = _receipt(
        outcome="HUMAN_REQUIRED",
        reason=reason,
        effect_request_digest=request_digest,
        authorization_digest=auth_digest,
        old_state_digest=state_digest,
        from_route_id=state["route_id"],
        to_route_id=target,
        old_state_revision=state["state_revision"],
        old_effect_epoch=state["effect_epoch"],
    )
    return validate_provider_transition_effect_receipt(
        result,
        expected_receipt_digest=provider_transition_effect_receipt_digest(result),
        expected_request_digest=request_digest,
        expected_authorization_digest=auth_digest,
    )
