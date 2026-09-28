"""Strict Atlas binding for Engineering System shadow action-equivalence evidence."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from atlas.context_optimization import (
    MAX_INPUT_BYTES,
    MAX_RECORDS,
    normalize_context_canary_report,
)
from atlas.provenance import ValidationError
from atlas.work_controller import normalize_github_repository

SHADOW_KIND = "context-shadow-equivalence-report"
QUALITY_STATE = "SHADOW_ACTION_EQUIVALENT"
QUALITY_SOURCE_KIND = "engineering_system_context_shadow_v2"
MAX_ACTIONS_PER_RUN = 16

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ARM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.:@\[\]=,+-]{1,80}$")
_SECRET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)

_SHADOW_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "decision",
        "control_arm_id",
        "system_head",
        "repo",
        "task_kind",
        "run_set_digest",
        "profile",
        "case_count",
        "arm_count",
        "observation_count",
        "arms",
    }
)
_SHADOW_ARM_KEYS = frozenset(
    {"arm_id", "run_count", "material_action_count"}
)
_CONTEXT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "scope",
        "source_evidence",
        "gates",
        "control_mode",
        "arms",
    }
)
_PROFILE_KEYS = frozenset({"provider", "model", "reasoning", "toolset"})
_TASK_KINDS = frozenset(
    {
        "DESIGN",
        "DEVELOPMENT",
        "TEST",
        "REVIEW",
        "RELEASE",
        "OPERATIONS",
        "ADOPTION",
        "DOCUMENTATION",
        "CLEANUP",
        "MIXED",
    }
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("context shadow input contains a duplicate JSON key")
        result[key] = value
    return result


def _bounded_int(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int = MAX_RECORDS,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _reject(f"{label} is invalid")
    return value


def _profile(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _PROFILE_KEYS:
        _reject("context shadow profile schema is invalid")
    result: dict[str, str] = {}
    for key in ("provider", "model", "reasoning", "toolset"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or _PROFILE_RE.fullmatch(item) is None
            or _SECRET_RE.search(item) is not None
        ):
            _reject(f"context shadow profile {key} is invalid")
        result[key] = item
    return result


def _arm(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SHADOW_ARM_KEYS:
        _reject("context shadow arm schema is invalid")
    arm_id = value.get("arm_id")
    if (
        not isinstance(arm_id, str)
        or _ARM_RE.fullmatch(arm_id) is None
        or _SECRET_RE.search(arm_id) is not None
    ):
        _reject("context shadow arm_id is invalid")
    run_count = _bounded_int(
        value.get("run_count"),
        label="context shadow run_count",
        minimum=1,
    )
    action_count = _bounded_int(
        value.get("material_action_count"),
        label="context shadow material_action_count",
        minimum=run_count,
        maximum=MAX_ACTIONS_PER_RUN * run_count,
    )
    return {
        "arm_id": arm_id,
        "run_count": run_count,
        "material_action_count": action_count,
    }


def normalize_shadow_report(payload: object) -> dict[str, Any]:
    """Validate one content-free Engineering System shadow report."""
    if not isinstance(payload, dict) or set(payload) != _SHADOW_KEYS:
        _reject("context shadow report schema is invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 2:
        _reject("context shadow schema_version is unsupported")
    if payload.get("kind") != SHADOW_KIND:
        _reject("context shadow report kind is invalid")
    if payload.get("decision") != "EQUIVALENT":
        _reject("context shadow decision is not EQUIVALENT")

    control_arm = payload.get("control_arm_id")
    if (
        not isinstance(control_arm, str)
        or _ARM_RE.fullmatch(control_arm) is None
        or _SECRET_RE.search(control_arm) is not None
    ):
        _reject("context shadow control_arm_id is invalid")

    system_head = payload.get("system_head")
    if not isinstance(system_head, str) or _SHA_RE.fullmatch(system_head) is None:
        _reject("context shadow system_head is invalid")

    repo_raw = payload.get("repo")
    if not isinstance(repo_raw, str):
        _reject("context shadow repo is invalid")
    repo = normalize_github_repository(repo_raw)
    if repo != repo_raw or _SECRET_RE.search(repo):
        _reject("context shadow repo must be canonical and credential-free")

    task_kind = payload.get("task_kind")
    if not isinstance(task_kind, str) or task_kind not in _TASK_KINDS:
        _reject("context shadow task_kind is invalid")

    run_set_digest = payload.get("run_set_digest")
    if (
        not isinstance(run_set_digest, str)
        or _DIGEST_RE.fullmatch(run_set_digest) is None
    ):
        _reject("context shadow run_set_digest is invalid")

    profile = _profile(payload.get("profile"))
    case_count = _bounded_int(
        payload.get("case_count"),
        label="context shadow case_count",
        minimum=1,
    )
    arm_count = _bounded_int(
        payload.get("arm_count"),
        label="context shadow arm_count",
        minimum=2,
    )
    observation_count = _bounded_int(
        payload.get("observation_count"),
        label="context shadow observation_count",
        minimum=2,
    )

    raw_arms = payload.get("arms")
    if (
        not isinstance(raw_arms, list)
        or len(raw_arms) < 2
        or len(raw_arms) > MAX_RECORDS
    ):
        _reject("context shadow arms must be a bounded multi-arm list")
    if arm_count != len(raw_arms):
        _reject("context shadow arm_count mismatch")

    arms = [_arm(item) for item in raw_arms]
    arms.sort(key=lambda item: item["arm_id"])
    arm_ids = [item["arm_id"] for item in arms]
    if len(arm_ids) != len(set(arm_ids)):
        _reject("context shadow arm_id values must be unique")
    if control_arm not in set(arm_ids):
        _reject("context shadow control arm is not present")

    if any(item["run_count"] != case_count for item in arms):
        _reject("context shadow arm run_count does not match case_count")
    if observation_count != arm_count * case_count:
        _reject("context shadow observation_count mismatch")
    control_action_count = next(
        item["material_action_count"]
        for item in arms
        if item["arm_id"] == control_arm
    )
    if any(
        item["material_action_count"] != control_action_count
        for item in arms
    ):
        _reject("context shadow material action counts contradict equivalence")

    return {
        "schema_version": 2,
        "kind": SHADOW_KIND,
        "decision": "EQUIVALENT",
        "control_arm_id": control_arm,
        "system_head": system_head,
        "repo": repo,
        "task_kind": task_kind,
        "run_set_digest": run_set_digest,
        "profile": profile,
        "case_count": case_count,
        "arm_count": arm_count,
        "observation_count": observation_count,
        "arms": arms,
    }


def _revalidate_base_context(value: object) -> dict[str, Any]:
    """Reconstruct the #117 source report and require exact normalized equality."""
    if not isinstance(value, dict) or set(value) != _CONTEXT_KEYS:
        _reject("context optimization input schema is invalid")
    scope = value.get("scope")
    evidence = value.get("source_evidence")
    gates = value.get("gates")
    arms = value.get("arms")
    if not isinstance(scope, dict) or not isinstance(evidence, dict):
        _reject("context optimization input source binding is invalid")
    if not isinstance(gates, dict) or not isinstance(arms, list):
        _reject("context optimization input state is invalid")
    if value.get("control_mode") != "OBSERVE_ONLY":
        _reject("context optimization input must remain OBSERVE_ONLY")
    if gates != {
        "quality_noninferiority": "UNKNOWN",
        "data_egress_eligibility": "UNKNOWN",
        "runtime_capability": "UNKNOWN",
        "active_control": "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
    }:
        _reject("context optimization input gates are not a #117 base input")
    source_version = evidence.get("source_schema_version")
    expected_source_kind = {
        1: "engineering_system_context_canary_v1",
        2: "engineering_system_context_canary_v2",
    }.get(source_version)
    if evidence.get("source_kind") != expected_source_kind:
        _reject("context optimization input source kind is invalid")
    if evidence.get("comparability") != "ELIGIBLE":
        _reject("context optimization input is not comparable")

    source = {
        "schema_version": source_version,
        "kind": "context-canary-eligibility-report",
        "decision": "ELIGIBLE",
        "system_head": evidence.get("system_head"),
        "profile": scope.get("profile"),
        "repo": scope.get("repository"),
        "task_kind": scope.get("task_kind"),
        "record_count": evidence.get("record_count"),
        "arm_count": evidence.get("arm_count"),
        "arms": arms,
    }
    if source_version == 2:
        source["run_set_digest"] = evidence.get("run_set_digest")
    normalized = normalize_context_canary_report(source)
    if normalized != value:
        _reject("context optimization input is not canonical")
    return normalized


