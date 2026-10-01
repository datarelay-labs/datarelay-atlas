"""Exact-source bounded security-review evidence.

This module validates and publishes reviewer-produced evidence. It never performs
or infers a security review and never grants release, deploy, merge, or PASS
authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError
from atlas.sbom import REPOSITORY, _source_facts
from atlas.secrets import contains_unsafe_secret

SCHEMA_VERSION = 1
KIND = "atlas_security_review_evidence"
SCOPE_VERSION = 1
AUTHORITY = "EVIDENCE_ONLY"
FILENAME = "security-review-evidence.json"
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_FINDINGS = 64

REQUIRED_CONTROL_IDS = (
    "MCP_AUTHORIZATION_SCOPE",
    "MCP_TOKEN_ISSUER_RESOURCE_BINDING",
    "TLS_AND_SECRET_FILE_PERMISSIONS",
    "DEPLOYMENT_INGRESS_HARDENING",
    "DATA_ROOT_PATH_AND_SYMLINK_SAFETY",
    "BACKUP_RESTORE_FAIL_CLOSED",
    "CONTENT_FREE_ERROR_AND_EVIDENCE_BOUNDARY",
)
CONTROL_OUTCOMES = frozenset({"PASS", "FAIL", "UNKNOWN"})
FINDING_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})
FINDING_DISPOSITIONS = frozenset({"OPEN", "RESOLVED"})
REVIEWER_KINDS = frozenset({"CHATGPT_CHAT", "CODEX", "HUMAN", "OTHER_APPROVED"})

_HEAD = re.compile(r"^[0-9a-f]{40}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,159}$")
_FINDING_ID = re.compile(r"^[A-Z0-9][A-Z0-9._\-]{0,63}$")


def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("security review evidence is not canonical JSON") from exc


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _utc(value: object) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("security review reviewed_at must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("security review reviewed_at must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("security review reviewed_at must be a UTC timestamp")
    return value


def _safe_ref(value: object, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        _reject(f"security review {field} is invalid")
    if "://" in value or value.startswith("/"):
        _reject(f"security review {field} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("security review evidence contains a duplicate JSON key")
        result[key] = value
    return result


def _load_json(path: Path) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        _reject("security review evidence path is unsafe")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError("security review evidence is unreadable") from exc
    if len(raw) > MAX_EVIDENCE_BYTES:
        _reject("security review evidence exceeds bounded input size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                "security review evidence contains a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("security review evidence is invalid JSON") from exc
    assert_content_free(payload)
    if contains_unsafe_secret(raw.decode("utf-8", errors="replace")):
        _reject("security review evidence contains unsafe sensitive content")
    return payload

def _validate_source_facts(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "repository",
        "source_revision",
        "clean",
    }:
        _reject("security review source facts are invalid")
    repository = value.get("repository")
    revision = value.get("source_revision")
    clean = value.get("clean")
    if repository != REPOSITORY:
        _reject("security review repository identity mismatch")
    if not isinstance(revision, str) or _HEAD.fullmatch(revision) is None:
        _reject("security review source revision is invalid")
    if not isinstance(clean, bool):
        _reject("security review source cleanliness is invalid")
    return {
        "repository": repository,
        "source_revision": revision,
        "clean": clean,
    }


def _current_source(repo_root: Path) -> dict[str, object]:
    try:
        facts = _source_facts(Path(repo_root), require_clean=True)
    except ValidationError as exc:
        raise ValidationError(
            "security review current source is unavailable or dirty"
        ) from exc
    return _validate_source_facts(facts)


def _validate_reviewer(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "reference"}:
        _reject("security review reviewer is invalid")
    kind = value.get("kind")
    if kind not in REVIEWER_KINDS:
        _reject("security review reviewer kind is unsupported")
    reference = _safe_ref(value.get("reference"), field="reviewer reference")
    assert isinstance(reference, str)
    return {"kind": str(kind), "reference": reference}


def _validate_controls(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(REQUIRED_CONTROL_IDS):
        _reject("security review controls must cover the complete required scope")
    by_id: dict[str, dict[str, object]] = {}
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "control_id",
            "outcome",
            "evidence_ref",
        }:
            _reject("security review control record is invalid")
        control_id = row.get("control_id")
        if control_id not in REQUIRED_CONTROL_IDS or control_id in by_id:
            _reject("security review control id is invalid or duplicated")
        outcome = row.get("outcome")
        if outcome not in CONTROL_OUTCOMES:
            _reject("security review control outcome is invalid")
        evidence_ref = _safe_ref(
            row.get("evidence_ref"),
            field="control evidence reference",
            optional=outcome == "UNKNOWN",
        )
        if outcome != "UNKNOWN" and evidence_ref is None:
            _reject("security review observed control requires evidence")
        by_id[str(control_id)] = {
            "control_id": str(control_id),
            "outcome": str(outcome),
            "evidence_ref": evidence_ref,
        }
    if set(by_id) != set(REQUIRED_CONTROL_IDS):
        _reject("security review controls must cover the complete required scope")
    return [by_id[control_id] for control_id in REQUIRED_CONTROL_IDS]


def _validate_findings(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_FINDINGS:
        _reject("security review findings exceed bounded scope")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "finding_id",
            "severity",
            "disposition",
            "evidence_ref",
        }:
            _reject("security review finding record is invalid")
        finding_id = row.get("finding_id")
        severity = row.get("severity")
        disposition = row.get("disposition")
        if (
            not isinstance(finding_id, str)
            or _FINDING_ID.fullmatch(finding_id) is None
            or finding_id in seen
        ):
            _reject("security review finding id is invalid or duplicated")
        if severity not in FINDING_SEVERITIES:
            _reject("security review finding severity is invalid")
        if disposition not in FINDING_DISPOSITIONS:
            _reject("security review finding disposition is invalid")
        evidence_ref = _safe_ref(
            row.get("evidence_ref"),
            field="finding evidence reference",
        )
        assert isinstance(evidence_ref, str)
        seen.add(finding_id)
        rows.append(
            {
                "finding_id": finding_id,
                "severity": str(severity),
                "disposition": str(disposition),
                "evidence_ref": evidence_ref,
            }
        )
    return sorted(rows, key=lambda row: row["finding_id"])


def _shape(payload: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "repository",
        "source_revision",
        "reviewed_at",
        "reviewer",
        "scope_version",
        "controls",
        "findings",
        "authority",
        "evidence_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("security review evidence schema is invalid")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION or isinstance(version, bool):
        _reject("security review evidence schema_version is unsupported")
    if payload.get("kind") != KIND:
        _reject("security review evidence kind is invalid")
    if payload.get("repository") != REPOSITORY:
        _reject("security review repository identity mismatch")
    revision = payload.get("source_revision")
    if not isinstance(revision, str) or _HEAD.fullmatch(revision) is None:
        _reject("security review source revision is invalid")
    scope = payload.get("scope_version")
    if scope != SCOPE_VERSION or isinstance(scope, bool):
        _reject("security review scope_version is unsupported")
    if payload.get("authority") != AUTHORITY:
        _reject("security review authority is invalid")
    evidence_digest = payload.get("evidence_digest")
    if not isinstance(evidence_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", evidence_digest
    ):
        _reject("security review evidence digest is invalid")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "repository": REPOSITORY,
        "source_revision": revision,
        "reviewed_at": _utc(payload.get("reviewed_at")),
        "reviewer": _validate_reviewer(payload.get("reviewer")),
        "scope_version": SCOPE_VERSION,
        "controls": _validate_controls(payload.get("controls")),
        "findings": _validate_findings(payload.get("findings")),
        "authority": AUTHORITY,
        "evidence_digest": evidence_digest,
    }
    assert_content_free(normalized)
    if contains_unsafe_secret(_canonical_bytes(normalized).decode("utf-8")):
        _reject("security review evidence contains unsafe sensitive content")
    core = dict(normalized)
    core.pop("evidence_digest")
    if _digest(core) != evidence_digest:
        _reject("security review evidence digest mismatch")
    return normalized

def derive_review_outcome(evidence: dict[str, object]) -> str:
    controls = evidence["controls"]
    findings = evidence["findings"]
    assert isinstance(controls, list)
    assert isinstance(findings, list)
    if any(
        isinstance(row, dict) and row.get("outcome") == "FAIL"
        for row in controls
    ) or any(
        isinstance(row, dict)
        and row.get("disposition") == "OPEN"
        and row.get("severity") in {"CRITICAL", "HIGH"}
        for row in findings
    ):
        return "BLOCKING_FINDINGS"
    if any(
        isinstance(row, dict) and row.get("outcome") == "UNKNOWN"
        for row in controls
    ):
        return "INCOMPLETE"
    return "NO_BLOCKING_FINDINGS"


def _summary(evidence: dict[str, object]) -> dict[str, object]:
    controls = evidence["controls"]
    findings = evidence["findings"]
    assert isinstance(controls, list)
    assert isinstance(findings, list)
    control_counts = {
        state: sum(
            isinstance(row, dict) and row.get("outcome") == state
            for row in controls
        )
        for state in ("PASS", "FAIL", "UNKNOWN")
    }
    finding_counts = {
        severity: sum(
            isinstance(row, dict)
            and row.get("severity") == severity
            and row.get("disposition") == "OPEN"
            for row in findings
        )
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    }
    return {
        "state": "VALIDATED_EVIDENCE",
        "review_outcome": derive_review_outcome(evidence),
        "authority": AUTHORITY,
        "repository": evidence["repository"],
        "source_revision": evidence["source_revision"],
        "reviewed_at": evidence["reviewed_at"],
        "reviewer": dict(evidence["reviewer"]),  # type: ignore[arg-type]
        "reviewer_attribution": "DECLARED_ONLY",
        "scope_version": evidence["scope_version"],
        "control_counts": control_counts,
        "finding_counts": finding_counts,
        "finding_count": len(findings),
        "evidence_digest": evidence["evidence_digest"],
        "detail": (
            "exact-source bounded security review evidence validates; reviewer "
            "identity is declared, not authenticated; this evidence does not grant "
            "release, deploy, merge, or PASS authority"
        ),
    }


def build_security_review_evidence_from_facts(
    *,
    source_facts: dict[str, object],
    reviewed_at: str,
    reviewer: dict[str, object],
    controls: list[dict[str, object]],
    findings: list[dict[str, object]],
) -> dict[str, object]:
    """Build a content-addressed evidence envelope from already-reviewed facts.

    This helper records caller-supplied review facts. It does not perform or
    certify the review.
    """
    source = _validate_source_facts(source_facts)
    if source["clean"] is not True:
        _reject("security review evidence requires a clean reviewed source")
    core: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "repository": source["repository"],
        "source_revision": source["source_revision"],
        "reviewed_at": _utc(reviewed_at),
        "reviewer": _validate_reviewer(reviewer),
        "scope_version": SCOPE_VERSION,
        "controls": _validate_controls(controls),
        "findings": _validate_findings(findings),
        "authority": AUTHORITY,
    }
    assert_content_free(core)
    if contains_unsafe_secret(_canonical_bytes(core).decode("utf-8")):
        _reject("security review evidence contains unsafe sensitive content")
    payload = {**core, "evidence_digest": _digest(core)}
    return _shape(payload)


def validate_security_review_evidence(
    payload: object,
    *,
    repo_root: Path | None = None,
    source_facts: dict[str, object] | None = None,
    require_current_source: bool = True,
) -> dict[str, object]:
    """Validate evidence and optionally bind it to the exact current clean source."""
    evidence = _shape(payload)
    if require_current_source:
        current = (
            _validate_source_facts(source_facts)
            if source_facts is not None
            else _current_source(
                Path(repo_root)
                if repo_root is not None
                else Path(__file__).resolve().parents[1]
            )
        )
        if current["clean"] is not True:
            _reject("security review current source is unavailable or dirty")
        if (
            current["repository"] != evidence["repository"]
            or current["source_revision"] != evidence["source_revision"]
        ):
            _reject("security review evidence does not match current source")
    return evidence


def load_security_review_evidence(
    path: Path,
    *,
    repo_root: Path | None = None,
    require_current_source: bool = True,
) -> dict[str, object]:
    return validate_security_review_evidence(
        _load_json(Path(path)),
        repo_root=repo_root,
        require_current_source=require_current_source,
    )


def publish_security_review_evidence(
    data_root: Path,
    evidence_path: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("security review data root is not a directory")
    evidence = load_security_review_evidence(
        Path(evidence_path),
        repo_root=repo_root,
        require_current_source=True,
    )
    text = json.dumps(
        evidence,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    with data_root_write_lock(root):
        atomic_write_text(root / FILENAME, text)
    return _summary(evidence)


def security_review_dashboard(
    data_root: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, object]:
    root = Path(data_root)
    unknown = {
        "state": "UNKNOWN",
        "review_outcome": "UNKNOWN",
        "authority": AUTHORITY,
        "repository": REPOSITORY,
        "source_revision": "UNKNOWN",
        "reviewed_at": None,
        "reviewer": None,
        "reviewer_attribution": "DECLARED_ONLY",
        "scope_version": SCOPE_VERSION,
        "control_counts": {"PASS": 0, "FAIL": 0, "UNKNOWN": len(REQUIRED_CONTROL_IDS)},
        "finding_counts": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
        "finding_count": 0,
        "evidence_digest": None,
        "detail": "no standalone exact-source security review evidence is published",
    }
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return {
            **unknown,
            "state": "INVALID_OR_STALE_EVIDENCE",
            "detail": (
                "published security review evidence data root is unsafe "
                "or is not a directory"
            ),
        }
    path = root / FILENAME
    if not path.exists() and not path.is_symlink():
        return unknown
    try:
        evidence = load_security_review_evidence(
            path,
            repo_root=repo_root,
            require_current_source=True,
        )
    except ValidationError:
        return {
            **unknown,
            "state": "INVALID_OR_STALE_EVIDENCE",
            "detail": (
                "published security review evidence is unsafe, invalid, stale, "
                "or does not match the current clean source"
            ),
        }
    return _summary(evidence)
