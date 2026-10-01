"""Provider-authoritative operational capacity evidence.

Evidence-only facts for reset semantics, service health, and latency. These
facts do not grant routing, ranking, provider invocation, or transition authority.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Any
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
KIND = "provider_capacity_operational"
AUTHORITY_PROVIDER = "PROVIDER_AUTHORITATIVE"
AUTHORITY_UNVERIFIED = "UNVERIFIED"
FACT_KEYS = frozenset({"reset_semantics", "health", "latency"})
_TOP_KEYS = frozenset({"schema_version", "kind", "provider", "evidence", "facts"})
_EVIDENCE_KEYS = frozenset({"authority", "source_kind", "source_ref", "source_digest", "observed_at"})
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
_HEALTH = {"OPERATIONAL", "DEGRADED", "OUTAGE", "MAINTENANCE"}
_LATENCY_METRICS = {"ROUND_TRIP_MS", "TIME_TO_FIRST_BYTE_MS", "PROVIDER_REPORTED_MS"}
_RESET_MODES = {"FIXED_WINDOW", "ROLLING_WINDOW", "CALENDAR_WINDOW", "EVENT_DRIVEN"}

def _reject(message: str) -> None:
    raise ValidationError(message)

def _label(value: object, label: str) -> str:
    if not isinstance(value, str) or _LABEL_RE.fullmatch(value) is None or _SECRET_RE.search(value):
        _reject(f"{label} is not a bounded label")
    return value

def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        _reject("provider operational observed_at must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("provider operational observed_at must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("provider operational observed_at must be UTC")
    return value

def _unknown(raw: object, name: str) -> bool:
    if isinstance(raw, dict) and raw.get("status") == "UNKNOWN":
        if set(raw) != {"status"}:
            _reject(f"provider operational {name} UNKNOWN cannot carry values")
        return True
    return False

def _reset(raw: object) -> dict[str, Any]:
    if _unknown(raw, "reset_semantics"): return {"status": "UNKNOWN"}
    if not isinstance(raw, dict) or set(raw) != {"status", "mode", "window_seconds"} or raw.get("status") != "OBSERVED":
        _reject("provider operational reset_semantics is invalid")
    mode, seconds = raw.get("mode"), raw.get("window_seconds")
    if mode not in _RESET_MODES or isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 31_536_000:
        _reject("provider operational reset_semantics is invalid")
    return {"status": "OBSERVED", "mode": mode, "window_seconds": seconds}

def _health(raw: object) -> dict[str, Any]:
    if _unknown(raw, "health"): return {"status": "UNKNOWN"}
    if not isinstance(raw, dict) or set(raw) != {"status", "value"} or raw.get("status") != "OBSERVED" or raw.get("value") not in _HEALTH:
        _reject("provider operational health is invalid")
    return {"status": "OBSERVED", "value": raw["value"]}

def _latency(raw: object) -> dict[str, Any]:
    if _unknown(raw, "latency"): return {"status": "UNKNOWN"}
    if not isinstance(raw, dict) or set(raw) != {"status", "metric", "boundary", "value_ms"} or raw.get("status") != "OBSERVED":
        _reject("provider operational latency is invalid")
    metric, boundary, value = raw.get("metric"), raw.get("boundary"), raw.get("value_ms")
    if metric not in _LATENCY_METRICS or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3_600_000:
        _reject("provider operational latency is invalid")
    return {"status": "OBSERVED", "metric": metric, "boundary": _label(boundary, "latency boundary"), "value_ms": value}

def validate_provider_capacity_operational(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _reject("provider operational schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool) or payload.get("kind") != KIND:
        _reject("provider operational version/kind is invalid")
    provider = payload.get("provider")
    if not isinstance(provider, str) or _PROVIDER_RE.fullmatch(provider) is None:
        _reject("provider operational provider is invalid")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        _reject("provider operational evidence is invalid")
    authority = evidence.get("authority")
    if authority not in {AUTHORITY_PROVIDER, AUTHORITY_UNVERIFIED}:
        _reject("provider operational authority is invalid")
    source_kind = _label(evidence.get("source_kind"), "source_kind")
    facts = payload.get("facts")
    if not isinstance(facts, dict) or set(facts) != FACT_KEYS:
        _reject("provider operational facts are invalid")
    normalized = {"reset_semantics": _reset(facts["reset_semantics"]), "health": _health(facts["health"]), "latency": _latency(facts["latency"])}
    observed = any(value["status"] == "OBSERVED" for value in normalized.values())
    if observed and authority != AUTHORITY_PROVIDER:
        _reject("OBSERVED provider operational facts require PROVIDER_AUTHORITATIVE evidence")
    ref, digest, observed_at = evidence.get("source_ref"), evidence.get("source_digest"), evidence.get("observed_at")
    if authority == AUTHORITY_PROVIDER:
        ref = _label(ref, "source_ref")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None: _reject("source_digest is invalid")
        observed_at = _timestamp(observed_at)
    else:
        if any(value is not None for value in (ref, digest, observed_at)):
            _reject("UNVERIFIED operational evidence cannot claim source provenance")
        ref = digest = observed_at = None
    return {"schema_version": SCHEMA_VERSION, "kind": KIND, "provider": provider,
        "evidence": {"authority": authority, "source_kind": source_kind, "source_ref": ref, "source_digest": digest, "observed_at": observed_at},
        "facts": normalized}

def build_unknown_provider_capacity_operational(*, provider: str, source_kind: str) -> dict[str, Any]:
    return validate_provider_capacity_operational({"schema_version": 1, "kind": KIND, "provider": provider,
        "evidence": {"authority": AUTHORITY_UNVERIFIED, "source_kind": source_kind, "source_ref": None, "source_digest": None, "observed_at": None},
        "facts": {name: {"status": "UNKNOWN"} for name in FACT_KEYS}})
