"""Provider-neutral bounded concurrency execution-cycle orchestration.

This module composes exact-plan authorization with the existing one-shot
parallel dispatch effect and later dispatch-bound join evidence. It adds
plan-level replay protection, but no provider implementation, retry loop,
GitHub mutation, or engineering PASS authority.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from atlas.concurrency_authorization import (
    build_concurrency_dispatch_authorization,
    publish_concurrency_dispatch_authorization,
)
from atlas.concurrency_effect import (
    ConcurrencyDispatchEffectPort,
    commit_concurrency_dispatch_effect,
    get_concurrency_dispatch_effect_entry,
    validate_concurrency_effect_receipt,
)
from atlas.concurrency_join import concurrency_dispatch_join_dashboard
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
RECORD_KIND = "concurrency_execution_cycle"
LEDGER_KIND = "concurrency_execution_cycle_ledger"
FILENAME = "concurrency-executions.json"
AUTHORITY = "ORCHESTRATION_EVIDENCE_ONLY"
PASS_AUTHORITY = "NONE"
_MAX_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 500
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#\-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("concurrency execution ledger contains duplicate JSON keys")
        result[key] = value
    return result


def _id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or _ID.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"concurrency execution {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency execution {label} is invalid")
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
            "concurrency execution payload is not canonical JSON"
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
        _reject("concurrency execution ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            "concurrency execution ledger is unreadable"
        ) from exc
    if len(raw) > _MAX_BYTES:
        _reject("concurrency execution ledger exceeds bounded size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "concurrency execution ledger is invalid JSON"
        ) from exc
    assert_content_free(payload)
    return payload


def validate_concurrency_execution_record(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "pass_authority",
        "cycle_id",
        "plan_digest",
        "authorization_id",
        "authorization_digest",
        "effect_id",
        "assignment_count",
        "state",
        "effect_result",
        "effect_receipt_digest",
        "record_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency execution record schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != RECORD_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("pass_authority") != PASS_AUTHORITY
    ):
        _reject("concurrency execution record authority/version is invalid")
    for key in ("cycle_id", "authorization_id", "effect_id"):
        _id(payload.get(key), label=key)
    for key in ("plan_digest", "authorization_digest"):
        _digest(payload.get(key), label=key)
    count = payload.get("assignment_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 2 <= count <= 64
    ):
        _reject("concurrency execution assignment_count is invalid")
    state = payload.get("state")
    effect_result = payload.get("effect_result")
    receipt_digest = payload.get("effect_receipt_digest")
    if state == "IN_PROGRESS":
        if effect_result is not None or receipt_digest is not None:
            _reject("concurrency execution in-progress result is invalid")
    elif state == "TERMINAL":
        if effect_result not in {
            "DISPATCHED",
            "PARTIAL",
            "FAILED",
            "HUMAN_REQUIRED",
        }:
            _reject("concurrency execution terminal result is invalid")
        _digest(receipt_digest, label="effect_receipt_digest")
    else:
        _reject("concurrency execution state is invalid")
    record_digest = _digest(
        payload.get("record_digest"), label="record_digest"
    )
    body = {
        key: value
        for key, value in payload.items()
        if key != "record_digest"
    }
    if _canonical_digest(body) != record_digest:
        _reject("concurrency execution record digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def _load_ledger(root: Path) -> dict[str, object]:
    path = root / FILENAME
    if path.is_symlink():
        _reject("concurrency execution ledger path is unsafe")
    if not path.exists():
        return _empty_ledger()
    payload = _load_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema_version", "kind", "authority", "records"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or payload.get("authority") != AUTHORITY
        or not isinstance(payload.get("records"), list)
        or len(payload["records"]) > _MAX_RECORDS
    ):
        _reject("concurrency execution ledger schema is invalid")
    records = []
    seen_cycle: set[str] = set()
    seen_plan: set[str] = set()
    seen_auth: set[str] = set()
    seen_effect: set[str] = set()
    for raw in payload["records"]:
        item = validate_concurrency_execution_record(raw)
        cycle_id = str(item["cycle_id"])
        plan_digest = str(item["plan_digest"])
        auth_digest = str(item["authorization_digest"])
        effect_id = str(item["effect_id"])
        if (
            cycle_id in seen_cycle
            or plan_digest in seen_plan
            or auth_digest in seen_auth
            or effect_id in seen_effect
        ):
            _reject("concurrency execution replay identity is duplicated")
        seen_cycle.add(cycle_id)
        seen_plan.add(plan_digest)
        seen_auth.add(auth_digest)
        seen_effect.add(effect_id)
        records.append(item)
    return {**_empty_ledger(), "records": records}


def _make_record(
    *,
    cycle_id: str,
    authorization: dict[str, object],
    effect_id: str,
    state: str,
    effect_result: str | None,
    effect_receipt_digest: str | None,
) -> dict[str, object]:
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECORD_KIND,
        "authority": AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "cycle_id": cycle_id,
        "plan_digest": authorization["plan_digest"],
        "authorization_id": authorization["authorization_id"],
        "authorization_digest": authorization["authorization_digest"],
        "effect_id": effect_id,
        "assignment_count": authorization["assignment_count"],
        "state": state,
        "effect_result": effect_result,
        "effect_receipt_digest": effect_receipt_digest,
    }
    return validate_concurrency_execution_record(
        {**body, "record_digest": _canonical_digest(body)}
    )


def _reserve(
    root: Path,
    *,
    cycle_id: str,
    authorization: dict[str, object],
    effect_id: str,
) -> dict[str, object]:
    ledger = _load_ledger(root)
    if len(ledger["records"]) >= _MAX_RECORDS:
        _reject("concurrency execution ledger limit reached")
    plan_digest = str(authorization["plan_digest"])
    auth_digest = str(authorization["authorization_digest"])
    if any(
        item["cycle_id"] == cycle_id
        or item["plan_digest"] == plan_digest
        or item["authorization_digest"] == auth_digest
        or item["effect_id"] == effect_id
        for item in ledger["records"]
    ):
        _reject("concurrency execution replay is not allowed")
    record = _make_record(
        cycle_id=cycle_id,
        authorization=authorization,
        effect_id=effect_id,
        state="IN_PROGRESS",
        effect_result=None,
        effect_receipt_digest=None,
    )
    ledger["records"].append(record)
    atomic_write_text(
        root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )
    return record


def _finalize(
    root: Path,
    *,
    cycle_id: str,
    authorization: dict[str, object],
    effect_id: str,
    effect_result: str,
    effect_receipt_digest: str,
) -> dict[str, object]:
    ledger = _load_ledger(root)
    matches = [
        item
        for item in ledger["records"]
        if item["cycle_id"] == cycle_id
    ]
    if len(matches) != 1 or matches[0]["state"] != "IN_PROGRESS":
        _reject("concurrency execution reservation is not current")
    current = matches[0]
    if (
        current["plan_digest"] != authorization["plan_digest"]
        or current["authorization_digest"]
        != authorization["authorization_digest"]
        or current["effect_id"] != effect_id
    ):
        _reject("concurrency execution reservation binding drifted")
    record = _make_record(
        cycle_id=cycle_id,
        authorization=authorization,
        effect_id=effect_id,
        state="TERMINAL",
        effect_result=effect_result,
        effect_receipt_digest=effect_receipt_digest,
    )
    ledger["records"] = [
        record if item["cycle_id"] == cycle_id else item
        for item in ledger["records"]
    ]
    atomic_write_text(
        root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )
    return record


def start_concurrency_execution(
    data_root: Path,
    *,
    cycle_id: str,
    authorization_request: object,
    effect_id: str,
    effect_port: ConcurrencyDispatchEffectPort,
) -> dict[str, object]:
    """Authorize, reserve, and dispatch one exact current multi-node plan."""
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("concurrency execution data root is not a directory")
    cycle = _id(cycle_id, label="cycle_id")
    effect = _id(effect_id, label="effect_id")

    authorization = build_concurrency_dispatch_authorization(
        root, authorization_request
    )
    with data_root_write_lock(root):
        _reserve(
            root,
            cycle_id=cycle,
            authorization=authorization,
            effect_id=effect,
        )

    published = publish_concurrency_dispatch_authorization(
        root, authorization_request
    )
    if (
        published.get("binding_state") != "CURRENT"
        or published.get("authorization") != authorization
    ):
        _reject(
            "concurrency execution authorization changed after reservation"
        )

    receipt = commit_concurrency_dispatch_effect(
        root,
        effect_id=effect,
        expected_authorization_digest=str(
            authorization["authorization_digest"]
        ),
        effect_port=effect_port,
    )
    receipt_digest = _digest(
        receipt.get("receipt_digest"),
        label="effect_receipt_digest",
    )
    with data_root_write_lock(root):
        _finalize(
            root,
            cycle_id=cycle,
            authorization=authorization,
            effect_id=effect,
            effect_result=str(receipt["result"]),
            effect_receipt_digest=receipt_digest,
        )
    return concurrency_execution_dashboard(root)


def _validate_terminal_binding(
    root: Path,
    record: dict[str, object],
) -> dict[str, object]:
    entry = get_concurrency_dispatch_effect_entry(
        root, str(record["effect_id"])
    )
    if entry.get("state") != "TERMINAL":
        _reject("concurrency execution terminal effect is not terminal")
    if entry.get("authorization_digest") != record["authorization_digest"]:
        _reject(
            "concurrency execution effect authorization binding is invalid"
        )
    if entry.get("receipt_digest") != record["effect_receipt_digest"]:
        _reject("concurrency execution effect receipt binding is invalid")
    receipt = validate_concurrency_effect_receipt(
        entry.get("receipt"),
        expected_receipt_digest=str(record["effect_receipt_digest"]),
        expected_authorization_digest=str(
            record["authorization_digest"]
        ),
    )
    if (
        receipt["effect_id"] != record["effect_id"]
        or receipt["authorization_id"] != record["authorization_id"]
        or receipt["assignment_count"] != record["assignment_count"]
        or receipt["result"] != record["effect_result"]
    ):
        _reject(
            "concurrency execution effect receipt attribution is invalid"
        )
    return receipt


def concurrency_execution_dashboard(
    data_root: Path,
) -> dict[str, object]:
    root = Path(data_root)
    ledger = _load_ledger(root)
    records = list(ledger["records"])
    joins = concurrency_dispatch_join_dashboard(root)
    join_by_receipt = {
        str(item["effect_receipt_digest"]): item
        for item in joins["joins"]
    }

    rendered: list[dict[str, object]] = []
    stage_counts = {
        "IN_PROGRESS": 0,
        "AWAITING_JOIN": 0,
        "COMPLETE": 0,
        "PARTIAL": 0,
        "FAILED": 0,
        "HUMAN_REQUIRED": 0,
    }
    for record in records:
        stage = "IN_PROGRESS"
        join = None
        if record["state"] == "TERMINAL":
            _validate_terminal_binding(root, record)
            effect_result = str(record["effect_result"])
            if effect_result == "FAILED":
                stage = "FAILED"
            elif effect_result == "HUMAN_REQUIRED":
                stage = "HUMAN_REQUIRED"
            else:
                join = join_by_receipt.get(
                    str(record["effect_receipt_digest"])
                )
                if join is None:
                    stage = "AWAITING_JOIN"
                else:
                    join_result = str(join["result"])
                    if join_result == "HUMAN_REQUIRED":
                        stage = "HUMAN_REQUIRED"
                    elif join_result == "FAILED":
                        stage = "FAILED"
                    elif effect_result == "PARTIAL" or join_result == "PARTIAL":
                        stage = "PARTIAL"
                    else:
                        stage = "COMPLETE"
        stage_counts[stage] += 1
        rendered.append(
            {
                **record,
                "stage": stage,
                "join_digest": (
                    join["join_digest"]
                    if isinstance(join, dict)
                    else None
                ),
                "join_result": (
                    join["result"]
                    if isinstance(join, dict)
                    else None
                ),
            }
        )
    latest = rendered[-1] if rendered else None
    return {
        "state": "OBSERVED" if records else "UNKNOWN",
        "authority": AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "execution_count": len(records),
        "stage_counts": stage_counts,
        "latest_execution": latest,
        "executions": rendered[-50:],
    }
