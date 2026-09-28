"""Strict Atlas binding for Engineering System learned-canary admission evidence."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from atlas.context_optimization import MAX_INPUT_BYTES
from atlas.context_shadow import _revalidate_base_context, bind_shadow_quality
from atlas.provenance import ValidationError

LEARNED_KIND = "context-learned-canary-admission-report"
LEARNED_SOURCE_KIND = "engineering_system_context_learned_canary_v1"
DATA_EGRESS_READY = "LOCAL_CANARY_EGRESS_DENY_VERIFIED"
RUNTIME_READY = "LEARNED_COMPRESSOR_LOCAL_CANARY_READY"
ENGINEERING_SYSTEM_REPO = "datarelay-labs/engineering-system"
ENGINEERING_SYSTEM_ADMISSION_HEAD = "67e44dc4c92e81ba89657fab81770b81b6eaa93f"
TRUST_BOUNDARY_KIND = "trusted_learned_canary_boundary"

_CANDIDATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SOURCE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PACKAGE_VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
_LICENSE_RE = re.compile(r"^[A-Za-z0-9.-]{1,40}$")
_SECRET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)

_TOP_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "decision",
        "setup_allowed",
        "canary_ready",
        "candidate",
        "requirements",
        "blockers",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "candidate_id",
        "source_repo",
        "source_commit",
        "package_version",
        "license",
        "integration_mode",
    }
)
_REQUIREMENT_KEYS = (
    "execution_mode",
    "endpoint_class",
    "external_egress",
    "protected_state_route",
    "deterministic_bypass",
    "exact_original_recovery_verified",
    "identifier_preservation_verified",
    "cache_behavior_verified",
    "provider_usage_capture_ready",
    "live_comparability_gate_available",
    "shadow_equivalence_gate_available",
    "trusted_runtime_evidence",
)
_REQUIREMENT_BLOCKERS = (
    ("execution_mode", "EXECUTION_MODE_NOT_LOCAL"),
    ("endpoint_class", "ENDPOINT_NOT_LOOPBACK"),
    ("external_egress", "EXTERNAL_EGRESS_NOT_DENIED"),
    ("protected_state_route", "PROTECTED_STATE_NOT_BYPASSED"),
    ("deterministic_bypass", "DETERMINISTIC_BYPASS_UNVERIFIED"),
    ("exact_original_recovery_verified", "EXACT_RECOVERY_UNVERIFIED"),
    ("identifier_preservation_verified", "IDENTIFIER_PRESERVATION_UNVERIFIED"),
    ("cache_behavior_verified", "CACHE_BEHAVIOR_UNVERIFIED"),
    ("provider_usage_capture_ready", "PROVIDER_USAGE_CAPTURE_NOT_READY"),
    ("live_comparability_gate_available", "LIVE_COMPARABILITY_GATE_UNAVAILABLE"),
    ("shadow_equivalence_gate_available", "SHADOW_EQUIVALENCE_GATE_UNAVAILABLE"),
)
_TRUST_BLOCKERS = frozenset(
    {
        "RUNTIME_BINDING_REQUIRED",
        "TRUST_BOUNDARY_REQUIRED",
        "TRUST_BOUNDARY_UNTRUSTED",
        "TRUST_BOUNDARY_INVALID",
        "TRUST_REPO_MISMATCH",
        "TRUST_WORKSTREAM_MISMATCH",
        "TRUST_REVISION_MISMATCH",
        "TRUST_SUBJECT_HEAD_MISMATCH",
        "TRUST_RUNTIME_SUBJECT_MISMATCH",
        "TRUST_EVIDENCE_STALE_HEAD",
        "TRUST_EVIDENCE_STALE_REVISION",
        "TRUST_EVIDENCE_DUPLICATE",
        "TRUST_EVIDENCE_UNKNOWN",
        "TRUST_EVIDENCE_MISSING",
        "TRUST_EVIDENCE_NOT_PASS",
    }
)
_ALLOWED_BLOCKERS = frozenset(
    {code for _field, code in _REQUIREMENT_BLOCKERS}
) | _TRUST_BLOCKERS
_BOUNDARY_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "producer_repo",
        "producer_head",
        "report_digest",
    }
)


class TrustedLearnedAdmissionBoundary:
    """Opaque in-process coordinator precondition for CANARY_READY promotion."""

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, Any]) -> None:
        if type(payload) is not dict:
            raise ValidationError(
                "trusted learned-canary boundary payload must be a dict"
            )
        self._payload = deepcopy(payload)

    def payload(self) -> dict[str, Any]:
        return deepcopy(self._payload)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("context learned input contains a duplicate JSON key")
        result[key] = value
    return result


def _candidate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _CANDIDATE_KEYS:
        _reject("context learned candidate schema is invalid")
    candidate_id = value.get("candidate_id")
    source_repo = value.get("source_repo")
    source_commit = value.get("source_commit")
    package_version = value.get("package_version")
    license_name = value.get("license")
    integration_mode = value.get("integration_mode")
    checks = (
        (candidate_id, _CANDIDATE_ID_RE, "candidate_id"),
        (source_repo, _SOURCE_REPO_RE, "source_repo"),
        (source_commit, _SOURCE_COMMIT_RE, "source_commit"),
        (package_version, _PACKAGE_VERSION_RE, "package_version"),
        (license_name, _LICENSE_RE, "license"),
    )
    for raw, pattern, label in checks:
        if (
            not isinstance(raw, str)
            or pattern.fullmatch(raw) is None
            or _SECRET_RE.search(raw) is not None
        ):
            _reject(f"context learned {label} is invalid")
    if integration_mode != "LOCAL_SELF_HOST":
        _reject("context learned integration_mode is unsupported")
    return {
        "candidate_id": candidate_id,
        "source_repo": source_repo,
        "source_commit": source_commit,
        "package_version": package_version,
        "license": license_name,
        "integration_mode": integration_mode,
    }


def _requirements(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != set(_REQUIREMENT_KEYS):
        _reject("context learned requirements schema is invalid")
    normalized: dict[str, bool] = {}
    for key in _REQUIREMENT_KEYS:
        item = value.get(key)
        if type(item) is not bool:
            _reject(f"context learned requirement {key} must be boolean")
        normalized[key] = item
    return normalized

def normalize_learned_canary_report(payload: object) -> dict[str, Any]:
    """Validate one bounded final Engineering System learned-canary report."""
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _reject("context learned report schema is invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        _reject("context learned schema_version is unsupported")
    if payload.get("kind") != LEARNED_KIND:
        _reject("context learned report kind is invalid")

    decision = payload.get("decision")
    if decision not in {"SETUP_ALLOWED", "CANARY_READY"}:
        _reject("context learned decision is invalid")
    if payload.get("setup_allowed") is not True:
        _reject("context learned report is not setup-allowed")
    canary_ready = payload.get("canary_ready")
    if type(canary_ready) is not bool:
        _reject("context learned canary_ready must be boolean")

    candidate = _candidate(payload.get("candidate"))
    requirements = _requirements(payload.get("requirements"))
    blockers = payload.get("blockers")
    if not isinstance(blockers, list) or len(blockers) > len(_REQUIREMENT_KEYS):
        _reject("context learned blocker list is invalid")
    if any(
        not isinstance(item, str) or item not in _ALLOWED_BLOCKERS
        for item in blockers
    ):
        _reject("context learned blocker is unsupported")
    if len(blockers) != len(set(blockers)):
        _reject("context learned blockers must be unique")

    expected = [
        code
        for field, code in _REQUIREMENT_BLOCKERS
        if requirements[field] is False
    ]
    trust_blockers = [item for item in blockers if item in _TRUST_BLOCKERS]
    if requirements["trusted_runtime_evidence"]:
        if trust_blockers:
            _reject("context learned trust evidence contradicts blockers")
    elif len(trust_blockers) != 1:
        _reject("context learned trust blocker is inconsistent")
    expected.extend(trust_blockers)
    if blockers != expected:
        _reject("context learned blockers do not match requirements")

    all_ready = all(requirements.values()) and not blockers
    if decision == "CANARY_READY":
        if canary_ready is not True or not all_ready:
            _reject("context learned CANARY_READY evidence is inconsistent")
    elif canary_ready is not False or all_ready or not blockers:
        _reject("context learned SETUP_ALLOWED evidence is inconsistent")

    return {
        "schema_version": 1,
        "kind": LEARNED_KIND,
        "decision": decision,
        "setup_allowed": True,
        "canary_ready": canary_ready,
        "candidate": candidate,
        "requirements": requirements,
        "blockers": list(blockers),
    }


def _shadow_report_from_context(value: dict[str, Any]) -> dict[str, Any]:
    quality = value.get("quality_evidence")
    scope = value.get("scope")
    if not isinstance(quality, dict) or not isinstance(scope, dict):
        _reject("context optimization quality evidence is invalid")
    return {
        "schema_version": quality.get("source_schema_version"),
        "kind": "context-shadow-equivalence-report",
        "decision": quality.get("decision"),
        "control_arm_id": quality.get("control_arm_id"),
        "system_head": quality.get("system_head"),
        "repo": quality.get("repository"),
        "task_kind": quality.get("task_kind"),
        "run_set_digest": quality.get("run_set_digest"),
        "profile": scope.get("profile"),
        "case_count": quality.get("case_count"),
        "arm_count": quality.get("arm_count"),
        "observation_count": quality.get("observation_count"),
        "arms": quality.get("arms"),
    }


def _revalidate_context_input(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject("context optimization input schema is invalid")
    if "learned_canary_evidence" in value:
        _reject("context optimization input already has learned-canary evidence")

    if "quality_evidence" not in value:
        return _revalidate_base_context(value)

    candidate = deepcopy(value)
    candidate.pop("quality_evidence", None)
    gates = candidate.get("gates")
    if not isinstance(gates, dict):
        _reject("context optimization input gates are invalid")
    if gates.get("quality_noninferiority") != "SHADOW_ACTION_EQUIVALENT":
        _reject("context optimization quality gate is inconsistent")
    candidate["gates"] = deepcopy(gates)
    candidate["gates"]["quality_noninferiority"] = "UNKNOWN"

    base = _revalidate_base_context(candidate)
    rebound = bind_shadow_quality(base, _shadow_report_from_context(value))
    if rebound != value:
        _reject("context optimization shadow binding is not canonical")
    return rebound


def learned_canary_report_digest(report: object) -> str:
    """Return the canonical content-free digest of a normalized admission report."""
    normalized = normalize_learned_canary_report(report)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_trusted_boundary(
    boundary: object,
    report: dict[str, Any],
) -> dict[str, Any]:
    if type(boundary) is not TrustedLearnedAdmissionBoundary:
        _reject("trusted learned-canary boundary is required")
    payload = boundary.payload()
    if not isinstance(payload, dict) or set(payload) != _BOUNDARY_KEYS:
        _reject("trusted learned-canary boundary schema is invalid")
    if payload.get("schema_version") != 1:
        _reject("trusted learned-canary boundary schema_version is unsupported")
    if payload.get("kind") != TRUST_BOUNDARY_KIND:
        _reject("trusted learned-canary boundary kind is invalid")
    if payload.get("producer_repo") != ENGINEERING_SYSTEM_REPO:
        _reject("trusted learned-canary producer repository is invalid")
    if payload.get("producer_head") != ENGINEERING_SYSTEM_ADMISSION_HEAD:
        _reject("trusted learned-canary producer head is invalid")
    digest = payload.get("report_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        _reject("trusted learned-canary report digest is invalid")
    if digest != learned_canary_report_digest(report):
        _reject("trusted learned-canary report digest mismatch")
    return payload


def bind_learned_canary_admission(
    context_input: object,
    admission_report: object,
    boundary: object = None,
) -> dict[str, Any]:
    """Attach bounded local-canary evidence without granting active control."""
    base = _revalidate_context_input(context_input)
    report = normalize_learned_canary_report(admission_report)
    trusted_source: dict[str, Any] | None = None
    if report["canary_ready"] or boundary is not None:
        trusted_source = _validate_trusted_boundary(boundary, report)

    result = deepcopy(base)
    result["learned_canary_evidence"] = {
        "source_kind": LEARNED_SOURCE_KIND,
        "source_schema_version": 1,
        "decision": report["decision"],
        "setup_allowed": report["setup_allowed"],
        "canary_ready": report["canary_ready"],
        "candidate": deepcopy(report["candidate"]),
        "requirements": deepcopy(report["requirements"]),
        "blockers": list(report["blockers"]),
    }
    if trusted_source is not None:
        result["learned_canary_evidence"]["trusted_source"] = {
            "producer_repo": trusted_source["producer_repo"],
            "producer_head": trusted_source["producer_head"],
            "report_digest": trusted_source["report_digest"],
        }
    if report["canary_ready"]:
        result["gates"]["data_egress_eligibility"] = DATA_EGRESS_READY
        result["gates"]["runtime_capability"] = RUNTIME_READY
    return result


def _load_json(path: Path, *, label: str) -> object:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValidationError(f"{label} is not readable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        _reject(f"{label} exceeds the bounded input size")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                f"{label} contains a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} is not valid UTF-8 JSON") from exc


def load_learned_canary_binding(
    context_path: Path,
    admission_path: Path,
) -> dict[str, Any]:
    return bind_learned_canary_admission(
        _load_json(context_path, label="context optimization input"),
        _load_json(admission_path, label="context learned admission report"),
    )
