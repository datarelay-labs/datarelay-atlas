"""Provider-neutral capability descriptors and bounded result normalization.

Capability presence is descriptive evidence only. It never grants execution,
permission, trust, budget, routing, or mutation authority.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from atlas.provenance import ValidationError
from atlas.work_controller import AUDIT_VERDICTS, AuditResult

SCHEMA_VERSION = 1
DESCRIPTOR_KIND = "provider_capability_descriptor"
RESULT_KIND = "provider_capability_result"
AUTHORITY = "EVIDENCE_ONLY"
CODE_REVIEW_RESULT_CONTRACT = "audit_result_v1"

CAPABILITY_NAMES = frozenset(
    {
        "PLAN",
        "EFFORT_CONTROL",
        "COMPLETION_LOOP",
        "RUNTIME_VERIFY",
        "CODE_REVIEW",
        "SECOND_MODEL_ADVISOR",
        "PARALLEL_WORKERS",
        "PERIODIC_REENTRY",
        "PR_REWORK",
        "CONTEXT_USAGE",
        "CHECKPOINT_COMPACT",
        "SESSION_RESUME_RESET",
        "HARNESS_HEALTH_AUDIT",
        "PROMPT_INSTRUCTION_AUDIT",
        "SKILL_COST_AUDIT",
        "USAGE_INSIGHTS",
    }
)
CAPABILITY_STATES = frozenset({"SUPPORTED", "UNSUPPORTED", "UNKNOWN"})
_RESULT_CONTRACTS = frozenset({CODE_REVIEW_RESULT_CONTRACT})

_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_EVIDENCE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SECRET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)

_DESCRIPTOR_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "provider",
        "runtime",
        "usage_mode",
        "adapter",
        "capabilities",
        "authority",
    }
)
_CAPABILITY_KEYS = frozenset({"name", "status", "result_contract"})
_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "capability",
        "provider",
        "runtime",
        "usage_mode",
        "adapter",
        "result_contract",
        "outcome",
        "evidence",
        "authority",
    }
)
_EVIDENCE_KEYS = frozenset({"subject_head", "evidence_ref"})


def _reject(message: str) -> None:
    raise ValidationError(message)


def _identity(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER_RE if provider else _IDENTITY_RE
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or _SECRET_RE.search(value) is not None
    ):
        _reject(f"{label} is invalid")
    return value


def _capability_entry(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _CAPABILITY_KEYS:
        _reject("provider capability entry schema is invalid")
    name = raw.get("name")
    status = raw.get("status")
    result_contract = raw.get("result_contract")
    if name not in CAPABILITY_NAMES:
        _reject("provider capability name is unsupported")
    if status not in CAPABILITY_STATES:
        _reject("provider capability status is invalid")

    if status == "SUPPORTED":
        if result_contract is not None:
            if (
                not isinstance(result_contract, str)
                or result_contract not in _RESULT_CONTRACTS
            ):
                _reject("provider capability result contract is unsupported")
        if name == "CODE_REVIEW" and result_contract != CODE_REVIEW_RESULT_CONTRACT:
            _reject("CODE_REVIEW requires audit_result_v1")
        if name != "CODE_REVIEW" and result_contract is not None:
            _reject("result contract is not defined for this capability")
    elif result_contract is not None:
        _reject("unsupported or unknown capability cannot claim a result contract")

    return {
        "name": name,
        "status": status,
        "result_contract": result_contract,
    }


def validate_provider_capability_descriptor(payload: object) -> dict[str, Any]:
    """Validate and deterministically normalize one provider capability descriptor."""
    if not isinstance(payload, dict) or set(payload) != _DESCRIPTOR_KEYS:
        _reject("provider capability descriptor schema is invalid")
    version = payload.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SCHEMA_VERSION
    ):
        _reject("provider capability descriptor schema_version is unsupported")
    if payload.get("kind") != DESCRIPTOR_KIND:
        _reject("provider capability descriptor kind is invalid")
    if payload.get("authority") != AUTHORITY:
        _reject("provider capability descriptor cannot grant authority")

    provider = _identity(payload.get("provider"), label="provider", provider=True)
    runtime = _identity(payload.get("runtime"), label="runtime")
    usage_mode = _identity(payload.get("usage_mode"), label="usage_mode")
    adapter = _identity(payload.get("adapter"), label="adapter")

    raw_capabilities = payload.get("capabilities")
    if (
        not isinstance(raw_capabilities, list)
        or not raw_capabilities
        or len(raw_capabilities) > len(CAPABILITY_NAMES)
    ):
        _reject("provider capability list is invalid")
    capabilities = [_capability_entry(item) for item in raw_capabilities]
    names = [item["name"] for item in capabilities]
    if len(names) != len(set(names)):
        _reject("provider capability names must be unique")
    capabilities.sort(key=lambda item: item["name"])

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DESCRIPTOR_KIND,
        "provider": provider,
        "runtime": runtime,
        "usage_mode": usage_mode,
        "adapter": adapter,
        "capabilities": capabilities,
        "authority": AUTHORITY,
    }


def _descriptor(
    *,
    provider: str,
    runtime: str,
    usage_mode: str,
    adapter: str,
) -> dict[str, Any]:
    return validate_provider_capability_descriptor(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": DESCRIPTOR_KIND,
            "provider": provider,
            "runtime": runtime,
            "usage_mode": usage_mode,
            "adapter": adapter,
            "capabilities": [
                {
                    "name": "CODE_REVIEW",
                    "status": "SUPPORTED",
                    "result_contract": CODE_REVIEW_RESULT_CONTRACT,
                }
            ],
            "authority": AUTHORITY,
        }
    )


def codex_cli_capability_descriptor() -> dict[str, Any]:
    """Describe the existing CodexAuditProvider without invoking Codex."""
    return _descriptor(
        provider="codex",
        runtime="codex_cli",
        usage_mode="chatgpt_plan",
        adapter="CodexAuditProvider",
    )


def openai_responses_capability_descriptor() -> dict[str, Any]:
    """Describe the existing BoundedResponsesAuditProvider without API access."""
    return _descriptor(
        provider="openai",
        runtime="responses_api",
        usage_mode="api",
        adapter="BoundedResponsesAuditProvider",
    )


def generic_audit_capability_descriptor() -> dict[str, Any]:
    """Describe the provider-neutral AuditPort fallback contract."""
    return _descriptor(
        provider="generic",
        runtime="audit_port",
        usage_mode="caller_supplied",
        adapter="AuditPort",
    )


def descriptor_for_adapter(adapter_name: str) -> dict[str, Any]:
    """Return a known descriptor by existing Atlas adapter identity."""
    if adapter_name == "CodexAuditProvider":
        return codex_cli_capability_descriptor()
    if adapter_name == "BoundedResponsesAuditProvider":
        return openai_responses_capability_descriptor()
    if adapter_name in {"AuditPort", "FixedAuditAdapter"}:
        return generic_audit_capability_descriptor()
    _reject("provider adapter is not registered")


def _code_review_entry(descriptor: dict[str, Any]) -> dict[str, Any]:
    for item in descriptor["capabilities"]:
        if item["name"] == "CODE_REVIEW":
            return item
    _reject("provider descriptor does not declare CODE_REVIEW")


def normalize_code_review_result(
    descriptor: object,
    result: AuditResult,
    *,
    subject_head: str,
    evidence_ref: str,
) -> dict[str, Any]:
    """Normalize an existing AuditResult without retaining provider findings."""
    normalized_descriptor = validate_provider_capability_descriptor(descriptor)
    capability = _code_review_entry(normalized_descriptor)
    if capability["status"] != "SUPPORTED":
        _reject("provider CODE_REVIEW capability is not supported")
    if capability["result_contract"] != CODE_REVIEW_RESULT_CONTRACT:
        _reject("provider CODE_REVIEW result contract is incompatible")
    if not isinstance(result, AuditResult):
        _reject("CODE_REVIEW result must be an AuditResult")
    if result.verdict not in AUDIT_VERDICTS:
        _reject("CODE_REVIEW verdict is invalid")

    if not isinstance(subject_head, str) or _SHA_RE.fullmatch(subject_head) is None:
        _reject("CODE_REVIEW subject_head must be an exact 40-hex SHA")
    if (
        not isinstance(evidence_ref, str)
        or _EVIDENCE_REF_RE.fullmatch(evidence_ref) is None
        or _SECRET_RE.search(evidence_ref) is not None
    ):
        _reject("CODE_REVIEW evidence_ref is invalid")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "capability": "CODE_REVIEW",
        "provider": normalized_descriptor["provider"],
        "runtime": normalized_descriptor["runtime"],
        "usage_mode": normalized_descriptor["usage_mode"],
        "adapter": normalized_descriptor["adapter"],
        "result_contract": CODE_REVIEW_RESULT_CONTRACT,
        "outcome": result.verdict,
        "evidence": {
            "subject_head": subject_head,
            "evidence_ref": evidence_ref,
        },
        "authority": AUTHORITY,
    }


def validate_provider_capability_result(payload: object) -> dict[str, Any]:
    """Validate the bounded normalized result envelope."""
    if not isinstance(payload, dict) or set(payload) != _RESULT_KEYS:
        _reject("provider capability result schema is invalid")
    version = payload.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SCHEMA_VERSION
    ):
        _reject("provider capability result schema_version is unsupported")
    if payload.get("kind") != RESULT_KIND:
        _reject("provider capability result kind is invalid")
    if payload.get("capability") != "CODE_REVIEW":
        _reject("provider capability result capability is unsupported")
    if payload.get("result_contract") != CODE_REVIEW_RESULT_CONTRACT:
        _reject("provider capability result contract is unsupported")
    if payload.get("authority") != AUTHORITY:
        _reject("provider capability result cannot grant authority")
    if payload.get("outcome") not in AUDIT_VERDICTS:
        _reject("provider capability result outcome is invalid")

    provider = _identity(payload.get("provider"), label="provider", provider=True)
    runtime = _identity(payload.get("runtime"), label="runtime")
    usage_mode = _identity(payload.get("usage_mode"), label="usage_mode")
    adapter = _identity(payload.get("adapter"), label="adapter")

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        _reject("provider capability result evidence schema is invalid")
    subject_head = evidence.get("subject_head")
    evidence_ref = evidence.get("evidence_ref")
    if not isinstance(subject_head, str) or _SHA_RE.fullmatch(subject_head) is None:
        _reject("provider capability result subject_head is invalid")
    if (
        not isinstance(evidence_ref, str)
        or _EVIDENCE_REF_RE.fullmatch(evidence_ref) is None
        or _SECRET_RE.search(evidence_ref) is not None
    ):
        _reject("provider capability result evidence_ref is invalid")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "capability": "CODE_REVIEW",
        "provider": provider,
        "runtime": runtime,
        "usage_mode": usage_mode,
        "adapter": adapter,
        "result_contract": CODE_REVIEW_RESULT_CONTRACT,
        "outcome": payload["outcome"],
        "evidence": {
            "subject_head": subject_head,
            "evidence_ref": evidence_ref,
        },
        "authority": AUTHORITY,
    }


def strip_provider_attribution(result: object) -> dict[str, Any]:
    """Return only cross-provider semantics for equivalence assertions."""
    normalized = validate_provider_capability_result(result)
    comparable = deepcopy(normalized)
    for key in ("provider", "runtime", "usage_mode", "adapter"):
        comparable.pop(key)
    return comparable
