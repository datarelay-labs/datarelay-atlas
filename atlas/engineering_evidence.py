"""Bounded Engineering System evidence federation for Atlas U2.

Engineering System remains the normative methodology authority. Atlas validates
explicitly supplied evidence and stores only bounded attributable metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.lifecycle_intelligence import lifecycle_view
from atlas.provenance import REPO_RE, ValidationError, validate_project_id
from atlas.registry import ProjectRegistry
from atlas.secrets import contains_unsafe_secret

FILENAME = "engineering-system-evidence.json"
STORE_SCHEMA_VERSION = 1
STORE_KIND = "atlas_engineering_system_evidence_store"
AUTHORITY = "ENGINEERING_SYSTEM_EVIDENCE_ONLY"
PRODUCER_REPOSITORY = "datarelay-labs/engineering-system"
PRODUCER_REVISION = "c50d2a3b7540dcc2899d752573d0634da78dd2bc"
PRODUCER_RELEASE = "v1.7.0"
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 256 * 1024
MAX_STORE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 4096
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

SUPPORTED_ARTIFACT_KINDS = {
    "efficiency": frozenset({"efficiency-telemetry", "efficiency-outcome-report"}),
    "behavior": frozenset({"behavior-eval-run", "behavior-rollout-gate"}),
    "trust": frozenset({"trust-evidence-receipt"}),
    "runtime": frozenset({"runtime-contract"}),
}

SUPPORTED_SCHEMAS = {
    "efficiency": {
        "path": "schemas/efficiency-telemetry.schema.json",
        "sha256": "17ed1d5584c220a49b3338469b4d4d74e1c146ab3578995e9f0c24b763ca81b7",
    },
    "behavior": {
        "path": "schemas/behavior-result.schema.json",
        "sha256": "26181ed399298cfa54e9a3cd542bfb4dc87376de7e2120859e2e805a8dedf797",
    },
    "trust": {
        "path": "schemas/trust-evidence-receipt.schema.json",
        "sha256": "0ca08c346cfb43abdcf82cb457a104696317cf3bac515c12fe0f859f080be430",
    },
    "runtime": {
        "path": "schemas/runtime-contract.schema.json",
        "sha256": "66317afefdfd9e49d125675972af9c2667e4e21e55a7baeaa7e1f214179d5f85",
    },
}
def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("Engineering System evidence contains duplicate JSON keys")
        result[key] = value
    return result


def _read_regular(path: Path, *, limit: int, label: str) -> bytes:
    source = Path(path)
    if not hasattr(os, "O_NOFOLLOW"):
        _reject(f"{label} path safety is unsupported")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise ValidationError(f"{label} path is unsafe or unreadable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _reject(f"{label} path is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(1024 * 1024, limit - total + 1))
            if not block:
                break
            total += len(block)
            if total > limit:
                _reject(f"{label} exceeds bounded input size")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _json(raw: bytes, *, label: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(f"{label} contains a non-finite number"),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} is invalid JSON") from exc


def _observed_at(value: str) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("observed_at must be a UTC second-resolution timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("observed_at must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("observed_at must be a UTC timestamp")
    return value


def _head(value: str, *, field: str = "subject_head") -> str:
    if not isinstance(value, str) or _HEAD.fullmatch(value) is None:
        _reject(f"{field} is invalid")
    return value


def _workstream(value: str) -> str:
    if not isinstance(value, str) or _WORKSTREAM.fullmatch(value) is None:
        _reject("workstream is invalid")
    return value
def _load_schema(
    family: str,
    schema_path: Path,
    producer_revision: str,
) -> tuple[dict[str, object], str, str]:
    spec = SUPPORTED_SCHEMAS.get(family)
    if spec is None:
        _reject("unsupported Engineering System evidence family")
    if producer_revision != PRODUCER_REVISION:
        _reject("unsupported Engineering System producer revision")
    raw = _read_regular(schema_path, limit=MAX_SCHEMA_BYTES, label="Engineering System schema")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != spec["sha256"]:
        _reject("Engineering System schema digest does not match producer revision")
    schema = _json(raw, label="Engineering System schema")
    if not isinstance(schema, dict):
        _reject("Engineering System schema must be a JSON object")
    return schema, str(spec["path"]), digest


def _load_artifact(family: str, artifact_path: Path) -> tuple[object, bytes]:
    raw = _read_regular(
        artifact_path,
        limit=MAX_ARTIFACT_BYTES,
        label="Engineering System evidence artifact",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("Engineering System evidence artifact is not UTF-8") from exc
    if contains_unsafe_secret(text):
        _reject("Engineering System evidence artifact contains unsafe sensitive content")
    if family == "runtime":
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValidationError("runtime contract artifact is invalid YAML") from exc
    else:
        payload = _json(raw, label="Engineering System evidence artifact")
    return payload, raw
def _validate_schema(schema: dict[str, object], payload: object) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        _reject("Engineering System evidence artifact does not match producer schema")


def _count_statuses(rows: object) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "UNKNOWN": 0}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("status") in counts:
                counts[str(row["status"])] += 1
    return counts


def _summary(
    family: str,
    payload: dict[str, object],
    *,
    repository: str,
    workstream: str,
    subject_head: str,
) -> tuple[str, dict[str, object]]:
    kind = payload.get("kind")
    if family == "runtime":
        authorities = payload.get("authorities")
        capabilities = payload.get("capabilities")
        support: dict[str, object] = {}
        if isinstance(capabilities, dict):
            for name, value in sorted(capabilities.items()):
                if isinstance(value, dict):
                    support[str(name)] = value.get("support")
        return "runtime-contract", {
            "version": payload.get("version"),
            "authorities": dict(authorities) if isinstance(authorities, dict) else {},
            "capability_support": support,
        }

    if not isinstance(kind, str):
        _reject("Engineering System evidence kind is missing")

    if family == "efficiency" and kind == "efficiency-telemetry":
        if payload.get("repo") != repository or payload.get("workstream") != workstream:
            _reject("efficiency telemetry attribution does not match registered project")
        validation = payload.get("validation")
        if not isinstance(validation, dict) or validation.get("exact_head") != subject_head:
            _reject("efficiency telemetry is not bound to subject_head")
        return kind, {
            "task_kind": payload.get("task_kind"),
            "terminal": payload.get("terminal"),
            "validation_evidence_state": validation.get("evidence_state"),
            "validation_outcome": validation.get("outcome"),
            "duration_seconds": payload.get("duration_seconds"),
            "usage": payload.get("usage"),
            "counts": payload.get("counts"),
            "budget": payload.get("budget"),
            "profile": payload.get("profile"),
        }

    if family == "efficiency" and kind == "efficiency-outcome-report":
        if payload.get("head") != subject_head:
            _reject("efficiency outcome report is not bound to subject_head")
        return kind, {
            key: payload.get(key)
            for key in (
                "evidence_state",
                "terminal",
                "validation_outcome",
                "record_count",
                "duration_seconds",
                "cost",
                "rework_count",
                "human_interventions",
            )
        }

    if family == "behavior" and kind == "behavior-eval-run":
        if payload.get("head") != subject_head:
            _reject("behavior eval is not bound to subject_head")
        scenarios = payload.get("scenarios")
        status_counts = _count_statuses(scenarios)
        mandatory_nonpass = 0
        safety_nonpass = 0
        if isinstance(scenarios, list):
            for row in scenarios:
                if not isinstance(row, dict) or row.get("status") == "PASS":
                    continue
                mandatory_nonpass += int(row.get("mandatory") is True)
                safety_nonpass += int(row.get("safety") is True)
        return kind, {
            "runner": payload.get("runner"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "scenario_count": len(scenarios) if isinstance(scenarios, list) else 0,
            "status_counts": status_counts,
            "mandatory_nonpass": mandatory_nonpass,
            "safety_nonpass": safety_nonpass,
        }

    if family == "behavior" and kind == "behavior-rollout-gate":
        if payload.get("head") != subject_head:
            _reject("behavior rollout gate is not bound to subject_head")
        reasons = payload.get("reasons")
        return kind, {
            "status": payload.get("status"),
            "reasons": list(reasons) if isinstance(reasons, list) else [],
        }

    if family == "trust" and kind == "trust-evidence-receipt":
        if (
            payload.get("target_repo") != repository
            or payload.get("workstream") != workstream
            or payload.get("subject_head") != subject_head
        ):
            _reject("trust evidence attribution does not match import envelope")
        items = payload.get("items")
        receipt_intent_revision = payload.get("intent_revision")
        authority_counts: dict[str, int] = {}
        status_counts = _count_statuses(items)
        if isinstance(items, list):
            for row in items:
                if not isinstance(row, dict):
                    continue
                if (
                    row.get("subject_head") != subject_head
                    or row.get("intent_revision") != receipt_intent_revision
                ):
                    _reject("trust evidence item attribution does not match receipt")
                if isinstance(row.get("authority"), str):
                    authority = str(row["authority"])
                    authority_counts[authority] = authority_counts.get(authority, 0) + 1
        return kind, {
            "feature_id": payload.get("feature_id"),
            "oracle": payload.get("oracle"),
            "intent_revision": receipt_intent_revision,
            "item_count": len(items) if isinstance(items, list) else 0,
            "authority_counts": dict(sorted(authority_counts.items())),
            "status_counts": status_counts,
        }

    _reject("unsupported Engineering System artifact kind for evidence family")
    raise AssertionError("unreachable")
def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _identity_digest(parts: list[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _empty_store() -> dict[str, object]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "kind": STORE_KIND,
        "records": [],
    }


def _store_path(data_root: Path) -> Path:
    return Path(data_root) / FILENAME


def validate_engineering_evidence_store(data_root: Path) -> dict[str, object]:
    path = _store_path(data_root)
    if not path.exists():
        return _empty_store()
    raw = _read_regular(path, limit=MAX_STORE_BYTES, label="engineering evidence store")
    if contains_unsafe_secret(raw.decode("utf-8", errors="replace")):
        _reject("engineering evidence store contains unsafe sensitive content")
    payload = _json(raw, label="engineering evidence store")
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "kind", "records"}:
        _reject("engineering evidence store is invalid")
    if (
        payload.get("schema_version") != STORE_SCHEMA_VERSION
        or payload.get("kind") != STORE_KIND
        or not isinstance(payload.get("records"), list)
        or len(payload["records"]) > MAX_RECORDS
    ):
        _reject("engineering evidence store is unsupported")
    required = {
        "record_id",
        "logical_id",
        "project_id",
        "repository",
        "family",
        "artifact_kind",
        "workstream",
        "subject_head",
        "observed_at",
        "producer_repository",
        "producer_release",
        "producer_revision",
        "schema_path",
        "schema_sha256",
        "artifact_sha256",
        "summary_sha256",
        "authority",
        "summary",
    }
    seen_records: set[str] = set()
    seen_logical: set[str] = set()
    for record in payload["records"]:
        if not isinstance(record, dict) or set(record) != required:
            _reject("engineering evidence store record is invalid")
        if (
            record["authority"] != AUTHORITY
            or record["producer_repository"] != PRODUCER_REPOSITORY
            or record["producer_release"] != PRODUCER_RELEASE
        ):
            _reject("engineering evidence store authority is invalid")
        if record["producer_revision"] != PRODUCER_REVISION:
            _reject("engineering evidence store producer revision is unsupported")
        project_id = record.get("project_id")
        repository = record.get("repository")
        if not isinstance(project_id, str):
            _reject("engineering evidence store project identity is invalid")
        try:
            validate_project_id(project_id)
        except ValidationError as exc:
            raise ValidationError("engineering evidence store project identity is invalid") from exc
        if not isinstance(repository, str) or REPO_RE.fullmatch(repository) is None:
            _reject("engineering evidence store repository identity is invalid")
        _head(record["subject_head"])
        _workstream(record["workstream"])
        _observed_at(record["observed_at"])
        for name in (
            "record_id",
            "logical_id",
            "schema_sha256",
            "artifact_sha256",
            "summary_sha256",
        ):
            value = record.get(name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                _reject("engineering evidence store digest is invalid")
        if record["record_id"] in seen_records or record["logical_id"] in seen_logical:
            _reject("engineering evidence store contains duplicate identity")
        seen_records.add(str(record["record_id"]))
        seen_logical.add(str(record["logical_id"]))
        family = record.get("family")
        artifact_kind = record.get("artifact_kind")
        if (
            not isinstance(family, str)
            or family not in SUPPORTED_SCHEMAS
            or not isinstance(record.get("summary"), dict)
        ):
            _reject("engineering evidence store record is unsupported")
        if (
            not isinstance(artifact_kind, str)
            or artifact_kind not in SUPPORTED_ARTIFACT_KINDS[family]
        ):
            _reject("engineering evidence store artifact kind is unsupported")
        schema = SUPPORTED_SCHEMAS[family]
        if (
            record.get("schema_path") != schema["path"]
            or record.get("schema_sha256") != schema["sha256"]
        ):
            _reject("engineering evidence store schema identity is invalid")
        summary_digest = hashlib.sha256(
            _canonical_json(record["summary"]).encode("utf-8")
        ).hexdigest()
        if record["summary_sha256"] != summary_digest:
            _reject("engineering evidence store summary identity is invalid")
        logical_id = _identity_digest(
            [
                project_id,
                repository,
                str(family),
                str(record["artifact_kind"]),
                str(record["workstream"]),
                str(record["subject_head"]),
                str(record["observed_at"]),
            ]
        )
        if record["logical_id"] != logical_id:
            _reject("engineering evidence store logical identity is invalid")
        record_id = _identity_digest(
            [
                logical_id,
                str(record["artifact_sha256"]),
                str(record["schema_sha256"]),
                str(record["producer_revision"]),
            ]
        )
        if record["record_id"] != record_id:
            _reject("engineering evidence store record identity is invalid")
    return payload


def _save_store(data_root: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode("utf-8")) > MAX_STORE_BYTES:
        _reject("engineering evidence store exceeds bounded store size")
    if contains_unsafe_secret(encoded):
        _reject("engineering evidence store would contain unsafe sensitive content")
    atomic_write_text(_store_path(data_root), encoded)


def import_engineering_evidence(
    data_root: Path,
    registry: ProjectRegistry,
    *,
    project_id: str,
    family: str,
    artifact_path: Path,
    schema_path: Path,
    producer_revision: str,
    workstream: str,
    subject_head: str,
    observed_at: str,
) -> dict[str, object]:
    project = registry.get(project_id)
    workstream = _workstream(workstream)
    subject_head = _head(subject_head)
    observed_at = _observed_at(observed_at)
    schema, canonical_schema_path, schema_digest = _load_schema(
        family, schema_path, producer_revision
    )
    payload, raw_artifact = _load_artifact(family, artifact_path)
    _validate_schema(schema, payload)
    if not isinstance(payload, dict):
        _reject("Engineering System evidence artifact must be an object")
    artifact_kind, summary = _summary(
        family,
        payload,
        repository=project.repository,
        workstream=workstream,
        subject_head=subject_head,
    )
    artifact_digest = hashlib.sha256(raw_artifact).hexdigest()
    logical_id = _identity_digest(
        [
            project.project_id,
            project.repository,
            family,
            artifact_kind,
            workstream,
            subject_head,
            observed_at,
        ]
    )
    record_id = _identity_digest([logical_id, artifact_digest, schema_digest, producer_revision])
    summary_digest = hashlib.sha256(_canonical_json(summary).encode("utf-8")).hexdigest()
    record = {
        "record_id": record_id,
        "logical_id": logical_id,
        "project_id": project.project_id,
        "repository": project.repository,
        "family": family,
        "artifact_kind": artifact_kind,
        "workstream": workstream,
        "subject_head": subject_head,
        "observed_at": observed_at,
        "producer_repository": PRODUCER_REPOSITORY,
        "producer_release": PRODUCER_RELEASE,
        "producer_revision": producer_revision,
        "schema_path": canonical_schema_path,
        "schema_sha256": schema_digest,
        "artifact_sha256": artifact_digest,
        "summary_sha256": summary_digest,
        "authority": AUTHORITY,
        "summary": summary,
    }
    if contains_unsafe_secret(_canonical_json(record)):
        _reject("normalized Engineering System evidence contains unsafe sensitive content")

    root = Path(data_root)
    with data_root_write_lock(root):
        store = validate_engineering_evidence_store(root)
        records = list(store["records"])
        for existing in records:
            if existing["logical_id"] != logical_id:
                continue
            if existing["record_id"] == record_id:
                return {"state": "DUPLICATE", "record": existing}
            _reject("conflicting Engineering System evidence identity already exists")
        if len(records) >= MAX_RECORDS:
            _reject("engineering evidence store is full")
        records.append(record)
        records.sort(key=lambda item: (item["observed_at"], item["record_id"]))
        store["records"] = records
        _save_store(root, store)
    return {"state": "IMPORTED", "record": record}


def _record_state(
    data_root: Path,
    repository: str,
    records: list[dict[str, object]],
) -> tuple[str, dict[str, object] | None, str | None]:
    if not records:
        return "UNKNOWN", None, None
    try:
        view = lifecycle_view(data_root, repository)
    except ValidationError:
        return "UNAVAILABLE", max(records, key=lambda item: str(item["observed_at"])), None
    if view.work.state == "UNAVAILABLE":
        return "UNAVAILABLE", max(records, key=lambda item: str(item["observed_at"])), None

    canonical_packets = [packet for packet in view.work_packets if packet.canonical]
    if not canonical_packets:
        return "UNKNOWN", max(records, key=lambda item: str(item["observed_at"])), None
    priority = {"ACTIVE": 0, "BLOCKED": 1, "PAUSED": 2, "COMPLETE": 3}
    current_priority = min(
        priority.get(packet.packet_status, 9) for packet in canonical_packets
    )
    current_heads = {
        packet.head
        for packet in canonical_packets
        if priority.get(packet.packet_status, 9) == current_priority
    }
    if not current_heads:
        return "UNKNOWN", max(records, key=lambda item: str(item["observed_at"])), None

    matching = [item for item in records if item["subject_head"] in current_heads]
    if matching:
        selected = max(matching, key=lambda item: str(item["observed_at"]))
        return "CURRENT", selected, str(selected["subject_head"])
    current_head = next(iter(current_heads)) if len(current_heads) == 1 else None
    return (
        "STALE_DIFFERENT_HEAD",
        max(records, key=lambda item: str(item["observed_at"])),
        current_head,
    )


def engineering_evidence_dashboard(
    data_root: Path,
    registry: ProjectRegistry,
    *,
    project_ids: list[str] | None = None,
) -> dict[str, object]:
    try:
        store = validate_engineering_evidence_store(data_root)
    except ValidationError:
        return {
            "state": "UNAVAILABLE",
            "authority": AUTHORITY,
            "producer_release": PRODUCER_RELEASE,
            "project_count": 0,
            "record_count": 0,
            "state_counts": {"CURRENT": 0, "STALE_DIFFERENT_HEAD": 0, "UNKNOWN": 0, "UNAVAILABLE": 1},
            "projects": [],
        }
    if project_ids is None:
        projects = registry.list_projects()
    else:
        if not project_ids or len(set(project_ids)) != len(project_ids):
            _reject("project_ids scope is invalid")
        projects = [registry.get(value) for value in project_ids]
    all_records = list(store["records"])
    scoped_projects = {
        (project.project_id, project.repository) for project in projects
    }
    scoped_records = [
        item
        for item in all_records
        if (item["project_id"], item["repository"]) in scoped_projects
    ]
    state_counts = {"CURRENT": 0, "STALE_DIFFERENT_HEAD": 0, "UNKNOWN": 0, "UNAVAILABLE": 0}
    project_rows: list[dict[str, object]] = []
    for project in projects:
        project_records = [
            item
            for item in scoped_records
            if item["project_id"] == project.project_id
            and item["repository"] == project.repository
        ]
        families: list[dict[str, object]] = []
        for family in SUPPORTED_SCHEMAS:
            family_records = [item for item in project_records if item["family"] == family]
            state, selected, current_head = _record_state(
                Path(data_root), project.repository, family_records
            )
            state_counts[state] += 1
            families.append(
                {
                    "family": family,
                    "state": state,
                    "current_head": current_head,
                    "record_count": len(family_records),
                    "latest": selected,
                }
            )
        project_rows.append(
            {
                "project_id": project.project_id,
                "repository": project.repository,
                "enabled": project.enabled,
                "record_count": len(project_records),
                "families": families,
            }
        )
    return {
        "state": "OBSERVED",
        "authority": AUTHORITY,
        "producer_repository": PRODUCER_REPOSITORY,
        "producer_release": PRODUCER_RELEASE,
        "producer_revision": PRODUCER_REVISION,
        "project_count": len(project_rows),
        "record_count": len(scoped_records),
        "state_counts": state_counts,
        "projects": project_rows,
    }
