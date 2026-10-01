"""Claim-bound multi-node join/reconciliation evidence."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from atlas.concurrency_claim import get_concurrency_handoff_claim
from atlas.concurrency_join import get_concurrency_dispatch_join_entry
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
OBSERVATION_KIND = "concurrency_claim_bound_join_observation"
EVIDENCE_KIND = "concurrency_claim_bound_join_evidence"
LEDGER_KIND = "concurrency_claim_bound_join_ledger"
FILENAME = "concurrency-claim-bound-joins.json"
AUTHORITY = "MEASUREMENT_ONLY"
PASS_AUTHORITY = "MEASUREMENT_ONLY"
RESULTS = frozenset({"PASS", "PARTIAL", "FAILED", "HUMAN_REQUIRED"})
OUTCOMES = frozenset({"COMPLETE", "FAILED", "HUMAN_REQUIRED"})
_MAX_BYTES = 2 * 1024 * 1024
_MAX_JOINS = 500
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#+-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _reject(f"concurrency claim-bound join {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency claim-bound join {label} is invalid")
    return value


def _utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject(f"concurrency claim-bound join {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"concurrency claim-bound join {label} must be UTC"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"concurrency claim-bound join {label} must be UTC")
    return value


def _bounded_int(
    value: object,
    *,
    label: str,
    maximum: int = 1_000_000,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        _reject(f"concurrency claim-bound join {label} is invalid")
    return value


def _canonical_digest(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "concurrency claim-bound join payload is not canonical JSON"
        ) from exc
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
        _reject("concurrency claim-bound join ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            "concurrency claim-bound join ledger is unreadable"
        ) from exc
    if len(raw) > _MAX_BYTES:
        _reject("concurrency claim-bound join ledger exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "concurrency claim-bound join ledger is invalid JSON"
        ) from exc
    assert_content_free(payload)
    return payload


def validate_concurrency_claim_bound_join_evidence(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "claim_join_id",
        "dispatch_join_id",
        "dispatch_join_digest",
        "effect_id",
        "effect_receipt_digest",
        "authorization_digest",
        "observed_at",
        "effect_result",
        "result",
        "pass_authority",
        "completion_authority",
        "release_authority",
        "merge_authority",
        "deploy_authority",
        "dispatched_count",
        "dispatch_omission_count",
        "complete_count",
        "failed_count",
        "human_required_count",
        "bindings",
        "dispatch_omissions",
        "evidence_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency claim-bound join evidence schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != EVIDENCE_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("result") not in RESULTS
        or payload.get("pass_authority") != PASS_AUTHORITY
        or payload.get("completion_authority") != "NONE"
        or payload.get("release_authority") != "NONE"
        or payload.get("merge_authority") != "NONE"
        or payload.get("deploy_authority") != "NONE"
        or payload.get("effect_result") not in {"DISPATCHED", "PARTIAL"}
    ):
        _reject("concurrency claim-bound join authority/result is invalid")
    for key in ("claim_join_id", "dispatch_join_id", "effect_id"):
        _id(payload.get(key), label=key)
    for key in (
        "dispatch_join_digest",
        "effect_receipt_digest",
        "authorization_digest",
    ):
        _digest(payload.get(key), label=key)
    _utc(payload.get("observed_at"), label="observed_at")

    bindings = payload.get("bindings")
    omissions = payload.get("dispatch_omissions")
    if not isinstance(bindings, list) or not bindings:
        _reject("concurrency claim-bound join bindings are invalid")
    if not isinstance(omissions, list):
        _reject("concurrency claim-bound join omissions are invalid")

    seen_nodes: set[str] = set()
    seen_claims: set[str] = set()
    counts = {"COMPLETE": 0, "FAILED": 0, "HUMAN_REQUIRED": 0}
    for item in bindings:
        expected_keys = {
            "node_id",
            "claim_digest",
            "handoff_digest",
            "slot_id",
            "worker_id",
            "provider",
            "runtime",
            "route_id",
            "head",
            "outcome",
            "duration_ms",
            "evidence_ref",
        }
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency claim-bound join binding schema is invalid")
        node_id = _id(item.get("node_id"), label="node_id")
        claim_digest = _digest(item.get("claim_digest"), label="claim_digest")
        _digest(item.get("handoff_digest"), label="handoff_digest")
        if node_id in seen_nodes or claim_digest in seen_claims:
            _reject("concurrency claim-bound join binding is duplicated")
        seen_nodes.add(node_id)
        seen_claims.add(claim_digest)
        for key in (
            "slot_id",
            "worker_id",
            "provider",
            "runtime",
            "evidence_ref",
        ):
            _id(item.get(key), label=key)
        head = item.get("head")
        if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
            _reject("concurrency claim-bound join head is invalid")
        route_id = item.get("route_id")
        if route_id is not None:
            _id(route_id, label="route_id")
        outcome = item.get("outcome")
        if outcome not in OUTCOMES:
            _reject("concurrency claim-bound join outcome is invalid")
        _bounded_int(
            item.get("duration_ms"),
            label="duration_ms",
            maximum=7 * 24 * 60 * 60 * 1000,
        )
        counts[str(outcome)] += 1

    omission_nodes: set[str] = set()
    for item in omissions:
        expected_keys = {
            "node_id",
            "slot_id",
            "worker_id",
            "provider",
            "runtime",
            "route_id",
            "head",
            "dispatch_result",
        }
        if not isinstance(item, dict) or set(item) != expected_keys:
            _reject("concurrency claim-bound join omission schema is invalid")
        node_id = _id(item.get("node_id"), label="omission node_id")
        if node_id in seen_nodes or node_id in omission_nodes:
            _reject("concurrency claim-bound join omission node is duplicated")
        omission_nodes.add(node_id)
        for key in ("slot_id", "worker_id", "provider", "runtime"):
            _id(item.get(key), label=key)
        head = item.get("head")
        if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
            _reject("concurrency claim-bound join omission head is invalid")
        route_id = item.get("route_id")
        if route_id is not None:
            _id(route_id, label="route_id")
        if item.get("dispatch_result") not in {
            "REFUSED",
            "HUMAN_REQUIRED",
            "ERROR",
        }:
            _reject("concurrency claim-bound join omission result is invalid")

    reported = {
        "COMPLETE": _bounded_int(
            payload.get("complete_count"),
            label="complete_count",
            maximum=64,
        ),
        "FAILED": _bounded_int(
            payload.get("failed_count"),
            label="failed_count",
            maximum=64,
        ),
        "HUMAN_REQUIRED": _bounded_int(
            payload.get("human_required_count"),
            label="human_required_count",
            maximum=64,
        ),
    }
    if reported != counts:
        _reject("concurrency claim-bound join counts are inconsistent")
    dispatched_count = _bounded_int(
        payload.get("dispatched_count"),
        label="dispatched_count",
        maximum=64,
    )
    omission_count = _bounded_int(
        payload.get("dispatch_omission_count"),
        label="dispatch_omission_count",
        maximum=64,
    )
    if dispatched_count != len(bindings) or omission_count != len(omissions):
        _reject("concurrency claim-bound join assignment counts are inconsistent")
    expected_result = (
        "HUMAN_REQUIRED"
        if counts["HUMAN_REQUIRED"]
        else (
            "PASS"
            if counts["COMPLETE"] == len(bindings)
            else ("PARTIAL" if counts["COMPLETE"] else "FAILED")
        )
    )
    if payload.get("result") != expected_result:
        _reject("concurrency claim-bound join aggregate result is inconsistent")
    digest = _digest(payload.get("evidence_digest"), label="evidence_digest")
    basis = {
        key: value for key, value in payload.items()
        if key != "evidence_digest"
    }
    if _canonical_digest(basis) != digest:
        _reject("concurrency claim-bound join evidence digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def _validate_claim_binding(
    root: Path,
    *,
    source_join: dict[str, object],
    binding: dict[str, object],
) -> None:
    claim = get_concurrency_handoff_claim(
        root,
        str(binding["claim_digest"]),
    )
    if (
        claim["effect_id"] != source_join["effect_id"]
        or claim["effect_receipt_digest"] != source_join["effect_receipt_digest"]
        or claim["authorization_digest"] != source_join["authorization_digest"]
        or claim["node_id"] != binding["node_id"]
        or claim["handoff_digest"] != binding["handoff_digest"]
    ):
        _reject("concurrency claim-bound join claim source identity drifted")
    for key in (
        "slot_id",
        "worker_id",
        "provider",
        "runtime",
        "route_id",
        "head",
    ):
        if claim.get(key) != binding.get(key):
            _reject(
                "concurrency claim-bound join claim attribution does not match"
            )


def _validate_binding(
    root: Path,
    evidence: dict[str, object],
) -> None:
    source_join = get_concurrency_dispatch_join_entry(
        root,
        str(evidence["dispatch_join_digest"]),
    )
    for key, source_key in (
        ("dispatch_join_id", "join_id"),
        ("effect_id", "effect_id"),
        ("effect_receipt_digest", "effect_receipt_digest"),
        ("authorization_digest", "authorization_digest"),
        ("observed_at", "observed_at"),
        ("effect_result", "effect_result"),
        ("result", "result"),
        ("pass_authority", "pass_authority"),
        ("dispatched_count", "dispatched_count"),
        ("dispatch_omission_count", "dispatch_omission_count"),
        ("complete_count", "complete_count"),
        ("failed_count", "failed_count"),
        ("human_required_count", "human_required_count"),
    ):
        if evidence.get(key) != source_join.get(source_key):
            _reject("concurrency claim-bound join source join drifted")
    if evidence["dispatch_omissions"] != source_join["dispatch_omissions"]:
        _reject("concurrency claim-bound join omissions drifted")

    source_outcomes = {
        str(item["node_id"]): item for item in source_join["outcomes"]
    }
    bindings = {
        str(item["node_id"]): item for item in evidence["bindings"]
    }
    if set(source_outcomes) != set(bindings):
        _reject("concurrency claim-bound join outcome set drifted")
    for node_id, binding in bindings.items():
        outcome = source_outcomes[node_id]
        for key in (
            "slot_id",
            "worker_id",
            "provider",
            "runtime",
            "route_id",
            "head",
            "outcome",
            "duration_ms",
            "evidence_ref",
        ):
            if binding.get(key) != outcome.get(key):
                _reject(
                    "concurrency claim-bound join stored attribution drifted"
                )
        _validate_claim_binding(
            root,
            source_join=source_join,
            binding=binding,
        )


def _load_ledger(root: Path) -> dict[str, object]:
    path = Path(root) / FILENAME
    if path.is_symlink():
        _reject("concurrency claim-bound join ledger path is unsafe")
    if not path.exists():
        return _empty_ledger()
    payload = _load_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema_version",
            "kind",
            "authority",
            "joins",
        }
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or payload.get("authority") != AUTHORITY
        or not isinstance(payload.get("joins"), list)
        or len(payload["joins"]) > _MAX_JOINS
    ):
        _reject("concurrency claim-bound join ledger schema is invalid")

    joins: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_source_joins: set[str] = set()
    seen_claims: set[str] = set()
    for item in payload["joins"]:
        evidence = validate_concurrency_claim_bound_join_evidence(item)
        _validate_binding(root, evidence)
        claim_join_id = str(evidence["claim_join_id"])
        source_join_digest = str(evidence["dispatch_join_digest"])
        claim_digests = {
            str(binding["claim_digest"])
            for binding in evidence["bindings"]
        }
        if (
            claim_join_id in seen_ids
            or source_join_digest in seen_source_joins
            or seen_claims & claim_digests
        ):
            _reject("concurrency claim-bound join replay identity is duplicated")
        seen_ids.add(claim_join_id)
        seen_source_joins.add(source_join_digest)
        seen_claims.update(claim_digests)
        joins.append(evidence)
    return {**_empty_ledger(), "joins": joins}


def validate_concurrency_claim_bound_join_observation(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "claim_join_id",
        "dispatch_join_digest",
        "claims",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency claim-bound join observation schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != OBSERVATION_KIND
    ):
        _reject("concurrency claim-bound join observation version/kind is invalid")
    claims = payload.get("claims")
    if (
        not isinstance(claims, list)
        or not 1 <= len(claims) <= 64
    ):
        _reject("concurrency claim-bound join observation claims are invalid")
    normalized_claims: list[dict[str, str]] = []
    nodes: set[str] = set()
    digests: set[str] = set()
    for item in claims:
        if not isinstance(item, dict) or set(item) != {
            "node_id",
            "claim_digest",
        }:
            _reject("concurrency claim-bound join claim reference is invalid")
        node_id = _id(item.get("node_id"), label="node_id")
        claim_digest = _digest(item.get("claim_digest"), label="claim_digest")
        if node_id in nodes or claim_digest in digests:
            _reject("concurrency claim-bound join claim reference is duplicated")
        nodes.add(node_id)
        digests.add(claim_digest)
        normalized_claims.append(
            {
                "node_id": node_id,
                "claim_digest": claim_digest,
            }
        )
    normalized_claims.sort(key=lambda item: item["node_id"])
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": OBSERVATION_KIND,
        "claim_join_id": _id(
            payload.get("claim_join_id"),
            label="claim_join_id",
        ),
        "dispatch_join_digest": _digest(
            payload.get("dispatch_join_digest"),
            label="dispatch_join_digest",
        ),
        "claims": normalized_claims,
    }
    assert_content_free(result)
    return result


def build_concurrency_claim_bound_join(
    data_root: Path,
    observation: object,
) -> dict[str, object]:
    root = Path(data_root)
    normalized = validate_concurrency_claim_bound_join_observation(observation)
    source_join = get_concurrency_dispatch_join_entry(
        root,
        str(normalized["dispatch_join_digest"]),
    )
    outcomes = {
        str(item["node_id"]): item
        for item in source_join["outcomes"]
    }
    claim_refs = {
        str(item["node_id"]): str(item["claim_digest"])
        for item in normalized["claims"]
    }
    if set(outcomes) != set(claim_refs):
        _reject(
            "concurrency claim-bound join requires one claim per dispatched outcome"
        )

    bindings: list[dict[str, object]] = []
    for node_id in sorted(outcomes):
        outcome = outcomes[node_id]
        claim = get_concurrency_handoff_claim(
            root,
            claim_refs[node_id],
        )
        if (
            claim["effect_id"] != source_join["effect_id"]
            or claim["effect_receipt_digest"]
            != source_join["effect_receipt_digest"]
            or claim["authorization_digest"]
            != source_join["authorization_digest"]
            or claim["node_id"] != node_id
        ):
            _reject(
                "concurrency claim-bound join claim does not match source join"
            )
        for key in (
            "slot_id",
            "worker_id",
            "provider",
            "runtime",
            "route_id",
            "head",
        ):
            if claim.get(key) != outcome.get(key):
                _reject(
                    "concurrency claim-bound join claim attribution mismatch"
                )
        bindings.append(
            {
                "node_id": node_id,
                "claim_digest": claim["claim_digest"],
                "handoff_digest": claim["handoff_digest"],
                "slot_id": outcome["slot_id"],
                "worker_id": outcome["worker_id"],
                "provider": outcome["provider"],
                "runtime": outcome["runtime"],
                "route_id": outcome["route_id"],
                "head": outcome["head"],
                "outcome": outcome["outcome"],
                "duration_ms": outcome["duration_ms"],
                "evidence_ref": outcome["evidence_ref"],
            }
        )

    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "authority": AUTHORITY,
        "claim_join_id": normalized["claim_join_id"],
        "dispatch_join_id": source_join["join_id"],
        "dispatch_join_digest": source_join["join_digest"],
        "effect_id": source_join["effect_id"],
        "effect_receipt_digest": source_join["effect_receipt_digest"],
        "authorization_digest": source_join["authorization_digest"],
        "observed_at": source_join["observed_at"],
        "effect_result": source_join["effect_result"],
        "result": source_join["result"],
        "pass_authority": PASS_AUTHORITY,
        "completion_authority": "NONE",
        "release_authority": "NONE",
        "merge_authority": "NONE",
        "deploy_authority": "NONE",
        "dispatched_count": source_join["dispatched_count"],
        "dispatch_omission_count": source_join["dispatch_omission_count"],
        "complete_count": source_join["complete_count"],
        "failed_count": source_join["failed_count"],
        "human_required_count": source_join["human_required_count"],
        "bindings": bindings,
        "dispatch_omissions": list(source_join["dispatch_omissions"]),
    }
    evidence = {
        **body,
        "evidence_digest": _canonical_digest(body),
    }
    evidence = validate_concurrency_claim_bound_join_evidence(evidence)
    _validate_binding(root, evidence)
    return evidence


def record_concurrency_claim_bound_join(
    data_root: Path,
    observation: object,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("concurrency claim-bound join data root is not a directory")
    evidence = build_concurrency_claim_bound_join(root, observation)
    with data_root_write_lock(root):
        ledger = _load_ledger(root)
        _validate_binding(root, evidence)
        if len(ledger["joins"]) >= _MAX_JOINS:
            _reject("concurrency claim-bound join ledger limit reached")
        new_claims = {
            str(binding["claim_digest"])
            for binding in evidence["bindings"]
        }
        for item in ledger["joins"]:
            if (
                item["claim_join_id"] == evidence["claim_join_id"]
                or item["dispatch_join_digest"]
                == evidence["dispatch_join_digest"]
            ):
                _reject("concurrency claim-bound join replay is not allowed")
            old_claims = {
                str(binding["claim_digest"])
                for binding in item["bindings"]
            }
            if old_claims & new_claims:
                _reject("concurrency claim reuse is not allowed")
        ledger["joins"].append(evidence)
        ledger["joins"].sort(
            key=lambda item: (
                str(item["observed_at"]),
                str(item["claim_join_id"]),
            )
        )
        atomic_write_text(
            root / FILENAME,
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        )
    return concurrency_claim_bound_join_dashboard(root)


def get_concurrency_claim_bound_join(
    data_root: Path,
    evidence_digest: str,
) -> dict[str, object]:
    digest = _digest(evidence_digest, label="evidence_digest")
    ledger = _load_ledger(Path(data_root))
    matches = [
        item for item in ledger["joins"]
        if item.get("evidence_digest") == digest
    ]
    if len(matches) != 1:
        _reject("concurrency claim-bound join is not found")
    return dict(matches[0])


def concurrency_claim_bound_join_dashboard(
    data_root: Path,
) -> dict[str, object]:
    ledger = _load_ledger(Path(data_root))
    joins = list(ledger["joins"])
    counts = {name: 0 for name in RESULTS}
    for item in joins:
        counts[str(item["result"])] += 1
    return {
        "state": "OBSERVED" if joins else "UNKNOWN",
        "authority": AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "completion_authority": "NONE",
        "release_authority": "NONE",
        "join_count": len(joins),
        "result_counts": counts,
        "latest_join": joins[-1] if joins else None,
        "joins": joins[-50:],
    }