def bind_shadow_quality(
    context_input: object,
    shadow_report: object,
) -> dict[str, Any]:
    """Bind exact P0.75-B facts to a canonical #117 input without control authority."""
    base = _revalidate_base_context(context_input)
    shadow = normalize_shadow_report(shadow_report)

    evidence = base["source_evidence"]
    scope = base["scope"]
    if evidence.get("source_schema_version") != 2:
        _reject("context optimization input lacks exact run-set binding")
    if shadow["system_head"] != evidence["system_head"]:
        _reject("context shadow system_head does not match context input")
    if shadow["repo"] != scope["repository"]:
        _reject("context shadow repository does not match context input")
    if shadow["task_kind"] != scope["task_kind"]:
        _reject("context shadow task_kind does not match context input")
    if shadow["run_set_digest"] != evidence.get("run_set_digest"):
        _reject("context shadow run_set_digest does not match context input")
    if shadow["profile"] != scope["profile"]:
        _reject("context shadow profile does not match context input")
    if shadow["arm_count"] != evidence["arm_count"]:
        _reject("context shadow arm_count does not match context input")
    if shadow["observation_count"] != evidence["record_count"]:
        _reject("context shadow observation_count does not match context input")

    base_runs = {
        item["arm_id"]: item["run_count"]
        for item in base["arms"]
    }
    shadow_runs = {
        item["arm_id"]: item["run_count"]
        for item in shadow["arms"]
    }
    if shadow_runs != base_runs:
        _reject("context shadow arm identities or run counts do not match context input")
    if shadow["control_arm_id"] not in base_runs:
        _reject("context shadow control arm does not match context input")

    result = deepcopy(base)
    result["gates"]["quality_noninferiority"] = QUALITY_STATE
    result["quality_evidence"] = {
        "source_kind": QUALITY_SOURCE_KIND,
        "source_schema_version": 2,
        "decision": "EQUIVALENT",
        "control_arm_id": shadow["control_arm_id"],
        "system_head": shadow["system_head"],
        "repository": shadow["repo"],
        "task_kind": shadow["task_kind"],
        "run_set_digest": shadow["run_set_digest"],
        "case_count": shadow["case_count"],
        "arm_count": shadow["arm_count"],
        "observation_count": shadow["observation_count"],
        "arms": deepcopy(shadow["arms"]),
    }
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


def load_shadow_quality_binding(
    context_path: Path,
    shadow_path: Path,
) -> dict[str, Any]:
    return bind_shadow_quality(
        _load_json(context_path, label="context optimization input"),
        _load_json(shadow_path, label="context shadow report"),
    )
