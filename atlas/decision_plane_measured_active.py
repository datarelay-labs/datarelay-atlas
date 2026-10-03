"""Measured LIMITED_ACTIVE evidence and deterministic rollback policy.

This layer binds verified downstream outcomes to exact Decision Plane effect
receipts. It may force subsequent LIMITED_ACTIVE calls back to the deterministic
current choice. It never grants provider, permission, deploy, release, PASS, or
HUMAN_REQUIRED authority.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
OBSERVATION_KIND = "decision_plane_measured_active_observation"
RECORD_KIND = "decision_plane_measured_active_record"
LEDGER_KIND = "decision_plane_measured_active_ledger"
FILENAME = "decision-plane-measured-active.json"
AUTHORITY = "MEASURED_ACTIVE_EVIDENCE_ONLY"
EXPANSION_AUTHORITY = "NONE"
PASS_AUTHORITY = "NONE"
HUMAN_REQUIRED_AUTHORITY = "NONE"
DECISION_CLASSES = frozenset({
    "OPTIONAL_CONTEXT_SELECTION",
    "FOCUSED_CHECK_SELECTION",
})
VERIFIED_OUTCOMES = frozenset({
    "VERIFIED_SUCCESS",
    "VERIFIED_FAILURE",
})
SOURCE_RESULTS = frozenset({
    "APPLIED_CANARY",
    "FALLBACK",
    "NO_EFFECT",
})
POLICY_STATES = frozenset({
    "LIMITED_ACTIVE_CONTINUE",
    "ROLLBACK_TO_CURRENT",
    "MEASURED_EXPANSION_ELIGIBLE",
})
MIN_EXPANSION_MEASUREMENTS = 5
_MAX_RECORDS = 1000
_MAX_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+#\-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SECRET = re.compile(
    r"(?:^|[^A-Za-z0-9])"
    r"(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or _ID.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"decision plane measured-active {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"decision plane measured-active {label} is invalid")
    return value


def _utc(value: object) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("decision plane measured-active timestamp must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            "decision plane measured-active timestamp must be UTC"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("decision plane measured-active timestamp must be UTC")
    return value


def _bounded_int(value: object, *, label: str, limit: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -limit <= value <= limit
    ):
        _reject(f"decision plane measured-active {label} is invalid")
    return value


def _canonical_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "decision plane measured-active payload is not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "authority": AUTHORITY,
        "records": [],
    }


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        _reject("decision plane measured-active ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            "decision plane measured-active ledger is unreadable"
        ) from exc
    if len(raw) > _MAX_BYTES:
        _reject("decision plane measured-active ledger exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "decision plane measured-active ledger is invalid JSON"
        ) from exc
    assert_content_free(payload)
    return payload


def _source_effect(
    data_root: Path,
    *,
    decision_class: str,
    activation_id: str,
    expected_receipt_digest: str,
) -> dict[str, object]:
    if decision_class == "OPTIONAL_CONTEXT_SELECTION":
        from atlas.decision_plane_limited_active import (
            get_optional_context_effect_entry,
        )

        entry = get_optional_context_effect_entry(
            data_root,
            activation_id,
        )
    elif decision_class == "FOCUSED_CHECK_SELECTION":
        from atlas.decision_plane_focused_check_limited_active import (
            get_focused_check_effect_entry,
        )

        entry = get_focused_check_effect_entry(
            data_root,
            activation_id,
        )
    else:
        _reject("decision plane measured-active decision class is unsupported")

    if entry.get("state") != "TERMINAL":
        _reject("decision plane measured-active source effect is not terminal")
    if entry.get("receipt_digest") != expected_receipt_digest:
        _reject("decision plane measured-active source receipt digest mismatch")
    receipt = entry.get("receipt")
    if not isinstance(receipt, dict):
        _reject("decision plane measured-active source receipt is unavailable")
    if receipt.get("decision_class") != decision_class:
        _reject("decision plane measured-active source class mismatch")
    if receipt.get("activation_id") != activation_id:
        _reject("decision plane measured-active source activation mismatch")
    result = receipt.get("result")
    if result not in SOURCE_RESULTS:
        _reject("decision plane measured-active source result is invalid")
    return {
        "entry": entry,
        "receipt": receipt,
        "source_result": result,
    }


def _selector_attribution(receipt: dict[str, object]) -> dict[str, str] | None:
    value = receipt.get("selector_attribution")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "provider",
        "model",
        "profile",
        "decision_ref",
    }:
        _reject(
            "decision plane measured-active source selector attribution is invalid"
        )
    return {
        "provider": _id(value.get("provider"), label="provider"),
        "model": _id(value.get("model"), label="model"),
        "profile": _id(value.get("profile"), label="profile"),
        "decision_ref": _id(
            value.get("decision_ref"),
            label="decision_ref",
        ),
    }


def validate_measured_active_record(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "expansion_authority",
        "pass_authority",
        "human_required_authority",
        "measurement_id",
        "decision_class",
        "source_activation_id",
        "source_receipt_digest",
        "source_result",
        "observed_at",
        "baseline_outcome",
        "active_outcome",
        "cost_delta_milliunits",
        "wall_time_delta_ms",
        "frontier_call_delta",
        "retry_delta",
        "selector_attribution",
        "measurement_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("decision plane measured-active record schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != RECORD_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("expansion_authority") != EXPANSION_AUTHORITY
        or payload.get("pass_authority") != PASS_AUTHORITY
        or payload.get("human_required_authority")
        != HUMAN_REQUIRED_AUTHORITY
        or payload.get("decision_class") not in DECISION_CLASSES
        or payload.get("source_result") not in SOURCE_RESULTS
        or payload.get("baseline_outcome") not in VERIFIED_OUTCOMES
        or payload.get("active_outcome") not in VERIFIED_OUTCOMES
    ):
        _reject("decision plane measured-active record authority/state is invalid")
    _id(payload.get("measurement_id"), label="measurement_id")
    _id(
        payload.get("source_activation_id"),
        label="source_activation_id",
    )
    _digest(
        payload.get("source_receipt_digest"),
        label="source_receipt_digest",
    )
    _utc(payload.get("observed_at"))
    _bounded_int(
        payload.get("cost_delta_milliunits"),
        label="cost_delta_milliunits",
        limit=1_000_000_000,
    )
    _bounded_int(
        payload.get("wall_time_delta_ms"),
        label="wall_time_delta_ms",
        limit=86_400_000,
    )
    _bounded_int(
        payload.get("frontier_call_delta"),
        label="frontier_call_delta",
        limit=1_000_000,
    )
    _bounded_int(
        payload.get("retry_delta"),
        label="retry_delta",
        limit=1_000_000,
    )
    attribution = payload.get("selector_attribution")
    if payload["source_result"] == "APPLIED_CANARY":
        if attribution is None:
            _reject(
                "decision plane measured-active applied source "
                "requires selector attribution"
            )
        if not isinstance(attribution, dict) or set(attribution) != {
            "provider",
            "model",
            "profile",
            "decision_ref",
        }:
            _reject(
                "decision plane measured-active selector attribution is invalid"
            )
        for key in ("provider", "model", "profile", "decision_ref"):
            _id(attribution.get(key), label=key)
    elif attribution is not None:
        _reject(
            "decision plane measured-active non-applied source "
            "cannot retain selector attribution"
        )
    digest = _digest(
        payload.get("measurement_digest"),
        label="measurement_digest",
    )
    body = {
        key: value
        for key, value in payload.items()
        if key != "measurement_digest"
    }
    if _canonical_digest(body) != digest:
        _reject("decision plane measured-active measurement digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def _load_ledger(root: Path) -> dict[str, object]:
    path = root / FILENAME
    if not path.exists():
        return _empty_ledger()
    payload = _load_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "authority", "records"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or payload.get("authority") != AUTHORITY
        or not isinstance(payload.get("records"), list)
        or len(payload["records"]) > _MAX_RECORDS
    ):
        _reject("decision plane measured-active ledger schema is invalid")
    records = []
    seen_measurements: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    for raw in payload["records"]:
        record = validate_measured_active_record(raw)
        measurement_id = str(record["measurement_id"])
        source_key = (
            str(record["decision_class"]),
            str(record["source_activation_id"]),
        )
        if measurement_id in seen_measurements:
            _reject(
                "decision plane measured-active measurement identity is duplicated"
            )
        if source_key in seen_sources:
            _reject(
                "decision plane measured-active source activation is duplicated"
            )
        seen_measurements.add(measurement_id)
        seen_sources.add(source_key)
        records.append(record)
    return {**_empty_ledger(), "records": records}


def build_measured_active_record(
    data_root: Path,
    observation: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "measurement_id",
        "decision_class",
        "source_activation_id",
        "expected_receipt_digest",
        "observed_at",
        "baseline_outcome",
        "active_outcome",
        "cost_delta_milliunits",
        "wall_time_delta_ms",
        "frontier_call_delta",
        "retry_delta",
    }
    if not isinstance(observation, dict) or set(observation) != expected:
        _reject("decision plane measured-active observation schema is invalid")
    if (
        observation.get("schema_version") != SCHEMA_VERSION
        or isinstance(observation.get("schema_version"), bool)
        or observation.get("kind") != OBSERVATION_KIND
    ):
        _reject(
            "decision plane measured-active observation version/kind is invalid"
        )
    measurement_id = _id(
        observation.get("measurement_id"),
        label="measurement_id",
    )
    decision_class = observation.get("decision_class")
    if decision_class not in DECISION_CLASSES:
        _reject("decision plane measured-active decision class is unsupported")
    activation_id = _id(
        observation.get("source_activation_id"),
        label="source_activation_id",
    )
    receipt_digest = _digest(
        observation.get("expected_receipt_digest"),
        label="expected_receipt_digest",
    )
    source = _source_effect(
        Path(data_root),
        decision_class=str(decision_class),
        activation_id=activation_id,
        expected_receipt_digest=receipt_digest,
    )
    receipt = source["receipt"]
    assert isinstance(receipt, dict)
    attribution = _selector_attribution(receipt)
    source_result = str(source["source_result"])
    if source_result != "APPLIED_CANARY":
        attribution = None

    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECORD_KIND,
        "authority": AUTHORITY,
        "expansion_authority": EXPANSION_AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "human_required_authority": HUMAN_REQUIRED_AUTHORITY,
        "measurement_id": measurement_id,
        "decision_class": decision_class,
        "source_activation_id": activation_id,
        "source_receipt_digest": receipt_digest,
        "source_result": source_result,
        "observed_at": _utc(observation.get("observed_at")),
        "baseline_outcome": observation.get("baseline_outcome"),
        "active_outcome": observation.get("active_outcome"),
        "cost_delta_milliunits": _bounded_int(
            observation.get("cost_delta_milliunits"),
            label="cost_delta_milliunits",
            limit=1_000_000_000,
        ),
        "wall_time_delta_ms": _bounded_int(
            observation.get("wall_time_delta_ms"),
            label="wall_time_delta_ms",
            limit=86_400_000,
        ),
        "frontier_call_delta": _bounded_int(
            observation.get("frontier_call_delta"),
            label="frontier_call_delta",
            limit=1_000_000,
        ),
        "retry_delta": _bounded_int(
            observation.get("retry_delta"),
            label="retry_delta",
            limit=1_000_000,
        ),
        "selector_attribution": attribution,
    }
    if body["baseline_outcome"] not in VERIFIED_OUTCOMES:
        _reject("decision plane measured-active baseline outcome is invalid")
    if body["active_outcome"] not in VERIFIED_OUTCOMES:
        _reject("decision plane measured-active active outcome is invalid")
    record = {
        **body,
        "measurement_digest": _canonical_digest(body),
    }
    return validate_measured_active_record(record)


def record_measured_active_observation(
    data_root: Path,
    observation: object,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("decision plane measured-active data root is not a directory")
    record = build_measured_active_record(root, observation)
    with data_root_write_lock(root):
        # Revalidate the exact source while the append is serialized.
        _source_effect(
            root,
            decision_class=str(record["decision_class"]),
            activation_id=str(record["source_activation_id"]),
            expected_receipt_digest=str(record["source_receipt_digest"]),
        )
        ledger = _load_ledger(root)
        same_measurement = next(
            (
                item
                for item in ledger["records"]
                if item["measurement_id"] == record["measurement_id"]
            ),
            None,
        )
        if same_measurement is not None:
            if (
                same_measurement["measurement_digest"]
                == record["measurement_digest"]
            ):
                return {
                    "state": "DUPLICATE_NOOP",
                    "record": same_measurement,
                    "dashboard": measured_active_dashboard(root),
                }
            _reject("decision plane measured-active measurement replay drifted")
        if any(
            item["decision_class"] == record["decision_class"]
            and item["source_activation_id"]
            == record["source_activation_id"]
            for item in ledger["records"]
        ):
            _reject(
                "decision plane measured-active source activation "
                "already has a measurement"
            )
        if len(ledger["records"]) >= _MAX_RECORDS:
            _reject("decision plane measured-active ledger limit reached")
        ledger["records"].append(record)
        ledger["records"].sort(
            key=lambda item: (
                str(item["observed_at"]),
                str(item["measurement_id"]),
            )
        )
        atomic_write_text(
            root / FILENAME,
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        )
    return {
        "state": "PUBLISHED",
        "record": record,
        "dashboard": measured_active_dashboard(root),
    }


def _class_policy(
    decision_class: str,
    records: list[dict[str, object]],
) -> dict[str, object]:
    class_records = [
        item
        for item in records
        if item["decision_class"] == decision_class
    ]
    applied = [
        item
        for item in class_records
        if item["source_result"] == "APPLIED_CANARY"
    ]
    baseline_success = sum(
        item["baseline_outcome"] == "VERIFIED_SUCCESS"
        for item in applied
    )
    active_success = sum(
        item["active_outcome"] == "VERIFIED_SUCCESS"
        for item in applied
    )
    regressions = sum(
        item["baseline_outcome"] == "VERIFIED_SUCCESS"
        and item["active_outcome"] == "VERIFIED_FAILURE"
        for item in applied
    )
    cost_delta = sum(
        int(item["cost_delta_milliunits"])
        for item in applied
    )
    wall_delta = sum(
        int(item["wall_time_delta_ms"])
        for item in applied
    )
    frontier_delta = sum(
        int(item["frontier_call_delta"])
        for item in applied
    )
    retry_delta = sum(
        int(item["retry_delta"])
        for item in applied
    )
    if regressions:
        effective_state = "ROLLBACK_TO_CURRENT"
        reason = "VERIFIED_QUALITY_REGRESSION"
    elif (
        len(applied) >= MIN_EXPANSION_MEASUREMENTS
        and active_success >= baseline_success
        and (cost_delta < 0 or wall_delta < 0)
    ):
        effective_state = "MEASURED_EXPANSION_ELIGIBLE"
        reason = "NONINFERIOR_WITH_MEASURABLE_REDUCTION"
    else:
        effective_state = "LIMITED_ACTIVE_CONTINUE"
        reason = "INSUFFICIENT_MEASURED_EVIDENCE"
    return {
        "decision_class": decision_class,
        "effective_state": effective_state,
        "reason": reason,
        "measurement_count": len(class_records),
        "applied_measurement_count": len(applied),
        "minimum_expansion_measurements": MIN_EXPANSION_MEASUREMENTS,
        "baseline_success_count": baseline_success,
        "active_success_count": active_success,
        "quality_regression_count": regressions,
        "cost_delta_milliunits": cost_delta,
        "wall_time_delta_ms": wall_delta,
        "frontier_call_delta": frontier_delta,
        "retry_delta": retry_delta,
        "expansion_authority": EXPANSION_AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "human_required_authority": HUMAN_REQUIRED_AUTHORITY,
    }


def decision_class_policy(
    data_root: Path,
    decision_class: str,
) -> dict[str, object]:
    if decision_class not in DECISION_CLASSES:
        _reject("decision plane measured-active decision class is unsupported")
    ledger = _load_ledger(Path(data_root))
    records = list(ledger["records"])
    for item in records:
        if item["decision_class"] != decision_class:
            continue
        _source_effect(
            Path(data_root),
            decision_class=decision_class,
            activation_id=str(item["source_activation_id"]),
            expected_receipt_digest=str(item["source_receipt_digest"]),
        )
    return _class_policy(decision_class, records)


def measured_active_dashboard(
    data_root: Path,
) -> dict[str, object]:
    root = Path(data_root)
    ledger = _load_ledger(root)
    records = list(ledger["records"])
    for item in records:
        _source_effect(
            root,
            decision_class=str(item["decision_class"]),
            activation_id=str(item["source_activation_id"]),
            expected_receipt_digest=str(item["source_receipt_digest"]),
        )
    policies = [
        _class_policy(decision_class, records)
        for decision_class in sorted(DECISION_CLASSES)
    ]
    return {
        "state": "OBSERVED" if records else "UNKNOWN",
        "authority": AUTHORITY,
        "expansion_authority": EXPANSION_AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "human_required_authority": HUMAN_REQUIRED_AUTHORITY,
        "measurement_count": len(records),
        "class_policies": policies,
        "latest_record": records[-1] if records else None,
        "records": records[-100:],
    }
