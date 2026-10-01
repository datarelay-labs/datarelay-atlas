"""Provider-neutral, read-only capacity broker planning.

The broker consumes validated capability and capacity evidence plus explicit
Atlas-owned gates. It can explain an advisory route ordering, but it has no
provider execution, credential, session, or mutation authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal

from atlas.provider_capability import (
    CAPABILITY_NAMES,
    validate_provider_capability_descriptor,
)
from atlas.provider_capacity import validate_provider_capacity_input
from atlas.provider_capacity_attribution import (
    validate_provider_capacity_attribution,
)
from atlas.provider_capacity_operational import validate_provider_capacity_operational
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
CANDIDATE_KIND = "provider_route_candidate"
PLAN_KIND = "provider_broker_plan"
AUTHORITY = "ADVISORY_ONLY"
STRATEGIES = frozenset({"CAPABILITY_FIRST", "STEWARDSHIP"})
GATE_STATES = frozenset({"ALLOW", "DENY", "UNKNOWN"})
_GATE_KEYS = (
    "policy",
    "trust",
    "budget",
    "usage_mode",
    "blast_radius",
    "wip",
)
_CANDIDATE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "route_id",
        "capability_descriptor",
        "capacity_input",
        "gates",
        "ranks",
    }
)
_CANDIDATE_OPTIONAL_KEYS = frozenset({"capacity_attribution", "capacity_operational"})
_RANK_KEYS = frozenset({"capability_preference", "stewardship_preference"})
_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "strategy",
        "required_capability",
        "authority",
        "evaluated_at",
        "max_evidence_age_seconds",
        "evidence_fresh_until",
        "selected_route_id",
        "fallback_route_ids",
        "eligible_routes",
        "ineligible_routes",
    }
)
_ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_SECRET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)
_MAX_ROUTES = 32
_MAX_RANK = 1_000_000
_INELIGIBLE_REASONS = (
    "POLICY_DENY",
    "POLICY_UNKNOWN",
    "TRUST_DENY",
    "TRUST_UNKNOWN",
    "BUDGET_DENY",
    "BUDGET_UNKNOWN",
    "USAGE_MODE_DENY",
    "USAGE_MODE_UNKNOWN",
    "BLAST_RADIUS_DENY",
    "BLAST_RADIUS_UNKNOWN",
    "WIP_DENY",
    "WIP_UNKNOWN",
    "CAPABILITY_NOT_DECLARED",
    "CAPABILITY_UNSUPPORTED",
    "CAPABILITY_UNKNOWN",
    "REMAINING_CAPACITY_UNKNOWN",
    "REMAINING_CAPACITY_EXHAUSTED",
    "REMAINING_CAPACITY_UNBOUND",
    "REMAINING_CAPACITY_FUTURE",
    "REMAINING_CAPACITY_STALE",
)
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_EVIDENCE_AGE_SECONDS = 366 * 24 * 60 * 60
_REASON_ORDER = {value: index for index, value in enumerate(_INELIGIBLE_REASONS)}


def _reject(message: str) -> None:
    raise ValidationError(message)


def provider_broker_plan_digest(payload: object) -> str:
    """Return a deterministic content digest for out-of-band plan identity."""
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("provider broker plan is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _require_trusted_plan_digest(payload: object, expected_plan_digest: str) -> None:
    if (
        not isinstance(expected_plan_digest, str)
        or _DIGEST_RE.fullmatch(expected_plan_digest) is None
    ):
        _reject("expected provider broker plan digest is invalid")
    if provider_broker_plan_digest(payload) != expected_plan_digest:
        _reject("provider broker plan digest does not match trusted identity")


def _route_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _ROUTE_RE.fullmatch(value)
        or _SECRET_RE.search(value) is not None
    ):
        _reject("route_id is invalid")
    return value


def _summary_identity(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER_RE if provider else _IDENTITY_RE
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or _SECRET_RE.search(value) is not None
    ):
        _reject(f"provider broker {label} is invalid")
    return value


def _rank(value: object, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_RANK
    ):
        _reject(f"{label} must be an integer from 0 to {_MAX_RANK}")
    return value


def _capability_name(value: object) -> str:
    if not isinstance(value, str) or value not in CAPABILITY_NAMES:
        _reject("required_capability is unsupported")
    return value


def validate_provider_route_candidate(payload: object) -> dict:
    """Validate one bounded route candidate without granting route authority."""
    if not isinstance(payload, dict):
        _reject("provider route candidate schema is invalid")
    payload_keys = set(payload)
    if (
        not _CANDIDATE_KEYS.issubset(payload_keys)
        or not payload_keys.issubset(_CANDIDATE_KEYS | _CANDIDATE_OPTIONAL_KEYS)
    ):
        _reject("provider route candidate schema is invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        _reject("provider route candidate schema_version is unsupported")
    if payload.get("kind") != CANDIDATE_KIND:
        _reject("provider route candidate kind is invalid")

    route_id = _route_id(payload.get("route_id"))
    descriptor = validate_provider_capability_descriptor(
        payload.get("capability_descriptor")
    )
    capacity = validate_provider_capacity_input(payload.get("capacity_input"))
    if descriptor["provider"] != capacity["provider"]:
        _reject("route provider identity does not match capacity evidence")

    attribution = None
    if "capacity_attribution" in payload:
        attribution = validate_provider_capacity_attribution(
            payload.get("capacity_attribution")
        )
        if attribution["provider"] != descriptor["provider"]:
            _reject("route provider identity does not match capacity attribution")

    operational = None
    if "capacity_operational" in payload:
        operational = validate_provider_capacity_operational(payload.get("capacity_operational"))
        if operational["provider"] != descriptor["provider"]:
            _reject("route provider identity does not match capacity operational evidence")

    gates = payload.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(_GATE_KEYS):
        _reject("provider route candidate gates schema is invalid")
    normalized_gates: dict[str, str] = {}
    for name in _GATE_KEYS:
        state = gates.get(name)
        if state not in GATE_STATES:
            _reject(f"{name} gate state is invalid")
        normalized_gates[name] = state

    ranks = payload.get("ranks")
    if not isinstance(ranks, dict) or set(ranks) != _RANK_KEYS:
        _reject("provider route candidate ranks schema is invalid")
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
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": CANDIDATE_KIND,
        "route_id": route_id,
        "capability_descriptor": descriptor,
        "capacity_input": capacity,
        "gates": normalized_gates,
        "ranks": normalized_ranks,
    }
    if attribution is not None:
        normalized["capacity_attribution"] = attribution
    if operational is not None:
        normalized["capacity_operational"] = operational
    return normalized


def _capability_status(descriptor: dict, required: str) -> str | None:
    for capability in descriptor["capabilities"]:
        if capability["name"] == required:
            return capability["status"]
    return None
def _utc_instant(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        _reject(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"{label} must be a UTC timestamp")
    return parsed


def _max_evidence_age_seconds(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_EVIDENCE_AGE_SECONDS
    ):
        _reject(
            "max_evidence_age_seconds must be an integer from 0 to "
            f"{_MAX_EVIDENCE_AGE_SECONDS}"
        )
    return value


def _format_utc(value: datetime) -> str:
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        fraction = f"{value.microsecond:06d}".rstrip("0")
        text = f"{text}.{fraction}"
    return f"{text}Z"


def _evidence_expiry(observed: datetime, max_age: timedelta) -> datetime:
    """Derive a bounded expiry without leaking datetime arithmetic errors."""
    try:
        return observed + max_age
    except OverflowError as exc:
        raise ValidationError(
            "provider broker evidence expiry is out of range"
        ) from exc


def _validated_plan_freshness(
    payload: dict,
    *,
    eligible: list[dict],
    expected_max_evidence_age_seconds: int,
) -> tuple[str, int, str | None, datetime, datetime | None]:
    """Recompute plan expiry from each eligible route's retained observation.

    ``evidence_fresh_until`` is accepted only when it equals the earliest
    ``observed_at + max_evidence_age_seconds``. A later declared expiry is
    rejected even when it is still within ``evaluated_at + max_age``.
    """
    evaluated = _utc_instant(payload.get("evaluated_at"), label="evaluated_at")
    max_age = _max_evidence_age_seconds(payload.get("max_evidence_age_seconds"))
    expected_max_age = _max_evidence_age_seconds(expected_max_evidence_age_seconds)
    if max_age != expected_max_age:
        _reject("provider broker plan max evidence age does not match trusted policy")
    raw_until = payload.get("evidence_fresh_until")
    if not eligible:
        if raw_until is not None:
            _reject("provider broker plan evidence boundary is inconsistent")
        return _format_utc(evaluated), max_age, None, evaluated, None
    if raw_until is None:
        _reject("provider broker plan evidence boundary is missing")
    declared = _utc_instant(raw_until, label="evidence_fresh_until")
    limit = timedelta(seconds=max_age)
    expiries: list[datetime] = []
    for route in eligible:
        observed = _utc_instant(route.get("observed_at"), label="observed_at")
        if observed > evaluated or evaluated - observed > limit:
            _reject("provider broker plan evidence boundary is inconsistent")
        expiry = _evidence_expiry(observed, limit)
        route_expiry = _utc_instant(route.get("fresh_until"), label="fresh_until")
        if route_expiry != expiry:
            _reject("provider broker plan evidence boundary is inconsistent")
        route["observed_at"] = _format_utc(observed)
        route["fresh_until"] = _format_utc(expiry)
        expiries.append(expiry)
    expected = min(expiries)
    if declared != expected:
        _reject("provider broker plan evidence boundary is inconsistent")
    return (
        _format_utc(evaluated),
        max_age,
        _format_utc(expected),
        evaluated,
        expected,
    )


def _require_plan_consumption(
    consumed_at: str,
    *,
    evaluated_at: datetime,
    fresh_until: datetime | None,
) -> None:
    """Reject eligible-plan replay outside the recorded evidence window."""
    consumed = _utc_instant(consumed_at, label="consumed_at")
    if fresh_until is None:
        return
    if consumed < evaluated_at or consumed > fresh_until:
        _reject("provider broker plan evidence is stale")


def _eligibility_reasons(
    candidate: dict,
    required: str,
    *,
    evaluated_at: datetime,
    max_evidence_age: timedelta,
) -> list[str]:
    reasons: list[str] = []
    for name in _GATE_KEYS:
        state = candidate["gates"][name]
        if state != "ALLOW":
            reasons.append(f"{name.upper()}_{state}")

    status = _capability_status(candidate["capability_descriptor"], required)
    if status is None:
        reasons.append("CAPABILITY_NOT_DECLARED")
    elif status != "SUPPORTED":
        reasons.append(f"CAPABILITY_{status}")

    remaining = candidate["capacity_input"]["signals"]["remaining_capacity"]
    if remaining["status"] == "UNKNOWN":
        reasons.append("REMAINING_CAPACITY_UNKNOWN")
    elif Decimal(remaining["value"]) == 0:
        reasons.append("REMAINING_CAPACITY_EXHAUSTED")
    else:
        observed_at = candidate["capacity_input"]["evidence"]["window_end"]
        if observed_at is None:
            reasons.append("REMAINING_CAPACITY_UNBOUND")
        else:
            observed = _utc_instant(observed_at, label="window_end")
            if observed > evaluated_at:
                reasons.append("REMAINING_CAPACITY_FUTURE")
            elif evaluated_at - observed > max_evidence_age:
                reasons.append("REMAINING_CAPACITY_STALE")
    return reasons


def _strategy_rank(candidate: dict, strategy: str) -> tuple[int, int, str]:
    ranks = candidate["ranks"]
    if strategy == "CAPABILITY_FIRST":
        return (
            ranks["capability_preference"],
            ranks["stewardship_preference"],
            candidate["route_id"],
        )
    if strategy == "STEWARDSHIP":
        return (
            ranks["stewardship_preference"],
            ranks["capability_preference"],
            candidate["route_id"],
        )
    _reject("provider broker strategy is unsupported")
def _route_summary(
    candidate: dict,
    *,
    reasons: list[str],
    rank: list[int] | None = None,
    observed_at: str | None = None,
    fresh_until: str | None = None,
) -> dict:
    descriptor = candidate["capability_descriptor"]
    summary = {
        "route_id": candidate["route_id"],
        "provider": descriptor["provider"],
        "runtime": descriptor["runtime"],
        "usage_mode": descriptor["usage_mode"],
        "reasons": reasons,
    }
    if rank is not None:
        summary["rank"] = rank
        summary["observed_at"] = observed_at
        summary["fresh_until"] = fresh_until
    return summary


def plan_provider_routes(
    candidates: object,
    *,
    required_capability: str,
    strategy: str = "CAPABILITY_FIRST",
    evaluated_at: str,
    max_evidence_age_seconds: int,
) -> dict:
    """Create a deterministic advisory provider-route plan."""
    required = _capability_name(required_capability)
    if strategy not in STRATEGIES:
        _reject("provider broker strategy is unsupported")
    evaluation_instant = _utc_instant(evaluated_at, label="evaluated_at")
    evidence_age_seconds = _max_evidence_age_seconds(max_evidence_age_seconds)
    evidence_age_limit = timedelta(seconds=evidence_age_seconds)
    if (
        not isinstance(candidates, list)
        or not candidates
        or len(candidates) > _MAX_ROUTES
    ):
        _reject(f"provider broker candidates must contain 1-{_MAX_ROUTES} routes")

    normalized = [validate_provider_route_candidate(item) for item in candidates]
    route_ids = [item["route_id"] for item in normalized]
    if len(set(route_ids)) != len(route_ids):
        _reject("provider broker route_id values must be unique")
    eligible: list[dict] = []
    ineligible: list[dict] = []
    sortable: list[tuple[tuple[int, int, str], dict]] = []
    for candidate in sorted(normalized, key=lambda item: item["route_id"]):
        reasons = _eligibility_reasons(
            candidate,
            required,
            evaluated_at=evaluation_instant,
            max_evidence_age=evidence_age_limit,
        )
        if reasons:
            ineligible.append(_route_summary(candidate, reasons=reasons))
            continue
        rank_key = _strategy_rank(candidate, strategy)
        sortable.append((rank_key, candidate))

    sortable.sort(key=lambda item: item[0])
    expiries: list[datetime] = []
    for rank_key, candidate in sortable:
        observed = _utc_instant(
            candidate["capacity_input"]["evidence"]["window_end"],
            label="window_end",
        )
        expiry = _evidence_expiry(observed, evidence_age_limit)
        expiries.append(expiry)
        eligible.append(
            _route_summary(
                candidate,
                reasons=["ELIGIBLE"],
                rank=[rank_key[0], rank_key[1]],
                observed_at=_format_utc(observed),
                fresh_until=_format_utc(expiry),
            )
        )

    selected = eligible[0]["route_id"] if eligible else None
    fresh_until = _format_utc(min(expiries)) if expiries else None
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "strategy": strategy,
        "required_capability": required,
        "authority": AUTHORITY,
        "evaluated_at": _format_utc(evaluation_instant),
        "max_evidence_age_seconds": evidence_age_seconds,
        "evidence_fresh_until": fresh_until,
        "selected_route_id": selected,
        "fallback_route_ids": [item["route_id"] for item in eligible[1:]],
        "eligible_routes": eligible,
        "ineligible_routes": ineligible,
    }
    return validate_provider_broker_plan(
        plan,
        consumed_at=evaluated_at,
        expected_max_evidence_age_seconds=evidence_age_seconds,
        expected_plan_digest=provider_broker_plan_digest(plan),
    )
def _validate_summary(item: object, *, eligible: bool) -> dict:
    expected = {"route_id", "provider", "runtime", "usage_mode", "reasons"}
    if eligible:
        expected.update({"rank", "observed_at", "fresh_until"})
    if not isinstance(item, dict) or set(item) != expected:
        _reject("provider broker route summary schema is invalid")

    route_id = _route_id(item.get("route_id"))
    provider = _summary_identity(
        item.get("provider"), label="provider", provider=True
    )
    runtime = _summary_identity(item.get("runtime"), label="runtime")
    usage_mode = _summary_identity(item.get("usage_mode"), label="usage_mode")

    reasons = item.get("reasons")
    if not isinstance(reasons, list) or not reasons or len(reasons) > 16:
        _reject("provider broker reasons are invalid")
    if len(set(reasons)) != len(reasons):
        _reject("provider broker reasons must be unique")

    normalized = {
        "route_id": route_id,
        "provider": provider,
        "runtime": runtime,
        "usage_mode": usage_mode,
        "reasons": list(reasons),
    }
    if eligible:
        rank = item.get("rank")
        if not isinstance(rank, list) or len(rank) != 2:
            _reject("provider broker eligible rank is invalid")
        normalized["rank"] = [
            _rank(rank[0], label="eligible rank"),
            _rank(rank[1], label="eligible rank"),
        ]
        if reasons != ["ELIGIBLE"]:
            _reject("eligible route reasons must be ELIGIBLE")
        normalized["observed_at"] = item.get("observed_at")
        normalized["fresh_until"] = item.get("fresh_until")
    else:
        if not all(reason in _REASON_ORDER for reason in reasons):
            _reject("provider broker ineligible reason is unsupported")
        if reasons != sorted(reasons, key=_REASON_ORDER.__getitem__):
            _reject("provider broker ineligible reasons are not deterministic")
    return normalized
def validate_provider_broker_plan(
    payload: object,
    *,
    consumed_at: str,
    expected_max_evidence_age_seconds: int,
    expected_plan_digest: str,
) -> dict:
    """Validate a broker plan against trusted policy and out-of-band identity.

    ``consumed_at``, ``expected_max_evidence_age_seconds``, and
    ``expected_plan_digest`` are caller-supplied. The digest binds the full
    serialized plan so coordinated timestamp edits cannot mint a new freshness
    window. This validator does not read a wall clock.
    """
    if not isinstance(payload, dict) or set(payload) != _PLAN_KEYS:
        _reject("provider broker plan schema is invalid")
    _require_trusted_plan_digest(payload, expected_plan_digest)
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        _reject("provider broker plan schema_version is unsupported")
    if payload.get("kind") != PLAN_KIND:
        _reject("provider broker plan kind is invalid")
    strategy = payload.get("strategy")
    if strategy not in STRATEGIES:
        _reject("provider broker strategy is unsupported")
    required = _capability_name(payload.get("required_capability"))
    if payload.get("authority") != AUTHORITY:
        _reject("provider broker plan authority is invalid")

    eligible_raw = payload.get("eligible_routes")
    ineligible_raw = payload.get("ineligible_routes")
    if not isinstance(eligible_raw, list) or len(eligible_raw) > _MAX_ROUTES:
        _reject("provider broker eligible_routes is invalid")
    if not isinstance(ineligible_raw, list) or len(ineligible_raw) > _MAX_ROUTES:
        _reject("provider broker ineligible_routes is invalid")
    route_count = len(eligible_raw) + len(ineligible_raw)
    if route_count == 0 or route_count > _MAX_ROUTES:
        _reject("provider broker plan must contain 1-32 routes")

    eligible = [_validate_summary(item, eligible=True) for item in eligible_raw]
    ineligible = [_validate_summary(item, eligible=False) for item in ineligible_raw]
    all_ids = [item["route_id"] for item in eligible + ineligible]
    if len(set(all_ids)) != len(all_ids):
        _reject("provider broker plan route ids must be unique")
    if eligible != sorted(
        eligible,
        key=lambda item: (item["rank"][0], item["rank"][1], item["route_id"]),
    ):
        _reject("provider broker eligible route order is not deterministic")
    if ineligible != sorted(ineligible, key=lambda item: item["route_id"]):
        _reject("provider broker ineligible route order is not deterministic")
    selected = payload.get("selected_route_id")
    if selected is not None:
        selected = _route_id(selected)
    expected_selected = eligible[0]["route_id"] if eligible else None
    if selected != expected_selected:
        _reject("provider broker selected route is inconsistent")

    fallback = payload.get("fallback_route_ids")
    if not isinstance(fallback, list) or len(fallback) > _MAX_ROUTES:
        _reject("provider broker fallback_route_ids is invalid")
    normalized_fallback = [_route_id(value) for value in fallback]
    expected_fallback = [item["route_id"] for item in eligible[1:]]
    if normalized_fallback != expected_fallback:
        _reject("provider broker fallback order is inconsistent")

    (
        evaluated_at,
        max_evidence_age_seconds,
        evidence_fresh_until,
        evaluated_instant,
        fresh_until,
    ) = _validated_plan_freshness(
        payload,
        eligible=eligible,
        expected_max_evidence_age_seconds=expected_max_evidence_age_seconds,
    )
    _require_plan_consumption(
        consumed_at,
        evaluated_at=evaluated_instant,
        fresh_until=fresh_until,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "strategy": strategy,
        "required_capability": required,
        "authority": AUTHORITY,
        "evaluated_at": evaluated_at,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "evidence_fresh_until": evidence_fresh_until,
        "selected_route_id": selected,
        "fallback_route_ids": normalized_fallback,
        "eligible_routes": eligible,
        "ineligible_routes": ineligible,
    }
