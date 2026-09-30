"""Provider-neutral capacity attribution evidence contract.

These facts describe where capacity belongs and how it is charged. They are
observation evidence only and never grant routing or provider-effect authority.
Unknown provider facts remain explicit UNKNOWN values.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "provider_capacity_attribution"
AUTHORITY_PROVIDER = "PROVIDER_AUTHORITATIVE"
AUTHORITY_UNVERIFIED = "UNVERIFIED"

FACT_KEYS = frozenset(
    {
        "execution_surface",
        "allowance_domain",
        "shared_allowance",
        "charging_mode",
    }
)
_TOP_KEYS = frozenset({"schema_version", "kind", "provider", "evidence", "facts"})
_EVIDENCE_KEYS = frozenset({"authority", "source_kind", "observed_at"})
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _provider(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER_RE.fullmatch(value) is None:
        _reject("provider capacity attribution provider is invalid")
    return value


def _label(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _LABEL_RE.fullmatch(value) is None:
        _reject(f"{label} is not a bounded label")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        _reject("provider capacity attribution observed_at must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            "provider capacity attribution observed_at must be a UTC timestamp"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("provider capacity attribution observed_at must be a UTC timestamp")
    return value


def _fact(name: str, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _reject(f"provider capacity attribution {name} fact must be an object")
    status = raw.get("status")
    if status == "UNKNOWN":
        if set(raw) != {"status"}:
            _reject(
                f"provider capacity attribution {name} UNKNOWN fact cannot carry a value"
            )
        return {"status": "UNKNOWN"}
    if status != "OBSERVED":
        _reject(f"provider capacity attribution {name} status is invalid")

    if name == "shared_allowance":
        if set(raw) != {"status", "value"} or not isinstance(raw.get("value"), bool):
            _reject(
                "provider capacity attribution shared_allowance OBSERVED fact is invalid"
            )
        return {"status": "OBSERVED", "value": raw["value"]}

    if set(raw) != {"status", "value"}:
        _reject(f"provider capacity attribution {name} OBSERVED fact is invalid")
    return {
        "status": "OBSERVED",
        "value": _label(
            raw.get("value"),
            label=f"provider capacity attribution {name}",
        ),
    }


def validate_provider_capacity_attribution(payload: object) -> dict[str, Any]:
    """Validate one bounded provider capacity attribution observation."""

    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _reject("provider capacity attribution schema is invalid")

    version = payload.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SCHEMA_VERSION
    ):
        _reject("provider capacity attribution schema_version is unsupported")
    if payload.get("kind") != KIND:
        _reject("provider capacity attribution kind is invalid")

    provider = _provider(payload.get("provider"))

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        _reject("provider capacity attribution evidence schema is invalid")
    authority = evidence.get("authority")
    if authority not in {AUTHORITY_PROVIDER, AUTHORITY_UNVERIFIED}:
        _reject("provider capacity attribution evidence authority is invalid")
    source_kind = _label(evidence.get("source_kind"), label="source_kind")
    observed_at_raw = evidence.get("observed_at")

    facts = payload.get("facts")
    if not isinstance(facts, dict) or set(facts) != FACT_KEYS:
        _reject("provider capacity attribution facts schema is invalid")
    normalized_facts = {
        name: _fact(name, facts[name]) for name in sorted(FACT_KEYS)
    }
    has_observed = any(
        item["status"] == "OBSERVED" for item in normalized_facts.values()
    )

    if has_observed:
        if authority != AUTHORITY_PROVIDER:
            _reject(
                "OBSERVED provider capacity attribution requires "
                "PROVIDER_AUTHORITATIVE evidence"
            )
        observed_at = _timestamp(observed_at_raw)
    elif authority == AUTHORITY_PROVIDER:
        if observed_at_raw is None:
            _reject("PROVIDER_AUTHORITATIVE attribution evidence requires observed_at")
        observed_at = _timestamp(observed_at_raw)
    else:
        if observed_at_raw is not None:
            _reject("UNVERIFIED attribution evidence cannot claim observed_at")
        observed_at = None

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "provider": provider,
        "evidence": {
            "authority": authority,
            "source_kind": source_kind,
            "observed_at": observed_at,
        },
        "facts": normalized_facts,
    }


def build_unknown_provider_capacity_attribution(
    *,
    provider: str,
    source_kind: str,
) -> dict[str, Any]:
    """Build attribution with every unproven fact explicitly UNKNOWN."""

    return validate_provider_capacity_attribution(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "provider": provider,
            "evidence": {
                "authority": AUTHORITY_UNVERIFIED,
                "source_kind": source_kind,
                "observed_at": None,
            },
            "facts": {name: {"status": "UNKNOWN"} for name in FACT_KEYS},
        }
    )
