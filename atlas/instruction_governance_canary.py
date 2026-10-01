"""Canary evidence gate for instruction-governance PR/adoption handoff.

This module consumes only a published PR_CANDIDATE disposition. It records
bounded external canary evidence and may emit an ordinary PR/adoption request
artifact. It never mutates repository files or GitHub and grants no merge,
release, or default-branch authority.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.instruction_governance import (
    DISPOSITION_AUTHORITY,
    build_instruction_governance_disposition,
    instruction_governance_disposition_dashboard,
)
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
OBSERVATION_KIND = "instruction_governance_canary_observation"
RECORD_KIND = "instruction_governance_canary_record"
LEDGER_KIND = "instruction_governance_canary_ledger"
FILENAME = "instruction-governance-canaries.json"
AUTHORITY = "CANARY_EVIDENCE_ONLY"
ADOPTION_REQUEST_KIND = "ordinary_pr_or_managed_adoption_request"
OUTCOMES = frozenset({"PASS", "FAIL", "HUMAN_REQUIRED"})
_MAX_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 500
_MAX_CHECKS = 128
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")


def _reject(message: str) -> None:
    raise ValidationError(message)


def _id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or _ID.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"instruction governance canary {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"instruction governance canary {label} is invalid")
    return value


def _utc(value: object) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("instruction governance canary timestamp must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("instruction governance canary timestamp must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("instruction governance canary timestamp must be UTC")
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
        raise ValidationError("instruction governance canary payload is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        _reject("instruction governance canary ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("instruction governance canary ledger is unreadable") from exc
    if len(raw) > _MAX_BYTES:
        _reject("instruction governance canary ledger exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("instruction governance canary ledger is invalid JSON") from exc
    assert_content_free(payload)
    return payload


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "authority": AUTHORITY,
        "records": [],
    }


def _validate_change(item: object) -> dict[str, str]:
    if not isinstance(item, dict) or set(item) != {"path", "before_digest", "after_digest"}:
        _reject("instruction governance canary managed change is invalid")
    path = _id(item.get("path"), label="managed path")
    before = _digest(item.get("before_digest"), label="before_digest")
    after = _digest(item.get("after_digest"), label="after_digest")
    if before == after:
        _reject("instruction governance canary managed change is unchanged")
    return {"path": path, "before_digest": before, "after_digest": after}


def _validate_adoption_request(payload: object) -> dict[str, object] | None:
    if payload is None:
        return None
    keys = {
        "kind", "target_repository", "target_head", "audit_identity",
        "disposition_digest", "handoff_digest", "canary_digest",
        "managed_changes", "canary_evidence_refs", "required_route",
        "mutation_authority", "merge_authority", "release_authority",
        "default_branch_authority", "request_digest",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        _reject("instruction governance canary adoption request schema is invalid")
    if (
        payload.get("kind") != ADOPTION_REQUEST_KIND
        or payload.get("required_route") != "ORDINARY_PR_OR_MANAGED_ADOPTION"
        or payload.get("mutation_authority") != "NONE"
        or payload.get("merge_authority") != "NONE"
        or payload.get("release_authority") != "NONE"
        or payload.get("default_branch_authority") != "NONE"
    ):
        _reject("instruction governance canary adoption request authority is invalid")
    repository = payload.get("target_repository")
    head = payload.get("target_head")
    if not isinstance(repository, str) or _REPO.fullmatch(repository) is None:
        _reject("instruction governance canary adoption repository is invalid")
    if not isinstance(head, str) or _SHA40.fullmatch(head) is None:
        _reject("instruction governance canary adoption target_head is invalid")
    for key in ("audit_identity", "disposition_digest", "handoff_digest", "canary_digest"):
        _digest(payload.get(key), label=key)
    changes = payload.get("managed_changes")
    if not isinstance(changes, list) or not changes or len(changes) > 32:
        _reject("instruction governance canary adoption managed changes are invalid")
    normalized_changes = [_validate_change(item) for item in changes]
    paths = [item["path"] for item in normalized_changes]
    if len(paths) != len(set(paths)):
        _reject("instruction governance canary adoption managed changes are duplicated")
    normalized_changes.sort(key=lambda item: item["path"])
    refs = payload.get("canary_evidence_refs")
    if not isinstance(refs, list) or not refs or len(refs) > _MAX_CHECKS:
        _reject("instruction governance canary evidence refs are invalid")
    normalized_refs = [_id(item, label="evidence_ref") for item in refs]
    if len(normalized_refs) != len(set(normalized_refs)):
        _reject("instruction governance canary evidence refs are duplicated")
    request_digest = _digest(payload.get("request_digest"), label="request_digest")
    body = {key: value for key, value in payload.items() if key != "request_digest"}
    if _canonical_digest(body) != request_digest:
        _reject("instruction governance canary adoption request digest mismatch")
    return {
        **body,
        "managed_changes": normalized_changes,
        "canary_evidence_refs": normalized_refs,
        "request_digest": request_digest,
    }


def validate_instruction_governance_canary_record(payload: object) -> dict[str, object]:
    keys = {
        "schema_version", "kind", "authority", "canary_id", "evaluated_at",
        "audit_identity", "disposition_digest", "handoff_digest",
        "target_repository", "target_head", "managed_changes", "result",
        "check_count", "checks", "adoption_request", "canary_digest",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        _reject("instruction governance canary record schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != RECORD_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("result") not in OUTCOMES
    ):
        _reject("instruction governance canary record authority/result is invalid")
    canary_id = _id(payload.get("canary_id"), label="canary_id")
    evaluated_at = _utc(payload.get("evaluated_at"))
    audit_identity = _digest(payload.get("audit_identity"), label="audit_identity")
    disposition_digest = _digest(payload.get("disposition_digest"), label="disposition_digest")
    handoff_digest = _digest(payload.get("handoff_digest"), label="handoff_digest")
    repository = payload.get("target_repository")
    head = payload.get("target_head")
    if not isinstance(repository, str) or _REPO.fullmatch(repository) is None:
        _reject("instruction governance canary target repository is invalid")
    if not isinstance(head, str) or _SHA40.fullmatch(head) is None:
        _reject("instruction governance canary target head is invalid")

    changes = payload.get("managed_changes")
    if not isinstance(changes, list) or not changes or len(changes) > 32:
        _reject("instruction governance canary managed changes are invalid")
    normalized_changes = [_validate_change(item) for item in changes]
    paths = [item["path"] for item in normalized_changes]
    if len(paths) != len(set(paths)):
        _reject("instruction governance canary managed changes are duplicated")
    normalized_changes.sort(key=lambda item: item["path"])

    checks = payload.get("checks")
    count = payload.get("check_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or count > _MAX_CHECKS
        or not isinstance(checks, list)
        or len(checks) != count
    ):
        _reject("instruction governance canary checks are invalid")
    normalized_checks = []
    seen: set[str] = set()
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"check_id", "outcome", "evidence_ref"}:
            _reject("instruction governance canary check schema is invalid")
        check_id = _id(item.get("check_id"), label="check_id")
        if check_id in seen:
            _reject("instruction governance canary check is duplicated")
        seen.add(check_id)
        outcome = item.get("outcome")
        if outcome not in OUTCOMES:
            _reject("instruction governance canary check outcome is invalid")
        normalized_checks.append({
            "check_id": check_id,
            "outcome": outcome,
            "evidence_ref": _id(item.get("evidence_ref"), label="evidence_ref"),
        })
    normalized_checks.sort(key=lambda item: item["check_id"])
    outcomes = [item["outcome"] for item in normalized_checks]
    expected_result = (
        "HUMAN_REQUIRED"
        if "HUMAN_REQUIRED" in outcomes
        else ("FAIL" if "FAIL" in outcomes else "PASS")
    )
    if payload.get("result") != expected_result:
        _reject("instruction governance canary aggregate result is inconsistent")

    adoption = _validate_adoption_request(payload.get("adoption_request"))
    if expected_result == "PASS":
        if adoption is None:
            _reject("instruction governance canary PASS requires adoption request")
        expected_binding = {
            "target_repository": repository,
            "target_head": head,
            "audit_identity": audit_identity,
            "disposition_digest": disposition_digest,
            "handoff_digest": handoff_digest,
            "managed_changes": normalized_changes,
            "canary_evidence_refs": [item["evidence_ref"] for item in normalized_checks],
        }
        for key, value in expected_binding.items():
            if adoption[key] != value:
                _reject("instruction governance canary adoption request binding is invalid")
    elif adoption is not None:
        _reject("non-PASS instruction governance canary cannot request adoption")

    canary_digest = _digest(payload.get("canary_digest"), label="canary_digest")
    body = {key: value for key, value in payload.items() if key != "canary_digest"}
    # The adoption request binds to the enclosing canary digest. Exclude the
    # adoption object from the canary digest to avoid a circular hash.
    digest_body = {**body, "adoption_request": None}
    if _canonical_digest(digest_body) != canary_digest:
        _reject("instruction governance canary digest mismatch")
    if adoption is not None and adoption["canary_digest"] != canary_digest:
        _reject("instruction governance canary adoption canary_digest is invalid")
    assert_content_free(payload)
    return {
        **body,
        "managed_changes": normalized_changes,
        "checks": normalized_checks,
        "adoption_request": adoption,
        "canary_digest": canary_digest,
    }


def _load_ledger(root: Path) -> dict[str, object]:
    path = root / FILENAME
    if path.is_symlink():
        _reject("instruction governance canary ledger path is unsafe")
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
        _reject("instruction governance canary ledger schema is invalid")
    records = []
    seen_ids: set[str] = set()
    seen_handoffs: set[str] = set()
    for raw in payload["records"]:
        item = validate_instruction_governance_canary_record(raw)
        if item["canary_id"] in seen_ids or item["handoff_digest"] in seen_handoffs:
            _reject("instruction governance canary replay identity is duplicated")
        seen_ids.add(str(item["canary_id"]))
        seen_handoffs.add(str(item["handoff_digest"]))
        records.append(item)
    return {**_empty_ledger(), "records": records}


def _current_pr_candidate(
    data_root: Path,
    *,
    repo_root: Path,
    audit_identity: str,
    expected_disposition_digest: str,
    expected_handoff_digest: str,
) -> dict[str, object]:
    dashboard = instruction_governance_disposition_dashboard(data_root, repo_root=repo_root)
    matches = [
        item for item in dashboard["dispositions"]
        if item.get("audit_identity") == audit_identity
    ]
    if len(matches) != 1:
        _reject("instruction governance canary PR_CANDIDATE disposition is not found")
    disposition = matches[0]
    if disposition.get("authority") != DISPOSITION_AUTHORITY:
        _reject("instruction governance canary disposition authority is invalid")
    if disposition.get("disposition") != "PR_CANDIDATE":
        _reject("instruction governance canary requires PR_CANDIDATE disposition")
    if disposition.get("disposition_digest") != expected_disposition_digest:
        _reject("instruction governance canary disposition digest mismatch")
    handoff = disposition.get("pr_handoff")
    if not isinstance(handoff, dict) or handoff.get("handoff_digest") != expected_handoff_digest:
        _reject("instruction governance canary handoff digest mismatch")
    current = build_instruction_governance_disposition(
        data_root,
        repo_root=repo_root,
        audit_identity=audit_identity,
    )
    if (
        current.get("disposition") != "PR_CANDIDATE"
        or current.get("disposition_digest") != disposition.get("disposition_digest")
    ):
        _reject("instruction governance canary disposition is stale")
    return disposition


def build_instruction_governance_canary_record(
    data_root: Path,
    *,
    repo_root: Path,
    observation: object,
) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "canary_id", "evaluated_at",
        "audit_identity", "disposition_digest", "handoff_digest", "checks",
    }
    if not isinstance(observation, dict) or set(observation) != expected:
        _reject("instruction governance canary observation schema is invalid")
    if (
        observation.get("schema_version") != SCHEMA_VERSION
        or isinstance(observation.get("schema_version"), bool)
        or observation.get("kind") != OBSERVATION_KIND
    ):
        _reject("instruction governance canary observation version/kind is invalid")
    canary_id = _id(observation.get("canary_id"), label="canary_id")
    evaluated_at = _utc(observation.get("evaluated_at"))
    audit_identity = _digest(observation.get("audit_identity"), label="audit_identity")
    disposition_digest = _digest(
        observation.get("disposition_digest"), label="disposition_digest"
    )
    handoff_digest = _digest(observation.get("handoff_digest"), label="handoff_digest")
    disposition = _current_pr_candidate(
        Path(data_root),
        repo_root=Path(repo_root).resolve(),
        audit_identity=audit_identity,
        expected_disposition_digest=disposition_digest,
        expected_handoff_digest=handoff_digest,
    )
    raw_checks = observation.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks or len(raw_checks) > _MAX_CHECKS:
        _reject("instruction governance canary requires bounded checks")
    checks = []
    seen: set[str] = set()
    for item in raw_checks:
        if not isinstance(item, dict) or set(item) != {"check_id", "outcome", "evidence_ref"}:
            _reject("instruction governance canary check schema is invalid")
        check_id = _id(item.get("check_id"), label="check_id")
        if check_id in seen:
            _reject("instruction governance canary check is duplicated")
        seen.add(check_id)
        outcome = item.get("outcome")
        if outcome not in OUTCOMES:
            _reject("instruction governance canary check outcome is invalid")
        checks.append({
            "check_id": check_id,
            "outcome": outcome,
            "evidence_ref": _id(item.get("evidence_ref"), label="evidence_ref"),
        })
    checks.sort(key=lambda item: item["check_id"])
    outcomes = [item["outcome"] for item in checks]
    result = (
        "HUMAN_REQUIRED"
        if "HUMAN_REQUIRED" in outcomes
        else ("FAIL" if "FAIL" in outcomes else "PASS")
    )
    handoff = disposition["pr_handoff"]
    assert isinstance(handoff, dict)
    base = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECORD_KIND,
        "authority": AUTHORITY,
        "canary_id": canary_id,
        "evaluated_at": evaluated_at,
        "audit_identity": audit_identity,
        "disposition_digest": disposition_digest,
        "handoff_digest": handoff_digest,
        "target_repository": disposition["target_repository"],
        "target_head": disposition["target_head"],
        "managed_changes": list(handoff["managed_changes"]),
        "result": result,
        "check_count": len(checks),
        "checks": checks,
        "adoption_request": None,
    }
    canary_digest = _canonical_digest(base)
    adoption = None
    if result == "PASS":
        request_body = {
            "kind": ADOPTION_REQUEST_KIND,
            "target_repository": disposition["target_repository"],
            "target_head": disposition["target_head"],
            "audit_identity": audit_identity,
            "disposition_digest": disposition_digest,
            "handoff_digest": handoff_digest,
            "canary_digest": canary_digest,
            "managed_changes": list(handoff["managed_changes"]),
            "canary_evidence_refs": [item["evidence_ref"] for item in checks],
            "required_route": "ORDINARY_PR_OR_MANAGED_ADOPTION",
            "mutation_authority": "NONE",
            "merge_authority": "NONE",
            "release_authority": "NONE",
            "default_branch_authority": "NONE",
        }
        adoption = {**request_body, "request_digest": _canonical_digest(request_body)}
    record = {**base, "adoption_request": adoption, "canary_digest": canary_digest}
    return validate_instruction_governance_canary_record(record)


def record_instruction_governance_canary(
    data_root: Path,
    *,
    repo_root: Path,
    observation: object,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("instruction governance canary data root is not a directory")
    record = build_instruction_governance_canary_record(
        root, repo_root=repo_root, observation=observation
    )
    with data_root_write_lock(root):
        ledger = _load_ledger(root)
        if len(ledger["records"]) >= _MAX_RECORDS:
            _reject("instruction governance canary ledger limit reached")
        if any(
            item["canary_id"] == record["canary_id"]
            or item["handoff_digest"] == record["handoff_digest"]
            for item in ledger["records"]
        ):
            _reject("instruction governance canary replay is not allowed")
        ledger["records"].append(record)
        ledger["records"].sort(
            key=lambda item: (str(item["evaluated_at"]), str(item["canary_id"]))
        )
        atomic_write_text(
            root / FILENAME,
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        )
    return instruction_governance_canary_dashboard(root, repo_root=repo_root)


def instruction_governance_canary_dashboard(
    data_root: Path,
    *,
    repo_root: Path,
) -> dict[str, object]:
    root = Path(data_root)
    ledger = _load_ledger(root)
    records = list(ledger["records"])
    counts = {name: 0 for name in sorted(OUTCOMES)}
    for item in records:
        counts[str(item["result"])] += 1
    latest = records[-1] if records else None
    binding_state = "UNKNOWN"
    if isinstance(latest, dict):
        try:
            _current_pr_candidate(
                root,
                repo_root=Path(repo_root).resolve(),
                audit_identity=str(latest["audit_identity"]),
                expected_disposition_digest=str(latest["disposition_digest"]),
                expected_handoff_digest=str(latest["handoff_digest"]),
            )
        except ValidationError:
            binding_state = "STALE"
        else:
            binding_state = "CURRENT"
    return {
        "state": "OBSERVED" if records else "UNKNOWN",
        "authority": AUTHORITY,
        "mutation_authority": "NONE",
        "record_count": len(records),
        "result_counts": counts,
        "binding_state": binding_state,
        "latest_record": latest,
        "records": records[-50:],
    }
