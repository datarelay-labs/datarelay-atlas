"""Strict Atlas intake for Engineering System context-canary evidence.

This boundary consumes only the bounded, factual output of Engineering System's
live canary comparability gate. It never ranks optimizer arms and never grants
authority for COMPRESS/CLEAR/YIELD/ROUTE actions.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from atlas.provenance import ValidationError
from atlas.work_controller import normalize_github_repository

SCHEMA_VERSION = 1
INPUT_KIND = "context-canary-eligibility-report"
OUTPUT_KIND = "context_optimization_input"
MAX_INPUT_BYTES = 1024 * 1024
MAX_RECORDS = 256
MAX_ARMS = MAX_RECORDS
MAX_COUNT = 1_000_000_000_000
MAX_CONTEXT_BYTES = 1_000_000_000_000
MAX_USAGE_PER_RUN = 100_000_000
MAX_EFFORT_PER_RUN = 1_000_000
MAX_REWORK_PER_RUN = 3 * MAX_EFFORT_PER_RUN
MAX_PROVIDER_COST_PER_RUN = Decimal("1000000")
MAX_USAGE_TOTAL = MAX_RECORDS * MAX_USAGE_PER_RUN
MAX_EFFORT_TOTAL = MAX_RECORDS * MAX_EFFORT_PER_RUN
MAX_REWORK_TOTAL = 3 * MAX_EFFORT_TOTAL
MAX_PROVIDER_COST_TOTAL = Decimal(MAX_RECORDS) * MAX_PROVIDER_COST_PER_RUN
MAX_COST_PER_SOLVED = MAX_PROVIDER_COST_PER_RUN
MAX_DECIMAL_TEXT = 64

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ARM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.:@\[\]=,+-]{1,80}$")
_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SECRET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)"
)
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
_PROFILE_KEYS = frozenset({"provider", "model", "reasoning", "toolset"})
_TOP_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "decision",
        "system_head",
        "profile",
        "repo",
        "task_kind",
        "record_count",
        "arm_count",
        "arms",
    }
)
_ARM_KEYS = frozenset(
    {
        "arm_id",
        "system_head",
        "run_count",
        "verified_solved_count",
        "original_context_bytes",
        "kept_context_bytes",
        "reduction_ratio",
        "provider_cost_total",
        "cost_per_verified_solved_task",
        "rework_total",
        "input_tokens_total",
        "output_tokens_total",
        "cache_read_tokens_total",
        "cache_write_tokens_total",
        "tool_turns_total",
        "retries_total",
        "rereads_total",
        "compactions_total",
        "pr_rework_total",
        "ci_rework_total",
        "review_rework_total",
        "human_interventions_total",
    }
)
_USAGE_TOTAL_KEYS = (
    "input_tokens_total",
    "output_tokens_total",
    "cache_read_tokens_total",
    "cache_write_tokens_total",
)
_EFFORT_TOTAL_KEYS = (
    "tool_turns_total",
    "retries_total",
    "rereads_total",
    "compactions_total",
    "pr_rework_total",
    "ci_rework_total",
    "review_rework_total",
    "human_interventions_total",
)
_COUNT_KEYS = _USAGE_TOTAL_KEYS + _EFFORT_TOTAL_KEYS
_USAGE_COUNT_KEYS = frozenset(
    {
        "input_tokens_total",
        "output_tokens_total",
        "cache_read_tokens_total",
        "cache_write_tokens_total",
    }
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("context canary input contains a duplicate JSON key")
        result[key] = value
    return result


def _nonnegative_int(
    value: object,
    *,
    label: str,
    maximum: int = MAX_COUNT,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        _reject(f"{label} must be a bounded non-negative integer")
    return value


def _decimal_text(
    value: object,
    *,
    label: str,
    maximum: Decimal,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_DECIMAL_TEXT
        or not _DECIMAL_RE.fullmatch(value)
    ):
        _reject(f"{label} must be a bounded non-negative decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(
            f"{label} must be a bounded non-negative decimal string"
        ) from exc
    if not parsed.is_finite() or parsed < 0 or parsed > maximum:
        _reject(f"{label} must be a bounded non-negative decimal string")
    normalized = parsed.normalize()
    canonical = format(normalized, "f")
    if canonical in {"", "-0"}:
        canonical = "0"
    if value != canonical:
        _reject(f"{label} must use canonical decimal formatting")
    return value


def _profile(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _PROFILE_KEYS:
        _reject("context canary profile schema is invalid")
    result: dict[str, str] = {}
    for key in ("provider", "model", "reasoning", "toolset"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or _PROFILE_RE.fullmatch(item) is None
            or _SECRET_RE.search(item)
        ):
            _reject(f"context canary profile {key} is invalid")
        result[key] = item
    return result


def _reduction_ratio(
    value: object,
    *,
    original: int,
    kept: int,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject("context canary reduction_ratio is invalid")
    ratio = float(value)
    if ratio < 0 or ratio > 1:
        _reject("context canary reduction_ratio is invalid")
    expected = 0.0 if original == 0 else round(1.0 - (kept / original), 6)
    if ratio != expected:
        _reject("context canary reduction_ratio does not match context bytes")
    return ratio


def _arm(value: object, *, system_head: str) -> dict:
    if not isinstance(value, dict) or set(value) != _ARM_KEYS:
        _reject("context canary arm schema is invalid")

    arm_id = value.get("arm_id")
    if (
        not isinstance(arm_id, str)
        or _ARM_RE.fullmatch(arm_id) is None
        or _SECRET_RE.search(arm_id) is not None
    ):
        _reject("context canary arm_id is invalid")
    if value.get("system_head") != system_head:
        _reject("context canary arm system_head mismatch")

    run_count = _nonnegative_int(
        value.get("run_count"),
        label="run_count",
        maximum=MAX_RECORDS,
    )
    if run_count < 1:
        _reject("context canary run_count must be positive")
    solved = _nonnegative_int(
        value.get("verified_solved_count"),
        label="verified_solved_count",
        maximum=MAX_RECORDS,
    )
    if solved != run_count:
        _reject("context canary verified outcome is incomplete")

    original = _nonnegative_int(
        value.get("original_context_bytes"),
        label="original_context_bytes",
        maximum=MAX_CONTEXT_BYTES,
    )
    kept = _nonnegative_int(
        value.get("kept_context_bytes"),
        label="kept_context_bytes",
        maximum=MAX_CONTEXT_BYTES,
    )
    if kept > original:
        _reject("context canary kept context exceeds original context")
    reduction = _reduction_ratio(
        value.get("reduction_ratio"),
        original=original,
        kept=kept,
    )

    provider_cost = _decimal_text(
        value.get("provider_cost_total"),
        label="provider_cost_total",
        maximum=MAX_PROVIDER_COST_PER_RUN * run_count,
    )
    cost_per_solved = _decimal_text(
        value.get("cost_per_verified_solved_task"),
        label="cost_per_verified_solved_task",
        maximum=MAX_COST_PER_SOLVED,
    )
    expected_cost_per = Decimal(provider_cost) / Decimal(solved)
    if Decimal(cost_per_solved) != expected_cost_per:
        _reject("context canary cost per solved task is inconsistent")

    counts = {
        key: _nonnegative_int(
            value.get(key),
            label=key,
            maximum=(
                MAX_USAGE_PER_RUN
                if key in _USAGE_COUNT_KEYS
                else MAX_EFFORT_PER_RUN
            )
            * run_count,
        )
        for key in _COUNT_KEYS
    }
    rework_total = _nonnegative_int(
        value.get("rework_total"),
        label="rework_total",
        maximum=MAX_REWORK_PER_RUN * run_count,
    )
    if rework_total != (
        counts["pr_rework_total"]
        + counts["ci_rework_total"]
        + counts["review_rework_total"]
    ):
        _reject("context canary rework_total is inconsistent")

    return {
        "arm_id": arm_id,
        "system_head": system_head,
        "run_count": run_count,
        "verified_solved_count": solved,
        "original_context_bytes": original,
        "kept_context_bytes": kept,
        "reduction_ratio": reduction,
        "provider_cost_total": provider_cost,
        "cost_per_verified_solved_task": cost_per_solved,
        "rework_total": rework_total,
        **counts,
    }


def normalize_context_canary_report(payload: object) -> dict:
    """Validate an ES comparability report and emit observation-only Atlas input."""
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _reject("context canary report schema is invalid")
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        _reject("context canary report schema_version is unsupported")
    if payload.get("kind") != INPUT_KIND:
        _reject("context canary report kind is invalid")
    if payload.get("decision") != "ELIGIBLE":
        _reject("context canary report decision is not ELIGIBLE")

    system_head = payload.get("system_head")
    if not isinstance(system_head, str) or _SHA_RE.fullmatch(system_head) is None:
        _reject("context canary system_head is invalid")

    repo_raw = payload.get("repo")
    if not isinstance(repo_raw, str):
        _reject("context canary repo is invalid")
    repo = normalize_github_repository(repo_raw)
    if repo != repo_raw or _SECRET_RE.search(repo):
        _reject("context canary repo must be canonical and credential-free")

    task_kind = payload.get("task_kind")
    if not isinstance(task_kind, str) or task_kind not in _TASK_KINDS:
        _reject("context canary task_kind is invalid")

    profile = _profile(payload.get("profile"))
    record_count = _nonnegative_int(
        payload.get("record_count"),
        label="record_count",
        maximum=MAX_RECORDS,
    )
    arm_count = _nonnegative_int(
        payload.get("arm_count"),
        label="arm_count",
        maximum=MAX_ARMS,
    )
    raw_arms = payload.get("arms")
    if (
        not isinstance(raw_arms, list)
        or len(raw_arms) < 2
        or len(raw_arms) > MAX_ARMS
    ):
        _reject("context canary arms must be a bounded multi-arm list")
    if arm_count != len(raw_arms):
        _reject("context canary arm_count mismatch")

    arms = [_arm(item, system_head=system_head) for item in raw_arms]
    arm_ids = [item["arm_id"] for item in arms]
    if len(arm_ids) != len(set(arm_ids)):
        _reject("context canary arm_id values must be unique")
    run_counts = {item["run_count"] for item in arms}
    if len(run_counts) != 1:
        _reject("context canary arm run_count values must match")
    runs_per_arm = next(iter(run_counts))
    if record_count != arm_count * runs_per_arm:
        _reject("context canary record_count mismatch")

    arms.sort(key=lambda item: item["arm_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": OUTPUT_KIND,
        "scope": {
            "repository": repo,
            "task_kind": task_kind,
            "profile": profile,
        },
        "source_evidence": {
            "source_kind": "engineering_system_context_canary_v1",
            "source_schema_version": 1,
            "system_head": system_head,
            "comparability": "ELIGIBLE",
            "record_count": record_count,
            "arm_count": arm_count,
        },
        "gates": {
            "quality_noninferiority": "UNKNOWN",
            "data_egress_eligibility": "UNKNOWN",
            "runtime_capability": "UNKNOWN",
            "active_control": "NOT_ELIGIBLE_FOR_ACTIVE_CONTROL",
        },
        "control_mode": "OBSERVE_ONLY",
        "arms": arms,
    }


def load_context_canary_report(path: Path) -> dict:
    """Read a bounded JSON report and normalize it at the Atlas trust boundary."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValidationError("context canary report is not readable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        _reject("context canary report exceeds the bounded input size")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(
                "context canary report contains a non-finite number"
            ),
        )
    except ValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("context canary report is not valid UTF-8 JSON") from exc
    return normalize_context_canary_report(payload)
