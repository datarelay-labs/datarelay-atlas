"""First bounded Decision Plane LIMITED_ACTIVE effect.

Only OPTIONAL_CONTEXT_SELECTION is admitted. Atlas still prepares and validates
the complete candidate set and mandatory repository context. A selector may
choose among optional candidates once; malformed/unsafe/error results fall back
to the deterministic current choice (all prepared candidates).
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.decision_plane import build_optional_context_candidates
from atlas.decision_plane_canary import decision_canary_dashboard
from atlas.provenance import PROJECT_ID_RE, ValidationError, validate_source_path

SCHEMA_VERSION = 1
LEDGER_KIND = "decision_plane_limited_active_ledger"
RECEIPT_KIND = "decision_plane_optional_context_effect_receipt"
FILENAME = "decision-plane-limited-active.json"
AUTHORITY = "OPTIONAL_CONTEXT_SELECTION_ONLY"
PERMISSION_AUTHORITY = "NONE"
RELEASE_AUTHORITY = "NONE"
DEPLOY_AUTHORITY = "NONE"
PASS_AUTHORITY = "NONE"
HUMAN_REQUIRED_AUTHORITY = "NONE"
DECISION_CLASS = "OPTIONAL_CONTEXT_SELECTION"
RESULTS = frozenset({"APPLIED_CANARY", "FALLBACK"})
_MAX_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 500
_MAX_CANDIDATES = 128
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+#\-]{0,255}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")


class OptionalContextSelectorPort(Protocol):
    def select(self, request: dict[str, Any]) -> object: ...


def _reject(message: str) -> None:
    raise ValidationError(message)


def _id(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER if provider else _ID
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or _SECRET.search(value) is not None
    ):
        _reject(f"decision plane limited-active {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"decision plane limited-active {label} is invalid")
    return value


def _candidate_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        _reject(f"decision plane limited-active {label} is invalid")
    try:
        validate_source_path(value)
    except ValidationError as exc:
        raise ValidationError(
            f"decision plane limited-active {label} is invalid"
        ) from exc
    if _SECRET.search(value) is not None:
        _reject(f"decision plane limited-active {label} is invalid")
    return value


def _utc(value: object, *, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject(f"decision plane limited-active {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"decision plane limited-active {label} must be UTC"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"decision plane limited-active {label} must be UTC")
    return value, parsed


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
            "decision plane limited-active payload is not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def decision_canary_admission_digest(admission: object) -> str:
    if not isinstance(admission, dict):
        _reject("decision plane limited-active admission is invalid")
    return _canonical_digest(admission)


def _candidate_digest(candidates: dict[str, object]) -> str:
    return _canonical_digest(
        {
            "decision_class": candidates["decision_class"],
            "candidate_ids": candidates["candidate_ids"],
            "required_candidate_ids": candidates["required_candidate_ids"],
        }
    )


def _path_within_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _validate_activation_request(payload: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "activation_id",
        "expected_admission_digest",
        "project_id",
        "task_kind",
        "optional_paths",
        "activated_at",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("decision plane limited-active activation request schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != "decision_plane_optional_context_activation_request"
    ):
        _reject("decision plane limited-active activation request version/kind is invalid")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        _reject("decision plane limited-active project_id is invalid")
    optional_paths = payload.get("optional_paths")
    if (
        not isinstance(optional_paths, list)
        or len(optional_paths) > _MAX_CANDIDATES
        or any(not isinstance(item, str) for item in optional_paths)
    ):
        _reject("decision plane limited-active optional paths are invalid")
    normalized_paths: list[str] = []
    for raw in optional_paths:
        value = raw.rstrip("/")
        try:
            validate_source_path(value)
        except ValidationError as exc:
            raise ValidationError(
                "decision plane limited-active optional path is invalid"
            ) from exc
        normalized_paths.append(value)
    if len(normalized_paths) != len(set(normalized_paths)):
        _reject("decision plane limited-active optional paths are duplicated")
    activated_at, _ = _utc(payload.get("activated_at"), label="activated_at")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "decision_plane_optional_context_activation_request",
        "activation_id": _id(payload.get("activation_id"), label="activation_id"),
        "expected_admission_digest": _digest(
            payload.get("expected_admission_digest"),
            label="expected_admission_digest",
        ),
        "project_id": project_id,
        "task_kind": _id(payload.get("task_kind"), label="task_kind"),
        "optional_paths": sorted(normalized_paths),
        "activated_at": activated_at,
    }


def _current_admission(
    data_root: Path,
    *,
    expected_digest: str,
    project_id: str,
    task_kind: str,
    optional_paths: list[str],
    activated_at: str,
) -> tuple[dict[str, object], str]:
    dashboard = decision_canary_dashboard(data_root)
    if dashboard.get("binding_state") != "CURRENT":
        _reject("decision plane limited-active canary admission is not current")
    if dashboard.get("effective_decision") != "CANARY_ELIGIBLE":
        _reject("decision plane limited-active canary admission is not eligible")
    admission = dashboard.get("admission")
    if not isinstance(admission, dict):
        _reject("decision plane limited-active canary admission is unavailable")
    if admission.get("decision_class") != DECISION_CLASS:
        _reject("decision plane limited-active decision class is not supported")
    digest = decision_canary_admission_digest(admission)
    if digest != expected_digest:
        _reject("decision plane limited-active admission digest mismatch")

    scope = admission.get("scope")
    if not isinstance(scope, dict):
        _reject("decision plane limited-active canary scope is invalid")
    if scope.get("project_id") != project_id:
        _reject("decision plane limited-active project is outside canary scope")
    task_kinds = scope.get("task_kinds")
    if not isinstance(task_kinds, list) or task_kind not in task_kinds:
        _reject("decision plane limited-active task kind is outside canary scope")
    prefixes = scope.get("path_prefixes")
    if not isinstance(prefixes, list):
        _reject("decision plane limited-active path scope is invalid")
    for path in optional_paths:
        if not any(
            isinstance(prefix, str) and _path_within_prefix(path, prefix)
            for prefix in prefixes
        ):
            _reject("decision plane limited-active optional path is outside canary scope")

    _, activated = _utc(activated_at, label="activated_at")
    _, evaluated = _utc(admission.get("evaluated_at"), label="admission evaluated_at")
    _, expires = _utc(admission.get("expires_at"), label="admission expires_at")
    if activated < evaluated or activated >= expires:
        _reject("decision plane limited-active activation is outside admission window")
    return admission, digest


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "authority": AUTHORITY,
        "records": [],
    }


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        _reject("decision plane limited-active ledger path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            "decision plane limited-active ledger is unreadable"
        ) from exc
    if len(raw) > _MAX_BYTES:
        _reject("decision plane limited-active ledger exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "decision plane limited-active ledger is invalid JSON"
        ) from exc
    assert_content_free(payload)
    return payload


def _validate_selector_attribution(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    expected = {"provider", "model", "profile", "decision_ref"}
    if not isinstance(value, dict) or set(value) != expected:
        return None
    try:
        return {
            "provider": _id(value.get("provider"), label="provider", provider=True),
            "model": _id(value.get("model"), label="model"),
            "profile": _id(value.get("profile"), label="profile"),
            "decision_ref": _id(value.get("decision_ref"), label="decision_ref"),
        }
    except ValidationError:
        return None


def _normalize_selector_result(
    raw: object,
    *,
    candidate_ids: list[str],
    required_candidate_ids: list[str],
) -> tuple[str, list[str], str | None, dict[str, str] | None]:
    current_choice = list(candidate_ids)
    if not isinstance(raw, dict) or set(raw) != {
        "selected_candidate_ids",
        "provider",
        "model",
        "profile",
        "decision_ref",
    }:
        return "FALLBACK", current_choice, "RESULT_INVALID", None
    selected = raw.get("selected_candidate_ids")
    if not isinstance(selected, list) or not selected:
        return "FALLBACK", current_choice, "CHOICE_EMPTY", None
    try:
        normalized = [_candidate_path(item, label="selected_candidate_id") for item in selected]
    except ValidationError:
        return "FALLBACK", current_choice, "RESULT_INVALID", None
    if len(normalized) != len(set(normalized)):
        return "FALLBACK", current_choice, "CHOICE_DUPLICATED", None
    selected_set = set(normalized)
    candidate_set = set(candidate_ids)
    required_set = set(required_candidate_ids)
    if not selected_set.issubset(candidate_set):
        return "FALLBACK", current_choice, "CHOICE_OUTSIDE_CANDIDATES", None
    if not required_set.issubset(selected_set):
        return "FALLBACK", current_choice, "CHOICE_MISSING_REQUIRED", None
    attribution = _validate_selector_attribution(
        {
            "provider": raw.get("provider"),
            "model": raw.get("model"),
            "profile": raw.get("profile"),
            "decision_ref": raw.get("decision_ref"),
        }
    )
    if attribution is None:
        return "FALLBACK", current_choice, "ATTRIBUTION_INVALID", None
    return "APPLIED_CANARY", sorted(normalized), None, attribution


def _receipt_digest(receipt: object) -> str:
    return _canonical_digest(receipt)


def validate_optional_context_effect_receipt(
    payload: object,
    *,
    expected_receipt_digest: str,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "activation_id",
        "canary_request_id",
        "admission_digest",
        "decision_class",
        "project_id",
        "task_kind",
        "activated_at",
        "candidate_digest",
        "candidate_ids",
        "required_candidate_ids",
        "selected_candidate_ids",
        "result",
        "fallback_reason",
        "selector_attribution",
        "authority",
        "permission_authority",
        "release_authority",
        "deploy_authority",
        "pass_authority",
        "human_required_authority",
        "rollout_state",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("decision plane limited-active receipt schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != RECEIPT_KIND
        or payload.get("decision_class") != DECISION_CLASS
        or payload.get("result") not in RESULTS
        or payload.get("authority") != AUTHORITY
        or payload.get("permission_authority") != PERMISSION_AUTHORITY
        or payload.get("release_authority") != RELEASE_AUTHORITY
        or payload.get("deploy_authority") != DEPLOY_AUTHORITY
        or payload.get("pass_authority") != PASS_AUTHORITY
        or payload.get("human_required_authority") != HUMAN_REQUIRED_AUTHORITY
        or payload.get("rollout_state") != "LIMITED_ACTIVE"
    ):
        _reject("decision plane limited-active receipt authority/state is invalid")
    _id(payload.get("activation_id"), label="activation_id")
    _id(payload.get("canary_request_id"), label="canary_request_id")
    _digest(payload.get("admission_digest"), label="admission_digest")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        _reject("decision plane limited-active receipt project_id is invalid")
    _id(payload.get("task_kind"), label="task_kind")
    _utc(payload.get("activated_at"), label="activated_at")
    _digest(payload.get("candidate_digest"), label="candidate_digest")

    candidates = payload.get("candidate_ids")
    required = payload.get("required_candidate_ids")
    selected = payload.get("selected_candidate_ids")
    if (
        not isinstance(candidates, list)
        or not candidates
        or len(candidates) > _MAX_CANDIDATES
        or not isinstance(required, list)
        or not isinstance(selected, list)
        or not selected
    ):
        _reject("decision plane limited-active receipt candidates are invalid")
    candidate_ids = [_candidate_path(item, label="candidate_id") for item in candidates]
    required_ids = [_candidate_path(item, label="required_candidate_id") for item in required]
    selected_ids = [_candidate_path(item, label="selected_candidate_id") for item in selected]
    if (
        candidate_ids != sorted(set(candidate_ids))
        or required_ids != sorted(set(required_ids))
        or selected_ids != sorted(set(selected_ids))
        or not set(required_ids).issubset(set(candidate_ids))
    ):
        _reject("decision plane limited-active receipt candidate identity is invalid")
    if _candidate_digest(
        {
            "decision_class": DECISION_CLASS,
            "candidate_ids": candidate_ids,
            "required_candidate_ids": required_ids,
        }
    ) != payload["candidate_digest"]:
        _reject("decision plane limited-active candidate digest mismatch")

    result = str(payload["result"])
    reason = payload.get("fallback_reason")
    attribution = payload.get("selector_attribution")
    if result == "APPLIED_CANARY":
        if reason is not None:
            _reject("applied Decision Plane choice cannot claim fallback reason")
        if not set(selected_ids).issubset(set(candidate_ids)):
            _reject("applied Decision Plane choice is outside candidates")
        if not set(required_ids).issubset(set(selected_ids)):
            _reject("applied Decision Plane choice omitted required context")
        if _validate_selector_attribution(attribution) is None:
            _reject("applied Decision Plane selector attribution is invalid")
    else:
        if not isinstance(reason, str) or _ID.fullmatch(reason) is None:
            _reject("decision plane limited-active fallback reason is invalid")
        if selected_ids != candidate_ids:
            _reject("Decision Plane fallback must preserve current candidate choice")
        if attribution is not None:
            _reject("Decision Plane fallback cannot retain selector attribution")

    receipt_digest = _digest(expected_receipt_digest, label="receipt_digest")
    if _receipt_digest(payload) != receipt_digest:
        _reject("decision plane limited-active receipt digest mismatch")
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
        _reject("decision plane limited-active ledger schema is invalid")
    seen_ids: set[str] = set()
    records: list[dict[str, object]] = []
    expected_keys = {
        "activation_id",
        "admission_digest",
        "canary_request_id",
        "state",
        "candidate_digest",
        "receipt",
        "receipt_digest",
    }
    for raw in payload["records"]:
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            _reject("decision plane limited-active ledger record is invalid")
        activation_id = _id(raw.get("activation_id"), label="activation_id")
        admission_digest = _digest(raw.get("admission_digest"), label="admission_digest")
        canary_request_id = _id(raw.get("canary_request_id"), label="canary_request_id")
        candidate_digest = _digest(raw.get("candidate_digest"), label="candidate_digest")
        if activation_id in seen_ids:
            _reject("decision plane limited-active activation identity is duplicated")
        state = raw.get("state")
        if state == "IN_PROGRESS":
            if raw.get("receipt") is not None or raw.get("receipt_digest") is not None:
                _reject("decision plane limited-active reservation is invalid")
        elif state == "TERMINAL":
            digest = _digest(raw.get("receipt_digest"), label="receipt_digest")
            receipt = validate_optional_context_effect_receipt(
                raw.get("receipt"),
                expected_receipt_digest=digest,
            )
            if (
                receipt["activation_id"] != activation_id
                or receipt["admission_digest"] != admission_digest
                or receipt["canary_request_id"] != canary_request_id
                or receipt["candidate_digest"] != candidate_digest
            ):
                _reject("decision plane limited-active terminal binding is invalid")
        else:
            _reject("decision plane limited-active ledger state is invalid")
        seen_ids.add(activation_id)
        records.append(dict(raw))
    return {**_empty_ledger(), "records": records}


def _reserve(
    root: Path,
    *,
    activation_id: str,
    admission: dict[str, object],
    admission_digest: str,
    candidate_digest: str,
) -> None:
    ledger = _load_ledger(root)
    if len(ledger["records"]) >= _MAX_RECORDS:
        _reject("decision plane limited-active ledger limit reached")
    if any(item["activation_id"] == activation_id for item in ledger["records"]):
        _reject("decision plane limited-active activation replay is not allowed")
    used = sum(
        item["admission_digest"] == admission_digest for item in ledger["records"]
    )
    max_decisions = admission.get("max_canary_decisions")
    if (
        isinstance(max_decisions, bool)
        or not isinstance(max_decisions, int)
        or used >= max_decisions
    ):
        _reject("decision plane limited-active canary decision budget is exhausted")
    ledger["records"].append(
        {
            "activation_id": activation_id,
            "admission_digest": admission_digest,
            "canary_request_id": admission["request_id"],
            "state": "IN_PROGRESS",
            "candidate_digest": candidate_digest,
            "receipt": None,
            "receipt_digest": None,
        }
    )
    atomic_write_text(
        root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )


def _finalize(
    root: Path,
    *,
    activation_id: str,
    receipt: dict[str, object],
    receipt_digest: str,
) -> None:
    ledger = _load_ledger(root)
    matches = [
        item for item in ledger["records"] if item["activation_id"] == activation_id
    ]
    if len(matches) != 1 or matches[0]["state"] != "IN_PROGRESS":
        _reject("decision plane limited-active reservation is not current")
    validate_optional_context_effect_receipt(
        receipt, expected_receipt_digest=receipt_digest
    )
    for item in ledger["records"]:
        if item["activation_id"] == activation_id:
            item["state"] = "TERMINAL"
            item["receipt"] = receipt
            item["receipt_digest"] = receipt_digest
    atomic_write_text(
        root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )


def commit_optional_context_effect(
    data_root: Path,
    *,
    repo_root: Path,
    request: object,
    selector_port: OptionalContextSelectorPort,
) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("decision plane limited-active data root is not a directory")
    normalized = _validate_activation_request(request)
    admission, admission_digest = _current_admission(
        root,
        expected_digest=str(normalized["expected_admission_digest"]),
        project_id=str(normalized["project_id"]),
        task_kind=str(normalized["task_kind"]),
        optional_paths=list(normalized["optional_paths"]),
        activated_at=str(normalized["activated_at"]),
    )
    candidates = build_optional_context_candidates(
        Path(repo_root).resolve(),
        list(normalized["optional_paths"]),
    )
    candidate_ids = list(candidates["candidate_ids"])
    required_ids = list(candidates["required_candidate_ids"])
    candidate_digest = _candidate_digest(candidates)

    with data_root_write_lock(root):
        # Re-read the admission while holding the reservation lock so replay
        # evidence cannot drift between admission validation and reservation.
        fresh, fresh_digest = _current_admission(
            root,
            expected_digest=admission_digest,
            project_id=str(normalized["project_id"]),
            task_kind=str(normalized["task_kind"]),
            optional_paths=list(normalized["optional_paths"]),
            activated_at=str(normalized["activated_at"]),
        )
        if fresh != admission or fresh_digest != admission_digest:
            _reject("decision plane limited-active admission drifted before reservation")
        _reserve(
            root,
            activation_id=str(normalized["activation_id"]),
            admission=admission,
            admission_digest=admission_digest,
            candidate_digest=candidate_digest,
        )

    selector_request = {
        "activation_id": normalized["activation_id"],
        "decision_class": DECISION_CLASS,
        "admission_digest": admission_digest,
        "canary_request_id": admission["request_id"],
        "project_id": normalized["project_id"],
        "task_kind": normalized["task_kind"],
        "candidate_ids": candidate_ids,
        "required_candidate_ids": required_ids,
    }
    try:
        raw = selector_port.select(selector_request)
    except Exception:
        result, selected, reason, attribution = (
            "FALLBACK",
            candidate_ids,
            "SELECTOR_ERROR",
            None,
        )
    else:
        result, selected, reason, attribution = _normalize_selector_result(
            raw,
            candidate_ids=candidate_ids,
            required_candidate_ids=required_ids,
        )

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "activation_id": normalized["activation_id"],
        "canary_request_id": admission["request_id"],
        "admission_digest": admission_digest,
        "decision_class": DECISION_CLASS,
        "project_id": normalized["project_id"],
        "task_kind": normalized["task_kind"],
        "activated_at": normalized["activated_at"],
        "candidate_digest": candidate_digest,
        "candidate_ids": candidate_ids,
        "required_candidate_ids": required_ids,
        "selected_candidate_ids": selected,
        "result": result,
        "fallback_reason": reason,
        "selector_attribution": attribution,
        "authority": AUTHORITY,
        "permission_authority": PERMISSION_AUTHORITY,
        "release_authority": RELEASE_AUTHORITY,
        "deploy_authority": DEPLOY_AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "human_required_authority": HUMAN_REQUIRED_AUTHORITY,
        "rollout_state": "LIMITED_ACTIVE",
    }
    receipt_digest = _receipt_digest(receipt)
    validate_optional_context_effect_receipt(
        receipt, expected_receipt_digest=receipt_digest
    )
    with data_root_write_lock(root):
        _finalize(
            root,
            activation_id=str(normalized["activation_id"]),
            receipt=receipt,
            receipt_digest=receipt_digest,
        )
    return {**receipt, "receipt_digest": receipt_digest}


def limited_active_dashboard(
    data_root: Path,
    *,
    repo_root: Path,
) -> dict[str, object]:
    root = Path(data_root)
    ledger = _load_ledger(root)
    records = list(ledger["records"])
    terminal = [item for item in records if item["state"] == "TERMINAL"]
    in_progress = [item for item in records if item["state"] == "IN_PROGRESS"]
    counts = {name: 0 for name in sorted(RESULTS)}
    for item in terminal:
        receipt = item["receipt"]
        assert isinstance(receipt, dict)
        counts[str(receipt["result"])] += 1
    binding_state = "UNKNOWN"
    latest = terminal[-1] if terminal else None
    if isinstance(latest, dict):
        receipt = latest["receipt"]
        assert isinstance(receipt, dict)
        try:
            dashboard = decision_canary_dashboard(root)
            admission = dashboard.get("admission")
            current_digest = (
                decision_canary_admission_digest(admission)
                if isinstance(admission, dict)
                else None
            )
            candidates = build_optional_context_candidates(
                Path(repo_root).resolve(),
                [
                    path
                    for path in receipt["candidate_ids"]
                    if path not in set(receipt["required_candidate_ids"])
                ],
            )
            current_candidate_digest = _candidate_digest(candidates)
        except ValidationError:
            binding_state = "STALE"
        else:
            binding_state = (
                "CURRENT"
                if (
                    dashboard.get("binding_state") == "CURRENT"
                    and dashboard.get("effective_decision") == "CANARY_ELIGIBLE"
                    and current_digest == receipt["admission_digest"]
                    and current_candidate_digest == receipt["candidate_digest"]
                )
                else "STALE"
            )
    return {
        "state": "OBSERVED" if records else "UNKNOWN",
        "authority": AUTHORITY,
        "permission_authority": PERMISSION_AUTHORITY,
        "release_authority": RELEASE_AUTHORITY,
        "deploy_authority": DEPLOY_AUTHORITY,
        "pass_authority": PASS_AUTHORITY,
        "human_required_authority": HUMAN_REQUIRED_AUTHORITY,
        "rollout_state": "LIMITED_ACTIVE",
        "effect_count": len(records),
        "terminal_count": len(terminal),
        "in_progress_count": len(in_progress),
        "result_counts": counts,
        "binding_state": binding_state,
        "latest_effect": latest,
        "effects": terminal[-50:],
    }
