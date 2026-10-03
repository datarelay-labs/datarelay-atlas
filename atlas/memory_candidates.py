"""Bounded non-authoritative memory candidates for Verified Memory M2."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from atlas.data_lock import data_root_fd_path, data_root_write_lock, require_data_root_writer_owner_fd
from atlas.provenance import ValidationError
from atlas.secrets import contains_unsafe_secret

FILENAME = "memory-candidates.json"
SCHEMA_VERSION = 2
TTL_SECONDS = {
    "OWNER_PREFERENCE": 365 * 24 * 3600,
    "VALIDATED_FINDING": 30 * 24 * 3600,
    "LESSON_LEARNED": 180 * 24 * 3600,
    "RUN_SUMMARY": 14 * 24 * 3600,
    "FUTURE_IDEA": 180 * 24 * 3600,
    "REFERENCE_FACT": 30 * 24 * 3600,
}
AUTHORITY = "NON_AUTHORITATIVE_CANDIDATE"
CANDIDATE_CLASSES = frozenset({
    "OWNER_PREFERENCE",
    "VALIDATED_FINDING",
    "LESSON_LEARNED",
    "RUN_SUMMARY",
    "FUTURE_IDEA",
    "REFERENCE_FACT",
})
APPROVED_INPUT_KINDS = frozenset({"INTERACTION_SUMMARY", "RUN_SUMMARY", "CANONICAL_EVENT"})
_MAX_FILE_BYTES = 1024 * 1024
_MAX_ITEMS = 2048
_MAX_CONTENT_CHARS = 2048
_MAX_ID_CHARS = 128
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("memory candidate store contains duplicate JSON keys")
        result[key] = value
    return result


def _text(value: object, field: str, *, optional: bool = False, limit: int = _MAX_ID_CHARS) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValidationError(f"memory candidate {field} is invalid")
    if contains_unsafe_secret(value):
        raise ValidationError(f"memory candidate {field} contains unsafe secret")
    return value


def _identity(value: object, field: str, *, optional: bool = False) -> str | None:
    text = _text(value, field, optional=optional)
    if text is None:
        return None
    if not _SAFE_ID.fullmatch(text) or ".." in Path(text).parts:
        raise ValidationError(f"memory candidate {field} is invalid")
    return text


def _normalize_provenance(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"source_identity", "source_revision", "source_digest"}:
        raise ValidationError("memory candidate provenance is invalid")
    normalized: dict[str, str] = {}
    for key in ("source_identity", "source_revision", "source_digest"):
        item = value.get(key)
        if item is not None:
            normalized[key] = _text(item, f"provenance {key}", limit=256)  # type: ignore[assignment]
    if not normalized:
        raise ValidationError("memory candidate provenance is invalid")
    digest = normalized.get("source_digest")
    if digest is not None and (len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)):
        raise ValidationError("memory candidate provenance source_digest is invalid")
    return normalized


def _normalize_item(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValidationError("memory candidate is invalid")
    allowed = {
        "candidate_id", "candidate_class", "project_id", "repository", "workstream",
        "content", "input_kind", "observed_at", "provenance", "authority", "canonical",
        "semantic_key", "supersedes", "pinned", "forgotten", "correction_of",
    }
    if set(value) - allowed:
        raise ValidationError("memory candidate contains unsupported fields")
    candidate_class = value.get("candidate_class")
    input_kind = value.get("input_kind")
    if candidate_class not in CANDIDATE_CLASSES:
        raise ValidationError("memory candidate class is invalid")
    if input_kind not in APPROVED_INPUT_KINDS:
        raise ValidationError("memory candidate input_kind is invalid")
    if value.get("authority") != AUTHORITY or value.get("canonical") is not False:
        raise ValidationError("memory candidate authority is invalid")
    content = _text(value.get("content"), "content", limit=_MAX_CONTENT_CHARS)
    return {
        "candidate_id": _identity(value.get("candidate_id"), "candidate_id"),
        "candidate_class": candidate_class,
        "project_id": _identity(value.get("project_id"), "project_id"),
        "repository": _identity(value.get("repository"), "repository"),
        "workstream": _identity(value.get("workstream"), "workstream", optional=True),
        "content": content,
        "input_kind": input_kind,
        "observed_at": _text(value.get("observed_at"), "observed_at", limit=64),
        "provenance": _normalize_provenance(value.get("provenance")),
        "authority": AUTHORITY,
        "canonical": False,
        "semantic_key": _identity(value.get("semantic_key"), "semantic_key"),
        "supersedes": _identity(value.get("supersedes"), "supersedes", optional=True),
        "pinned": value.get("pinned", False) if isinstance(value.get("pinned", False), bool) else False,
        "forgotten": value.get("forgotten", False) if isinstance(value.get("forgotten", False), bool) else False,
        "correction_of": _identity(value.get("correction_of"), "correction_of", optional=True),
    }


def _empty_store() -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "kind": "atlas_memory_candidates", "items": []}


def load_memory_candidates(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if path.is_symlink():
        raise ValidationError("memory candidate store is unsafe")
    if not path.exists():
        return _empty_store()
    if not path.is_file():
        raise ValidationError("memory candidate store is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("memory candidate store is unreadable") from exc
    if len(raw) > _MAX_FILE_BYTES:
        raise ValidationError("memory candidate store exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("memory candidate store is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "kind", "items"}:
        raise ValidationError("memory candidate store is invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] not in {1, SCHEMA_VERSION} or payload["kind"] != "atlas_memory_candidates":
        raise ValidationError("memory candidate store is unsupported")
    if payload["schema_version"] == 1:
        migrated = []
        for item in payload["items"]:
            if not isinstance(item, dict):
                raise ValidationError("memory candidate is invalid")
            semantic = {
                "candidate_class": item.get("candidate_class"),
                "project_id": item.get("project_id"),
                "repository": item.get("repository"),
                "workstream": item.get("workstream"),
                "content": item.get("content"),
            }
            migrated.append({
                **item,
                "pinned": False,
                "forgotten": False,
                "correction_of": None,
                "semantic_key": hashlib.sha256(
                    json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                ).hexdigest(),
                "supersedes": None,
            })
        payload = {"schema_version": SCHEMA_VERSION, "kind": "atlas_memory_candidates", "items": migrated}
    items = payload["items"]
    if not isinstance(items, list) or len(items) > _MAX_ITEMS:
        raise ValidationError("memory candidate store items are invalid")
    normalized = [_normalize_item(item) for item in items]
    ids = [item["candidate_id"] for item in normalized]
    if len(set(ids)) != len(ids):
        raise ValidationError("memory candidate ids are duplicated")
    return {"schema_version": SCHEMA_VERSION, "kind": "atlas_memory_candidates", "items": normalized}


def validate_memory_candidate_store(data_root: Path) -> dict[str, object]:
    return load_memory_candidates(data_root)


def list_memory_candidates(
    data_root: Path,
    *,
    project_id: str | None = None,
    repository: str | None = None,
    workstream: str | None = None,
    limit: int = 100,
    as_of: str | None = None,
    current_provenance: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValidationError("memory candidate limit is invalid")
    project_id = _identity(project_id, "project_id", optional=True)
    repository = _identity(repository, "repository", optional=True)
    workstream = _identity(workstream, "workstream", optional=True)
    if project_id is None and repository is None:
        raise ValidationError("memory candidate project_id or repository is required")
    store = load_memory_candidates(data_root)
    selected = [
        item for item in store["items"]
        if (project_id is None or item["project_id"] == project_id)
        and (repository is None or item["repository"] == repository)
        and (workstream is None or item["workstream"] == workstream)
        and not item.get("forgotten", False)
    ]
    now = _parse_time(as_of) if as_of is not None else datetime.now(timezone.utc)
    superseded = {item["supersedes"] for item in selected if item.get("supersedes")}
    items = []
    for item in selected[:limit]:
        validity = "CURRENT"
        reason = "within type-specific TTL"
        if item["candidate_id"] in superseded:
            validity, reason = "SUPERSEDED", "newer equivalent candidate supersedes this observation"
        else:
            observed = _parse_time(str(item["observed_at"]))
            if not item.get("pinned", False) and (now - observed).total_seconds() > TTL_SECONDS[str(item["candidate_class"])]:
                validity, reason = "STALE", "type-specific TTL expired"
            provenance = item.get("provenance")
            if validity == "CURRENT" and isinstance(provenance, dict) and current_provenance is not None:
                identity = provenance.get("source_identity")
                current = current_provenance.get(str(identity)) if identity else None
                if current is None:
                    validity, reason = "UNKNOWN", "current provenance fact unavailable"
                elif (
                    ("source_revision" in provenance and current.get("source_revision") != provenance.get("source_revision"))
                    or ("source_digest" in provenance and current.get("source_digest") != provenance.get("source_digest"))
                ):
                    validity, reason = "STALE", "current provenance differs from candidate citation"
        items.append({
            **item,
            "validity": validity,
            "validity_reason": reason,
            "ttl_seconds": TTL_SECONDS[str(item["candidate_class"])],
        })
    return {
        "state": "OBSERVED",
        "authority": AUTHORITY,
        "canonical": False,
        "scope": {"project_id": project_id, "repository": repository, "workstream": workstream},
        "count": len(items),
        "items": items,
    }


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("memory candidate observed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError("memory candidate observed_at is invalid")
    return parsed.astimezone(timezone.utc)


def _publish_store(path: Path, items: list[dict[str, object]], *, root_fd: int) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "kind": "atlas_memory_candidates", "items": items}
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    if len(items) > _MAX_ITEMS or len(encoded) > _MAX_FILE_BYTES:
        raise ValidationError("memory candidate store exceeds bounded size")
    if contains_unsafe_secret(encoded.decode("utf-8")):
        raise ValidationError("memory candidate store contains unsafe secret")
    require_data_root_writer_owner_fd(root_fd)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.is_symlink() or tmp.exists() or tmp.is_symlink():
        raise ValidationError("memory candidate store is unsafe")
    with tmp.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    require_data_root_writer_owner_fd(root_fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def control_memory_candidate(
    data_root: Path,
    *,
    project_id: str,
    repository: str,
    candidate_id: str,
    action: str,
    content: str | None = None,
    observed_at: str | None = None,
) -> dict[str, object]:
    project_id = _identity(project_id, "project_id")  # type: ignore[assignment]
    repository = _identity(repository, "repository")  # type: ignore[assignment]
    candidate_id = _identity(candidate_id, "candidate_id")  # type: ignore[assignment]
    if action not in {"PIN", "UNPIN", "FORGET", "CORRECT"}:
        raise ValidationError("memory candidate action is invalid")
    root = Path(data_root)
    with data_root_write_lock(root) as root_fd:
        require_data_root_writer_owner_fd(root_fd)
        bound_root = data_root_fd_path(root_fd); path = bound_root / FILENAME
        store = load_memory_candidates(bound_root)
        index = next((i for i,x in enumerate(store["items"]) if x["candidate_id"] == candidate_id), None)
        if index is None:
            raise ValidationError("unknown memory candidate")
        item = store["items"][index]
        if item["project_id"] != project_id or item["repository"] != repository:
            raise ValidationError("memory candidate scope mismatch")
        items = list(store["items"])
        if action in {"PIN", "UNPIN"}:
            items[index] = {**item, "pinned": action == "PIN"}
            result_id = candidate_id
        elif action == "FORGET":
            items[index] = {**item, "forgotten": True, "pinned": False}
            result_id = candidate_id
        else:
            corrected = _text(content, "content", limit=_MAX_CONTENT_CHARS)
            when = _text(observed_at, "observed_at", limit=64)
            semantic = {
                "candidate_class": item["candidate_class"], "project_id": project_id,
                "repository": repository, "workstream": item["workstream"], "content": corrected,
            }
            semantic_key = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            identity = {**semantic, "input_kind": "INTERACTION_SUMMARY", "observed_at": when, "provenance": item["provenance"]}
            result_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            replacement = {
                "candidate_id": result_id, **identity, "semantic_key": semantic_key,
                "supersedes": candidate_id, "pinned": item.get("pinned", False), "forgotten": False,
                "correction_of": candidate_id, "authority": AUTHORITY, "canonical": False,
            }
            items[index] = {**item, "forgotten": True, "pinned": False}
            items.append(replacement)
        _publish_store(path, items, root_fd=root_fd)
    return {"state": action, "candidate_id": result_id, "authority": AUTHORITY, "canonical": False}


def ingest_memory_candidates(
    data_root: Path,
    *,
    project_id: str,
    repository: str,
    input_kind: str,
    observed_at: str,
    candidates: list[dict[str, object]],
    workstream: str | None = None,
) -> dict[str, object]:
    project_id = _identity(project_id, "project_id")  # type: ignore[assignment]
    repository = _identity(repository, "repository")  # type: ignore[assignment]
    workstream = _identity(workstream, "workstream", optional=True)
    if input_kind not in APPROVED_INPUT_KINDS:
        raise ValidationError("memory candidate input_kind is invalid")
    observed_at = _text(observed_at, "observed_at", limit=64)  # type: ignore[assignment]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 64:
        raise ValidationError("memory candidate batch is invalid")

    normalized: list[dict[str, object]] = []
    for raw in candidates:
        if not isinstance(raw, dict) or set(raw) - {"candidate_class", "content", "provenance"}:
            raise ValidationError("memory candidate input is invalid")
        candidate_class = raw.get("candidate_class")
        if candidate_class not in CANDIDATE_CLASSES:
            raise ValidationError("memory candidate class is invalid")
        content = _text(raw.get("content"), "content", limit=_MAX_CONTENT_CHARS)
        provenance = _normalize_provenance(raw.get("provenance"))
        semantic_payload = {
            "candidate_class": candidate_class,
            "project_id": project_id,
            "repository": repository,
            "workstream": workstream,
            "content": content,
        }
        semantic_key = hashlib.sha256(
            json.dumps(semantic_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        identity_payload = {
            **semantic_payload,
            "input_kind": input_kind,
            "observed_at": observed_at,
            "provenance": provenance,
        }
        candidate_id = hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        normalized.append({
            "candidate_id": candidate_id,
            **identity_payload,
            "semantic_key": semantic_key,
            "supersedes": None,
            "pinned": False,
            "forgotten": False,
            "correction_of": None,
            "authority": AUTHORITY,
            "canonical": False,
        })

    root = Path(data_root)
    with data_root_write_lock(root) as root_fd:
        require_data_root_writer_owner_fd(root_fd)
        bound_root = data_root_fd_path(root_fd); path = bound_root / FILENAME
        store = load_memory_candidates(bound_root)
        existing = {item["candidate_id"] for item in store["items"]}
        latest_by_semantic = {}
        for item in store["items"]:
            latest_by_semantic[item["semantic_key"]] = item
        additions = []
        for item in normalized:
            if item["candidate_id"] in existing:
                continue
            prior = latest_by_semantic.get(item["semantic_key"])
            if prior is not None:
                item = {**item, "supersedes": prior["candidate_id"]}
            additions.append(item)
            latest_by_semantic[item["semantic_key"]] = item
        combined = [*store["items"], *additions]
        if len(combined) > _MAX_ITEMS:
            raise ValidationError("memory candidate store item limit exceeded")
        payload = {"schema_version": SCHEMA_VERSION, "kind": "atlas_memory_candidates", "items": combined}
        encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValidationError("memory candidate store exceeds bounded size")
        if contains_unsafe_secret(encoded.decode("utf-8")):
            raise ValidationError("memory candidate store contains unsafe secret")
        require_data_root_writer_owner_fd(root_fd)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if path.is_symlink() or tmp.exists() or tmp.is_symlink():
            raise ValidationError("memory candidate store is unsafe")
        with tmp.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        require_data_root_writer_owner_fd(root_fd)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    return {
        "state": "INGESTED",
        "authority": AUTHORITY,
        "canonical": False,
        "accepted": len(additions),
        "replayed": len(normalized) - len(additions),
        "candidate_ids": [item["candidate_id"] for item in normalized],
    }
