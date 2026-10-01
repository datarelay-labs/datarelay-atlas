"""Dispatch-bound multi-node join/reconciliation evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from atlas.concurrency_effect import (
    get_concurrency_dispatch_effect_entry,
    validate_concurrency_effect_receipt,
)
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
OBSERVATION_KIND = "concurrency_dispatch_join_observation"
LEDGER_KIND = "concurrency_dispatch_join_ledger"
FILENAME = "concurrency-dispatch-joins.json"
AUTHORITY = "MEASUREMENT_ONLY"
PASS_AUTHORITY = "MEASUREMENT_ONLY"
OUTCOMES = frozenset({"COMPLETE", "FAILED", "HUMAN_REQUIRED"})
RESULTS = frozenset({"PASS", "PARTIAL", "FAILED", "HUMAN_REQUIRED"})
_MAX_BYTES = 2 * 1024 * 1024
_MAX_JOINS = 500
_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#\-]{0,255}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")


def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("concurrency join ledger contains duplicate JSON keys")
        result[key] = value
    return result


def _id(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER if provider else _ID
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"concurrency join {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency join {label} is invalid")
    return value


def _utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject(f"concurrency join {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"concurrency join {label} must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"concurrency join {label} must be UTC")
    return value


def _bounded_int(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        _reject(f"concurrency join {label} is invalid")
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
        raise ValidationError("concurrency join payload is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "authority": AUTHORITY,
        "joins": [],
    }


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        _reject("concurrency join ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("concurrency join ledger is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject("concurrency join ledger exceeds bounded size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("concurrency join ledger is invalid JSON") from exc
    assert_content_free(payload)
    return payload


def _load_ledger(root: Path) -> dict[str, object]:
    path = root / FILENAME
    if path.is_symlink():
        _reject("concurrency join ledger path is unsafe")
    if not path.exists():
        return _empty_ledger()
    payload = _load_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "authority", "joins"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or payload.get("authority") != AUTHORITY
        or not isinstance(payload.get("joins"), list)
        or len(payload["joins"]) > _MAX_JOINS
    ):
        _reject("concurrency join ledger schema is invalid")
    joins = []
    seen_ids: set[str] = set()
    seen_effects: set[str] = set()
    for item in payload["joins"]:
        normalized = validate_concurrency_dispatch_join(item)
        _validate_join_binding(root, normalized)
        join_id = str(normalized["join_id"])
        receipt_digest = str(normalized["effect_receipt_digest"])
        if join_id in seen_ids or receipt_digest in seen_effects:
            _reject("concurrency join ledger replay identity is duplicated")
        seen_ids.add(join_id)
        seen_effects.add(receipt_digest)
        joins.append(normalized)
    return {**_empty_ledger(), "joins": joins}


def validate_concurrency_dispatch_join(payload: object) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "join_id", "effect_id",
        "effect_receipt_digest", "authorization_digest", "observed_at",
        "effect_result", "result", "pass_authority", "dispatched_count",
        "dispatch_omission_count", "complete_count", "failed_count",
        "human_required_count", "outcomes", "dispatch_omissions", "join_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency dispatch join schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != OBSERVATION_KIND
        or payload.get("result") not in RESULTS
        or payload.get("pass_authority") != PASS_AUTHORITY
        or payload.get("effect_result") not in {"DISPATCHED", "PARTIAL"}
    ):
        _reject("concurrency dispatch join identity/result is invalid")
    _id(payload.get("join_id"), label="join_id")
    _id(payload.get("effect_id"), label="effect_id")
    _digest(payload.get("effect_receipt_digest"), label="effect_receipt_digest")
    _digest(payload.get("authorization_digest"), label="authorization_digest")
    _utc(payload.get("observed_at"), label="observed_at")
    outcomes = payload.get("outcomes")
    omissions = payload.get("dispatch_omissions")
    if not isinstance(outcomes, list) or not outcomes:
        _reject("concurrency dispatch join outcomes are invalid")
    if not isinstance(omissions, list):
        _reject("concurrency dispatch join omissions are invalid")
    node_ids: set[str] = set()
    counts = {"COMPLETE": 0, "FAILED": 0, "HUMAN_REQUIRED": 0}
    for item in outcomes:
        expected_keys = {
            "node_id", "slot_id", "worker_id", "provider", "runtime",
            "route_id", "head", "outcome", "duration_ms", "evidence_ref",
        }
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency dispatch join outcome schema is invalid")
        node_id = _id(item.get("node_id"), label="node_id")
        if node_id in node_ids:
            _reject("concurrency dispatch join outcome node is duplicated")
        node_ids.add(node_id)
        for key in ("slot_id", "worker_id", "runtime", "head", "evidence_ref"):
            _id(item.get(key), label=key)
        _id(item.get("provider"), label="provider", provider=True)
        route = item.get("route_id")
        if route is not None:
            _id(route, label="route_id")
        outcome = item.get("outcome")
        if outcome not in OUTCOMES:
            _reject("concurrency dispatch join outcome is invalid")
        _bounded_int(item.get("duration_ms"), label="duration_ms", maximum=_MAX_DURATION_MS)
        counts[str(outcome)] += 1
    omission_nodes: set[str] = set()
    for item in omissions:
        expected_keys = {
            "node_id", "slot_id", "worker_id", "provider", "runtime",
            "route_id", "head", "dispatch_result",
        }
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency dispatch join omission schema is invalid")
        node_id = _id(item.get("node_id"), label="omission node_id")
        if node_id in node_ids or node_id in omission_nodes:
            _reject("concurrency dispatch join omission node is duplicated")
        omission_nodes.add(node_id)
        for key in ("slot_id", "worker_id", "runtime", "head"):
            _id(item.get(key), label=key)
        _id(item.get("provider"), label="provider", provider=True)
        route = item.get("route_id")
        if route is not None:
            _id(route, label="route_id")
        if item.get("dispatch_result") not in {"REFUSED", "HUMAN_REQUIRED", "ERROR"}:
            _reject("concurrency dispatch join omission result is invalid")
    reported = {
        "COMPLETE": payload.get("complete_count"),
        "FAILED": payload.get("failed_count"),
        "HUMAN_REQUIRED": payload.get("human_required_count"),
    }
    if reported != counts:
        _reject("concurrency dispatch join counts are inconsistent")
    if payload.get("dispatched_count") != len(outcomes):
        _reject("concurrency dispatch join dispatched_count is inconsistent")
    if payload.get("dispatch_omission_count") != len(omissions):
        _reject("concurrency dispatch join omission count is inconsistent")
    expected_result = (
        "HUMAN_REQUIRED"
        if counts["HUMAN_REQUIRED"]
        else (
            "PASS"
            if counts["COMPLETE"] == len(outcomes)
            else ("PARTIAL" if counts["COMPLETE"] else "FAILED")
        )
    )
    if payload.get("result") != expected_result:
        _reject("concurrency dispatch join aggregate result is inconsistent")
    digest = _digest(payload.get("join_digest"), label="join_digest")
    body = {key: value for key, value in payload.items() if key != "join_digest"}
    if _canonical_digest(body) != digest:
        _reject("concurrency dispatch join digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def _effect_receipt(
    root: Path,
    *,
    effect_id: str,
    expected_receipt_digest: str,
    expected_authorization_digest: str,
) -> dict[str, object]:
    entry = get_concurrency_dispatch_effect_entry(root, effect_id)
    if entry.get("state") != "TERMINAL":
        _reject("concurrency join effect is not terminal")
    receipt_digest = _digest(
        entry.get("receipt_digest"), label="effect receipt digest"
    )
    if receipt_digest != expected_receipt_digest:
        _reject("concurrency join effect receipt digest mismatch")
    receipt = validate_concurrency_effect_receipt(
        entry.get("receipt"),
        expected_receipt_digest=receipt_digest,
        expected_authorization_digest=expected_authorization_digest,
    )
    if receipt["authorization_digest"] != expected_authorization_digest:
        _reject("concurrency join authorization digest mismatch")
    if receipt["result"] not in {"DISPATCHED", "PARTIAL"}:
        _reject("concurrency join cannot consume unsuccessful dispatch effect")
    return receipt


def _validate_join_binding(
    root: Path,
    join: dict[str, object],
) -> None:
    receipt = _effect_receipt(
        root,
        effect_id=str(join["effect_id"]),
        expected_receipt_digest=str(join["effect_receipt_digest"]),
        expected_authorization_digest=str(join["authorization_digest"]),
    )
    if receipt["effect_id"] != join["effect_id"]:
        _reject("concurrency join effect id does not match dispatch receipt")
    if receipt["authorization_digest"] != join["authorization_digest"]:
        _reject("concurrency join authorization does not match dispatch receipt")
    if join["effect_result"] != receipt["result"]:
        _reject("concurrency join effect result does not match dispatch receipt")
    if join["dispatched_count"] != receipt["dispatched_count"]:
        _reject("concurrency join dispatched count does not match dispatch receipt")
    expected_omission_count = int(receipt["assignment_count"]) - int(receipt["dispatched_count"])
    if join["dispatch_omission_count"] != expected_omission_count:
        _reject("concurrency join omission count does not match dispatch receipt")
    dispatched = {
        str(item["node_id"]): item
        for item in receipt["receipts"]
        if item["result"] == "DISPATCHED"
    }
    outcomes = {
        str(item["node_id"]): item
        for item in join["outcomes"]
    }
    if set(dispatched) != set(outcomes):
        _reject("concurrency join outcomes do not match dispatched assignments")
    for node_id, outcome in outcomes.items():
        source = dispatched[node_id]
        for key in ("slot_id", "worker_id", "provider", "runtime", "route_id", "head"):
            if outcome.get(key) != source.get(key):
                _reject("concurrency join stored attribution does not match dispatch")
    expected_omissions = [
        {
            "node_id": item["node_id"],
            "slot_id": item["slot_id"],
            "worker_id": item["worker_id"],
            "provider": item["provider"],
            "runtime": item["runtime"],
            "route_id": item["route_id"],
            "head": item["head"],
            "dispatch_result": item["result"],
        }
        for item in receipt["receipts"]
        if item["result"] != "DISPATCHED"
    ]
    if join["dispatch_omissions"] != expected_omissions:
        _reject("concurrency join omissions do not match dispatch receipt")


def build_concurrency_dispatch_join(
    data_root: Path,
    observation: object,
) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "join_id", "effect_id",
        "effect_receipt_digest", "authorization_digest", "observed_at", "outcomes",
    }
    if not isinstance(observation, dict) or set(observation) != expected:
        _reject("concurrency dispatch join observation schema is invalid")
    if observation.get("schema_version") != SCHEMA_VERSION or observation.get("kind") != OBSERVATION_KIND:
        _reject("concurrency dispatch join observation version/kind is invalid")
    join_id = _id(observation.get("join_id"), label="join_id")
    effect_id = _id(observation.get("effect_id"), label="effect_id")
    receipt_digest = _digest(
        observation.get("effect_receipt_digest"), label="effect_receipt_digest"
    )
    authorization_digest = _digest(
        observation.get("authorization_digest"), label="authorization_digest"
    )
    observed_at = _utc(observation.get("observed_at"), label="observed_at")
    receipt = _effect_receipt(
        Path(data_root),
        effect_id=effect_id,
        expected_receipt_digest=receipt_digest,
        expected_authorization_digest=authorization_digest,
    )
    dispatched = {
        str(item["node_id"]): item
        for item in receipt["receipts"]
        if item["result"] == "DISPATCHED"
    }
    omissions = [
        {
            "node_id": item["node_id"],
            "slot_id": item["slot_id"],
            "worker_id": item["worker_id"],
            "provider": item["provider"],
            "runtime": item["runtime"],
            "route_id": item["route_id"],
            "head": item["head"],
            "dispatch_result": item["result"],
        }
        for item in receipt["receipts"]
        if item["result"] != "DISPATCHED"
    ]
    raw_outcomes = observation.get("outcomes")
    if not isinstance(raw_outcomes, list) or len(raw_outcomes) != len(dispatched):
        _reject("concurrency join requires one outcome per dispatched assignment")
    normalized = []
    seen: set[str] = set()
    for item in raw_outcomes:
        expected_keys = {
            "node_id", "slot_id", "worker_id", "provider", "runtime",
            "route_id", "head", "outcome", "duration_ms", "evidence_ref",
        }
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency join outcome schema is invalid")
        node_id = _id(item.get("node_id"), label="node_id")
        source = dispatched.get(node_id)
        if source is None or node_id in seen:
            _reject("concurrency join outcome is not a uniquely dispatched assignment")
        seen.add(node_id)
        for key in ("slot_id", "worker_id", "provider", "runtime", "route_id", "head"):
            if item.get(key) != source.get(key):
                _reject("concurrency join outcome attribution does not match dispatch")
        outcome = item.get("outcome")
        if outcome not in OUTCOMES:
            _reject("concurrency join outcome is invalid")
        normalized.append(
            {
                "node_id": node_id,
                "slot_id": source["slot_id"],
                "worker_id": source["worker_id"],
                "provider": source["provider"],
                "runtime": source["runtime"],
                "route_id": source["route_id"],
                "head": source["head"],
                "outcome": outcome,
                "duration_ms": _bounded_int(
                    item.get("duration_ms"), label="duration_ms", maximum=_MAX_DURATION_MS
                ),
                "evidence_ref": _id(item.get("evidence_ref"), label="evidence_ref"),
            }
        )
    normalized.sort(key=lambda item: str(item["node_id"]))
    counts = {
        outcome: sum(item["outcome"] == outcome for item in normalized)
        for outcome in OUTCOMES
    }
    result = (
        "HUMAN_REQUIRED"
        if counts["HUMAN_REQUIRED"]
        else (
            "PASS"
            if counts["COMPLETE"] == len(normalized)
            else ("PARTIAL" if counts["COMPLETE"] else "FAILED")
        )
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": OBSERVATION_KIND,
        "join_id": join_id,
        "effect_id": effect_id,
        "effect_receipt_digest": receipt_digest,
        "authorization_digest": authorization_digest,
        "observed_at": observed_at,
        "effect_result": receipt["result"],
        "result": result,
        "pass_authority": PASS_AUTHORITY,
        "dispatched_count": len(normalized),
        "dispatch_omission_count": len(omissions),
        "complete_count": counts["COMPLETE"],
        "failed_count": counts["FAILED"],
        "human_required_count": counts["HUMAN_REQUIRED"],
        "outcomes": normalized,
        "dispatch_omissions": omissions,
    }
    return {**body, "join_digest": _canonical_digest(body)}


def record_concurrency_dispatch_join(
    data_root: Path,
    observation: object,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("concurrency join data root is not a directory")
    join = validate_concurrency_dispatch_join(
        build_concurrency_dispatch_join(root, observation)
    )
    with data_root_write_lock(root):
        ledger = _load_ledger(root)
        if len(ledger["joins"]) >= _MAX_JOINS:
            _reject("concurrency join ledger limit reached")
        if any(
            item["join_id"] == join["join_id"]
            or item["effect_receipt_digest"] == join["effect_receipt_digest"]
            for item in ledger["joins"]
        ):
            _reject("concurrency dispatch join replay is not allowed")
        ledger["joins"].append(join)
        ledger["joins"].sort(
            key=lambda item: (str(item["observed_at"]), str(item["join_id"]))
        )
        atomic_write_text(
            root / FILENAME,
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        )
    return concurrency_dispatch_join_dashboard(root)


def get_concurrency_dispatch_join_entry(
    data_root: Path,
    join_digest: str,
) -> dict[str, object]:
    """Return one fully validated dispatch join by exact content digest."""
    digest = _digest(join_digest, label="join_digest")
    ledger = _load_ledger(Path(data_root))
    matches = [
        item for item in ledger["joins"]
        if item.get("join_digest") == digest
    ]
    if len(matches) != 1:
        _reject("concurrency dispatch join is not found")
    return dict(matches[0])


def concurrency_dispatch_join_dashboard(data_root: Path) -> dict[str, object]:
    ledger = _load_ledger(Path(data_root))
    joins = list(ledger["joins"])
    counts = {name: 0 for name in RESULTS}
    for item in joins:
        counts[str(item["result"])] += 1
    return {
        "state": "OBSERVED" if joins else "UNKNOWN",
        "authority": AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "join_count": len(joins),
        "result_counts": counts,
        "latest_join": joins[-1] if joins else None,
        "joins": joins[-50:],
    }
