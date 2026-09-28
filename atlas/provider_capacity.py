"""Provider-neutral, read-only capacity input contract.

Capacity observations are evidence, not routing authority. Unknown provider
facts remain explicit UNKNOWN values rather than estimates.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
_KIND = "provider_capacity_input"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SIGNAL_KEYS = frozenset(
    {
        "remaining_capacity",
        "reset_at",
        "capacity_pool",
        "active_inference_wip",
    }
)
_TOP_KEYS = frozenset({"schema_version", "kind", "provider", "scope", "evidence", "signals"})
_EVIDENCE_KEYS = frozenset(
    {"source_kind", "window_start", "window_end", "event_count", "total_tokens"}
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _timestamp(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _UTC_TIMESTAMP_RE.fullmatch(value):
        _reject(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"{label} must be a UTC timestamp")
    return value


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _reject(f"{label} must be a non-negative integer")
    return value


def _bounded_label(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        _reject(f"{label} is not a bounded label")
    return value


def _validate_signal(name: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        _reject(f"{name} signal must be an object")
    status = raw.get("status")
    if status == "UNKNOWN":
        if set(raw) != {"status"}:
            _reject(f"{name} UNKNOWN signal cannot carry a value")
        return {"status": "UNKNOWN"}
    if status != "OBSERVED":
        _reject(f"{name} signal status is invalid")

    if name == "remaining_capacity":
        if set(raw) != {"status", "value", "unit"}:
            _reject("remaining_capacity OBSERVED signal schema is invalid")
        value = raw.get("value")
        if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
            _reject("remaining_capacity value must be a decimal string")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValidationError(
                "remaining_capacity value must be a decimal string"
            ) from exc
        if not number.is_finite() or number < 0:
            _reject("remaining_capacity value must be finite and non-negative")
        unit = _bounded_label(raw.get("unit"), label="remaining_capacity unit")
        return {"status": "OBSERVED", "value": value, "unit": unit}

    if name == "reset_at":
        if set(raw) != {"status", "value"}:
            _reject("reset_at OBSERVED signal schema is invalid")
        return {
            "status": "OBSERVED",
            "value": _timestamp(raw.get("value"), label="reset_at"),
        }

    if name == "capacity_pool":
        if set(raw) != {"status", "value"}:
            _reject("capacity_pool OBSERVED signal schema is invalid")
        return {
            "status": "OBSERVED",
            "value": _bounded_label(raw.get("value"), label="capacity_pool"),
        }

    if name == "active_inference_wip":
        if set(raw) != {"status", "value"}:
            _reject("active_inference_wip OBSERVED signal schema is invalid")
        return {
            "status": "OBSERVED",
            "value": _nonnegative_int(
                raw.get("value"), label="active_inference_wip"
            ),
        }

    _reject("unknown capacity signal")


def validate_provider_capacity_input(payload: object) -> dict:
    """Validate and normalize one provider-neutral capacity input."""
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _reject("provider capacity input schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION:
        _reject("provider capacity input schema_version is unsupported")
    if payload.get("kind") != _KIND:
        _reject("provider capacity input kind is invalid")

    provider = payload.get("provider")
    if not isinstance(provider, str) or not _PROVIDER_RE.fullmatch(provider):
        _reject("provider capacity input provider is invalid")
    scope = payload.get("scope")
    if scope != "ACCOUNT_AGGREGATE":
        _reject("provider capacity input scope is unsupported")

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        _reject("provider capacity input evidence schema is invalid")
    source_kind = _bounded_label(evidence.get("source_kind"), label="source_kind")
    event_count = _nonnegative_int(evidence.get("event_count"), label="event_count")
    total_tokens = _nonnegative_int(evidence.get("total_tokens"), label="total_tokens")
    window_start = _timestamp(
        evidence.get("window_start"), label="window_start", nullable=True
    )
    window_end = _timestamp(
        evidence.get("window_end"), label="window_end", nullable=True
    )
    if event_count == 0:
        if total_tokens != 0:
            _reject("empty usage evidence cannot claim token usage")
        if window_start is not None or window_end is not None:
            _reject("empty usage evidence cannot claim an observation window")
    else:
        if window_start is None or window_end is None:
            _reject("non-empty usage evidence requires an observation window")
        if datetime.fromisoformat(window_start.replace("Z", "+00:00")) > datetime.fromisoformat(
            window_end.replace("Z", "+00:00")
        ):
            _reject("usage evidence window is reversed")

    signals = payload.get("signals")
    if not isinstance(signals, dict) or set(signals) != _SIGNAL_KEYS:
        _reject("provider capacity input signals schema is invalid")
    normalized_signals = {
        name: _validate_signal(name, signals[name]) for name in sorted(_SIGNAL_KEYS)
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": _KIND,
        "provider": provider,
        "scope": scope,
        "evidence": {
            "source_kind": source_kind,
            "window_start": window_start,
            "window_end": window_end,
            "event_count": event_count,
            "total_tokens": total_tokens,
        },
        "signals": normalized_signals,
    }


def build_provider_capacity_input(
    *,
    provider: str,
    scope: str = "ACCOUNT_AGGREGATE",
    source_kind: str,
    window_start: str | None,
    window_end: str | None,
    event_count: int,
    total_tokens: int,
) -> dict:
    """Build an observation-only capacity input with unavailable facts UNKNOWN."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": _KIND,
        "provider": provider,
        "scope": scope,
        "evidence": {
            "source_kind": source_kind,
            "window_start": window_start,
            "window_end": window_end,
            "event_count": event_count,
            "total_tokens": total_tokens,
        },
        "signals": {name: {"status": "UNKNOWN"} for name in _SIGNAL_KEYS},
    }
    return validate_provider_capacity_input(payload)
