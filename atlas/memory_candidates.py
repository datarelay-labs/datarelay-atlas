"""Bounded non-authoritative memory candidates for Verified Memory M2."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from atlas.data_lock import data_root_write_lock
from atlas.provenance import ValidationError
from atlas.secrets import contains_unsafe_secret

FILENAME = "memory-candidates.json"
SCHEMA_VERSION = 1
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
    raw = path.read_bytes()
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
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION or payload["kind"] != "atlas_memory_candidates":
        raise ValidationError("memory candidate store is unsupported")
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
) -> dict[str, object]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValidationError("memory candidate limit is invalid")
    project_id = _identity(project_id, "project_id", optional=True)
    repository = _identity(repository, "repository", optional=True)
    workstream = _identity(workstream, "workstream", optional=True)
    if project_id is None and repository is None:
        raise ValidationError("memory candidate project_id or repository is required")
    store = load_memory_candidates(data_root)
    items = [
        item for item in store["items"]
        if (project_id is None or item["project_id"] == project_id)
        and (repository is None or item["repository"] == repository)
        and (workstream is None or item["workstream"] == workstream)
    ][:limit]
    return {
        "state": "OBSERVED",
        "authority": AUTHORITY,
        "canonical": False,
        "scope": {"project_id": project_id, "repository": repository, "workstream": workstream},
        "count": len(items),
        "items": items,
    }


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
        identity_payload = {
            "candidate_class": candidate_class,
            "project_id": project_id,
            "repository": repository,
            "workstream": workstream,
            "content": content,
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
            "authority": AUTHORITY,
            "canonical": False,
        })

    root = Path(data_root)
    path = root / FILENAME
    root.mkdir(parents=True, exist_ok=True)
    with data_root_write_lock(root):
        store = load_memory_candidates(root)
        existing = {item["candidate_id"] for item in store["items"]}
        additions = [item for item in normalized if item["candidate_id"] not in existing]
        combined = [*store["items"], *additions]
        if len(combined) > _MAX_ITEMS:
            raise ValidationError("memory candidate store item limit exceeded")
        payload = {"schema_version": SCHEMA_VERSION, "kind": "atlas_memory_candidates", "items": combined}
        encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValidationError("memory candidate store exceeds bounded size")
        if contains_unsafe_secret(encoded.decode("utf-8")):
            raise ValidationError("memory candidate store contains unsafe secret")
        tmp = path.with_suffix(path.suffix + ".tmp")
        if path.is_symlink() or tmp.exists() or tmp.is_symlink():
            raise ValidationError("memory candidate store is unsafe")
        with tmp.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
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
