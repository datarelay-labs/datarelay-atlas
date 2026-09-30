"""One-shot provider-neutral multi-node dispatch effect boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol

from atlas.concurrency_authorization import (
    concurrency_dispatch_authorization_dashboard,
    validate_concurrency_dispatch_authorization,
)
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
LEDGER_KIND = "concurrency_dispatch_effect_ledger"
RECEIPT_KIND = "concurrency_dispatch_effect_receipt"
FILENAME = "concurrency-dispatch-effects.json"
AUTHORITY = "DISPATCH_EFFECT_RECEIPT_ONLY"
PORT_RESULTS = frozenset({"DISPATCHED", "REFUSED", "HUMAN_REQUIRED"})
OVERALL_RESULTS = frozenset({"DISPATCHED", "PARTIAL", "FAILED", "HUMAN_REQUIRED"})
_MAX_BYTES = 2 * 1024 * 1024
_MAX_EFFECTS = 500
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#\-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")


class ConcurrencyDispatchEffectPort(Protocol):
    def dispatch(self, request: dict[str, Any]) -> object: ...


def _reject(message: str) -> None:
    raise ValidationError(message)


def _identity(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or _ID.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"concurrency effect {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency effect {label} is invalid")
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
        raise ValidationError("concurrency effect payload is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "authority": AUTHORITY,
        "effects": [],
    }


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        _reject("concurrency effect ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("concurrency effect ledger is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject("concurrency effect ledger exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("concurrency effect ledger is invalid JSON") from exc
    assert_content_free(payload)
    return payload


def _load_ledger(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return _empty_ledger()
    payload = _load_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "authority", "effects"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or payload.get("authority") != AUTHORITY
        or not isinstance(payload.get("effects"), list)
        or len(payload["effects"]) > _MAX_EFFECTS
    ):
        _reject("concurrency effect ledger schema is invalid")
    seen_effects: set[str] = set()
    seen_authorizations: set[str] = set()
    effects: list[dict[str, object]] = []
    expected_keys = {
        "effect_id",
        "authorization_digest",
        "authorization_id",
        "state",
        "assignment_count",
        "receipt",
        "receipt_digest",
    }
    for item in payload["effects"]:
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency effect ledger entry is invalid")
        effect_id = _identity(item.get("effect_id"), label="effect_id")
        auth_digest = _digest(
            item.get("authorization_digest"), label="authorization_digest"
        )
        authorization_id = _identity(
            item.get("authorization_id"), label="authorization_id"
        )
        assignment_count = item.get("assignment_count")
        if (
            isinstance(assignment_count, bool)
            or not isinstance(assignment_count, int)
            or assignment_count < 2
            or assignment_count > 64
        ):
            _reject("concurrency effect ledger assignment_count is invalid")
        if effect_id in seen_effects or auth_digest in seen_authorizations:
            _reject("concurrency effect ledger replay identity is duplicated")
        state = item.get("state")
        if state == "IN_PROGRESS":
            if item.get("receipt") is not None or item.get("receipt_digest") is not None:
                _reject("concurrency effect in-progress reservation is invalid")
        elif state == "TERMINAL":
            receipt_digest = _digest(
                item.get("receipt_digest"), label="receipt_digest"
            )
            validated = validate_concurrency_effect_receipt(
                item.get("receipt"),
                expected_receipt_digest=receipt_digest,
                expected_authorization_digest=auth_digest,
            )
            if (
                validated["effect_id"] != effect_id
                or validated["authorization_id"] != authorization_id
                or validated["assignment_count"] != assignment_count
            ):
                _reject("concurrency effect terminal ledger binding is invalid")
        else:
            _reject("concurrency effect ledger state is invalid")
        seen_effects.add(effect_id)
        seen_authorizations.add(auth_digest)
        effects.append(dict(item))
    return {**_empty_ledger(), "effects": effects}


def _replay_key(authorization_digest: str, node_id: str) -> str:
    return hashlib.sha256(
        f"{authorization_digest}:{node_id}".encode("utf-8")
    ).hexdigest()


def concurrency_effect_receipt_digest(payload: object) -> str:
    return _canonical_digest(payload)


def validate_concurrency_effect_receipt(
    payload: object,
    *,
    expected_receipt_digest: str,
    expected_authorization_digest: str,
) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "effect_id", "authorization_digest",
        "authorization_id", "result", "assignment_count", "dispatched_count",
        "refused_count", "human_required_count", "error_count", "receipts",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency effect receipt schema is invalid")
    receipt_digest = _digest(expected_receipt_digest, label="receipt_digest")
    auth_digest = _digest(
        expected_authorization_digest, label="expected_authorization_digest"
    )
    if concurrency_effect_receipt_digest(payload) != receipt_digest:
        _reject("concurrency effect receipt digest mismatch")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != RECEIPT_KIND
        or payload.get("result") not in OVERALL_RESULTS
        or payload.get("authorization_digest") != auth_digest
    ):
        _reject("concurrency effect receipt identity/result is invalid")
    _identity(payload.get("effect_id"), label="effect_id")
    _identity(payload.get("authorization_id"), label="authorization_id")
    receipts = payload.get("receipts")
    assignment_count = payload.get("assignment_count")
    if (
        isinstance(assignment_count, bool)
        or not isinstance(assignment_count, int)
        or assignment_count < 2
        or not isinstance(receipts, list)
        or len(receipts) != assignment_count
    ):
        _reject("concurrency effect receipt assignments are invalid")
    node_ids: set[str] = set()
    counts = {"DISPATCHED": 0, "REFUSED": 0, "HUMAN_REQUIRED": 0, "ERROR": 0}
    for item in receipts:
        expected_keys = {
            "node_id", "slot_id", "worker_id", "provider", "runtime", "route_id",
            "head", "replay_key", "result", "dispatch_ref",
        }
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency effect assignment receipt schema is invalid")
        node_id = _identity(item.get("node_id"), label="node_id")
        if node_id in node_ids:
            _reject("concurrency effect receipt node is duplicated")
        node_ids.add(node_id)
        for key in ("slot_id", "worker_id", "provider", "runtime", "head", "replay_key"):
            _identity(item.get(key), label=key)
        route_id = item.get("route_id")
        if route_id is not None:
            _identity(route_id, label="route_id")
        result = item.get("result")
        if result not in counts:
            _reject("concurrency effect assignment result is invalid")
        dispatch_ref = item.get("dispatch_ref")
        if result == "DISPATCHED":
            _identity(dispatch_ref, label="dispatch_ref")
        elif dispatch_ref is not None:
            _reject("non-dispatched assignment cannot claim dispatch_ref")
        counts[result] += 1
    reported = {
        "DISPATCHED": payload.get("dispatched_count"),
        "REFUSED": payload.get("refused_count"),
        "HUMAN_REQUIRED": payload.get("human_required_count"),
        "ERROR": payload.get("error_count"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in reported.values()
    ) or reported != counts:
        _reject("concurrency effect receipt counts are inconsistent")
    result = payload["result"]
    expected_result = (
        "HUMAN_REQUIRED"
        if counts["HUMAN_REQUIRED"]
        else (
            "DISPATCHED"
            if counts["DISPATCHED"] == assignment_count
            else (
                "PARTIAL"
                if counts["DISPATCHED"]
                else "FAILED"
            )
        )
    )
    if result != expected_result:
        _reject("concurrency effect receipt overall result is inconsistent")
    return dict(payload)


def _normalize_port_result(
    assignment: dict[str, object],
    authorization_digest: str,
    raw: object,
) -> dict[str, object]:
    result = "ERROR"
    dispatch_ref = None
    if isinstance(raw, dict) and set(raw) == {"result", "dispatch_ref"}:
        candidate = raw.get("result")
        candidate_ref = raw.get("dispatch_ref")
        if candidate in PORT_RESULTS:
            if candidate == "DISPATCHED":
                try:
                    dispatch_ref = _identity(candidate_ref, label="dispatch_ref")
                except ValidationError:
                    result = "ERROR"
                else:
                    result = "DISPATCHED"
            elif candidate_ref is None:
                result = str(candidate)
    return {
        "node_id": assignment["node_id"],
        "slot_id": assignment["slot_id"],
        "worker_id": assignment["worker_id"],
        "provider": assignment["provider"],
        "runtime": assignment["runtime"],
        "route_id": assignment["route_id"],
        "head": assignment["head"],
        "replay_key": _replay_key(
            authorization_digest,
            str(assignment["node_id"]),
        ),
        "result": result,
        "dispatch_ref": dispatch_ref,
    }


def _reserve_effect(
    data_root: Path,
    *,
    effect_id: str,
    authorization: dict[str, object],
) -> None:
    ledger = _load_ledger(data_root)
    if len(ledger["effects"]) >= _MAX_EFFECTS:
        _reject("concurrency effect ledger limit reached")
    auth_digest = str(authorization["authorization_digest"])
    if any(
        item.get("effect_id") == effect_id
        or item.get("authorization_digest") == auth_digest
        for item in ledger["effects"]
    ):
        _reject("concurrency dispatch effect replay is not allowed")
    ledger["effects"].append(
        {
            "effect_id": effect_id,
            "authorization_digest": auth_digest,
            "authorization_id": authorization["authorization_id"],
            "state": "IN_PROGRESS",
            "assignment_count": authorization["assignment_count"],
            "receipt": None,
            "receipt_digest": None,
        }
    )
    atomic_write_text(
        data_root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )


def _finalize_effect(
    data_root: Path,
    *,
    effect_id: str,
    receipt: dict[str, object],
) -> None:
    ledger = _load_ledger(data_root)
    matches = [
        item for item in ledger["effects"]
        if item.get("effect_id") == effect_id
    ]
    if len(matches) != 1 or matches[0].get("state") != "IN_PROGRESS":
        _reject("concurrency effect reservation is not current")
    digest = concurrency_effect_receipt_digest(receipt)
    validate_concurrency_effect_receipt(
        receipt,
        expected_receipt_digest=digest,
        expected_authorization_digest=str(receipt["authorization_digest"]),
    )
    updated = []
    for item in ledger["effects"]:
        if item.get("effect_id") == effect_id:
            updated.append(
                {
                    "effect_id": effect_id,
                    "authorization_digest": receipt["authorization_digest"],
                    "authorization_id": receipt["authorization_id"],
                    "state": "TERMINAL",
                    "assignment_count": receipt["assignment_count"],
                    "receipt": receipt,
                    "receipt_digest": digest,
                }
            )
        else:
            updated.append(item)
    ledger["effects"] = updated
    atomic_write_text(
        data_root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )


def commit_concurrency_dispatch_effect(
    data_root: Path,
    *,
    effect_id: str,
    expected_authorization_digest: str,
    effect_port: ConcurrencyDispatchEffectPort,
) -> dict[str, object]:
    """Reserve one authorization, call each assignment once, and record receipts."""
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("concurrency effect data root is not a directory")
    effect_identity = _identity(effect_id, label="effect_id")
    trusted_auth_digest = _digest(
        expected_authorization_digest,
        label="expected_authorization_digest",
    )

    with data_root_write_lock(root):
        dashboard = concurrency_dispatch_authorization_dashboard(root)
        if dashboard["binding_state"] != "CURRENT":
            _reject("concurrency dispatch authorization is not current")
        authorization = dashboard["authorization"]
        if not isinstance(authorization, dict):
            _reject("concurrency dispatch authorization is unavailable")
        authorization = validate_concurrency_dispatch_authorization(authorization)
        if authorization["authorization_digest"] != trusted_auth_digest:
            _reject("concurrency dispatch authorization digest mismatch")
        _reserve_effect(
            root,
            effect_id=effect_identity,
            authorization=authorization,
        )

    receipts: list[dict[str, object]] = []
    for assignment in authorization["assignments"]:
        call = {
            "effect_id": effect_identity,
            "authorization_id": authorization["authorization_id"],
            "authorization_digest": trusted_auth_digest,
            "replay_key": _replay_key(
                trusted_auth_digest,
                str(assignment["node_id"]),
            ),
            "assignment": dict(assignment),
        }
        try:
            raw = effect_port.dispatch(call)
        except Exception:
            raw = {"result": "ERROR", "dispatch_ref": None}
        receipts.append(
            _normalize_port_result(
                dict(assignment),
                trusted_auth_digest,
                raw,
            )
        )

    counts = {
        name: sum(item["result"] == name for item in receipts)
        for name in ("DISPATCHED", "REFUSED", "HUMAN_REQUIRED", "ERROR")
    }
    result = (
        "HUMAN_REQUIRED"
        if counts["HUMAN_REQUIRED"]
        else (
            "DISPATCHED"
            if counts["DISPATCHED"] == len(receipts)
            else ("PARTIAL" if counts["DISPATCHED"] else "FAILED")
        )
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "effect_id": effect_identity,
        "authorization_digest": trusted_auth_digest,
        "authorization_id": authorization["authorization_id"],
        "result": result,
        "assignment_count": len(receipts),
        "dispatched_count": counts["DISPATCHED"],
        "refused_count": counts["REFUSED"],
        "human_required_count": counts["HUMAN_REQUIRED"],
        "error_count": counts["ERROR"],
        "receipts": receipts,
    }
    receipt_digest = concurrency_effect_receipt_digest(receipt)
    validate_concurrency_effect_receipt(
        receipt,
        expected_receipt_digest=receipt_digest,
        expected_authorization_digest=trusted_auth_digest,
    )
    with data_root_write_lock(root):
        _finalize_effect(root, effect_id=effect_identity, receipt=receipt)
    return {**receipt, "receipt_digest": receipt_digest}


def get_concurrency_dispatch_effect_entry(
    data_root: Path,
    effect_id: str,
) -> dict[str, object]:
    """Return one fully validated effect-ledger entry by exact effect id."""
    identity = _identity(effect_id, label="effect_id")
    ledger = _load_ledger(Path(data_root))
    matches = [
        item for item in ledger["effects"]
        if item.get("effect_id") == identity
    ]
    if len(matches) != 1:
        _reject("concurrency dispatch effect is not found")
    return dict(matches[0])


def concurrency_dispatch_effect_dashboard(data_root: Path) -> dict[str, object]:
    root = Path(data_root)
    ledger = _load_ledger(root)
    effects = list(ledger["effects"])
    terminal = [item for item in effects if item.get("state") == "TERMINAL"]
    in_progress = [item for item in effects if item.get("state") == "IN_PROGRESS"]
    return {
        "state": "OBSERVED" if effects else "UNKNOWN",
        "authority": AUTHORITY,
        "effect_count": len(effects),
        "terminal_count": len(terminal),
        "in_progress_count": len(in_progress),
        "latest_effect": effects[-1] if effects else None,
        "effects": effects[-50:],
        "join_authority": "NONE",
        "pass_authority": "NONE",
    }
