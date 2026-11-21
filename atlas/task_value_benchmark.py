"""Privacy-bounded, offline Atlas vs canonical-context productivity comparison.

This is an evaluative data plane, not a proof of completed engineering work.
All observations and reviews are operator-declared and unauthenticated here.
No code execution, GitHub/API queries, policy mutation or network requests.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from statistics import median
from typing import Any
from urllib.parse import urlsplit

MIN_PAIRS = 20
MAX_PAIRS = 100
MAX_FILE_BYTES = 1024 * 1024
ALLOWED_CONDITIONS = frozenset({"CONTROL", "ATLAS"})
ALLOWED_WORK_CLASSES = frozenset({"BUGFIX", "FEATURE", "INVESTIGATION", "DOCS"})
ALLOWED_OUTCOMES = frozenset({"PASS", "FAIL", "UNKNOWN"})
ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
REPO = re.compile(r"^datarelay-labs/[a-z0-9][a-z0-9._-]{1,99}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
REVIEW_URL = re.compile(
    r"^https://github\.com/datarelay-labs/[a-z0-9][a-z0-9._-]{1,99}/"
    r"(?:pull|issues|actions/runs|commit)/[0-9a-z]+(?:/[0-9a-z._/-]+)?$"
)


class TaskValueError(ValueError):
    """Untrusted input validation error without emitting raw source fields."""


def _timestamp(value: Any) -> None:
    if not isinstance(value, str) or not UTC.fullmatch(value):
        raise TaskValueError("UTC timestamp invalid")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TaskValueError("UTC timestamp invalid") from exc


def _integer(value: Any, *, bound: int) -> bool:
    return type(value) is int and 0 <= value <= bound


def _digest(value: Any) -> str:
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


def validate_record(value: Any) -> dict[str, Any]:
    fields = {
        "pair_id", "condition", "repository", "start_head", "work_class",
        "task_completed", "verified_outcome", "review", "preparation_seconds",
        "context_bytes", "reread_count", "owner_reexplanations",
        "stale_reference_count", "first_pass_defects", "used_atlas_reference",
        "public_metadata_only",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise TaskValueError("task measurement fields invalid")
    if (
        not isinstance(value["pair_id"], str)
        or ID.fullmatch(value["pair_id"]) is None
        or not isinstance(value["condition"], str)
        or value["condition"] not in ALLOWED_CONDITIONS
        or not isinstance(value["repository"], str)
        or REPO.fullmatch(value["repository"]) is None
        or not isinstance(value["start_head"], str)
        or SHA40.fullmatch(value["start_head"]) is None
        or not isinstance(value["work_class"], str)
        or value["work_class"] not in ALLOWED_WORK_CLASSES
        or type(value["task_completed"]) is not bool
        or not isinstance(value["verified_outcome"], str)
        or value["verified_outcome"] not in ALLOWED_OUTCOMES
        or type(value["used_atlas_reference"]) is not bool
        or value["public_metadata_only"] is not True
    ):
        raise TaskValueError("task identity, class, status or privacy invalid")
    for field, bound in {
        "preparation_seconds": 86400,
        "context_bytes": 10_000_000,
        "reread_count": 1000,
        "owner_reexplanations": 1000,
        "stale_reference_count": 1000,
        "first_pass_defects": 1000,
    }.items():
        if not _integer(value[field], bound=bound):
            raise TaskValueError(f"{field} is not bounded measured metadata")

    if value["condition"] == "CONTROL" and value["used_atlas_reference"]:
        raise TaskValueError("control condition cannot consume Atlas context")
    if value["verified_outcome"] == "PASS" and not value["task_completed"]:
        raise TaskValueError("incomplete task cannot be reported as PASS")
    review = value["review"]
    if review is not None:
        if not isinstance(review, dict) or set(review) != {
            "reviewer_id", "verified_at", "evidence_url"
        }:
            raise TaskValueError("independent review declaration invalid")
        if (
            not isinstance(review["reviewer_id"], str)
            or ID.fullmatch(review["reviewer_id"]) is None
        ):
            raise TaskValueError("independent review identity invalid")
        _timestamp(review["verified_at"])
        url = review["evidence_url"]
        if not isinstance(url, str) or not REVIEW_URL.fullmatch(url):
            raise TaskValueError("independent GitHub evidence URL invalid")
        split = urlsplit(url)
        if split.netloc != "github.com" or split.query or split.fragment:
            raise TaskValueError("independent GitHub evidence URL invalid")
        repo = "/".join(split.path.split("/")[1:3])
        if repo != value["repository"]:
            raise TaskValueError("review evidence repository mismatch")
        if value["verified_outcome"] == "UNKNOWN":
            raise TaskValueError("independent review cannot verify UNKNOWN outcome")
    return value


def evaluate(study: Any) -> dict[str, Any]:
    if not isinstance(study, dict) or set(study) != {
        "schema_version", "study_id", "scope", "observations"
    }:
        raise TaskValueError("study schema invalid")
    if study["schema_version"] != 1 or study["scope"] != "METADATA_ONLY":
        raise TaskValueError("study privacy scope or schema invalid")
    if not isinstance(study["study_id"], str) or ID.fullmatch(study["study_id"]) is None:
        raise TaskValueError("study identifier invalid")
    observations = study["observations"]
    if not isinstance(observations, list) or len(observations) > 2 * MAX_PAIRS:
        raise TaskValueError("study observation count exceeds bounds")
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for item in observations:
        record = validate_record(item)
        pair = pairs.setdefault(record["pair_id"], {})
        condition = record["condition"]
        if condition in pair:
            raise TaskValueError("duplicate task condition in study pair")
        pair[condition] = record
    if len(pairs) > MAX_PAIRS:
        raise TaskValueError("too many study pairs")
    for pair in pairs.values():
        if len(pair) != 2:
            continue
        left, right = pair["CONTROL"], pair["ATLAS"]
        for key in ("repository", "start_head", "work_class"):
            if left[key] != right[key]:
                raise TaskValueError("comparison pair identity mismatch")

    enough = len(pairs) >= MIN_PAIRS
    paired = bool(pairs) and all(len(pair) == 2 for pair in pairs.values())
    complete_review = paired and all(
        v["review"] is not None
        for pair in pairs.values() for v in pair.values()
    )
    eligible = enough and paired and complete_review
    records = {
        side: [pair[side] for pair in pairs.values() if side in pair]
        for side in ("CONTROL", "ATLAS")
    }
    metrics = {}
    for side, subset in records.items():
        metrics[side] = {
            "observations": len(subset),
            "declared_review_count": sum(x["review"] is not None for x in subset),
            "reported_pass_count": sum(x["verified_outcome"] == "PASS" for x in subset),
            "atlas_reference_used_count": sum(x["used_atlas_reference"] for x in subset),
            "owner_reexplanations": sum(x["owner_reexplanations"] for x in subset),
            "rereads": sum(x["reread_count"] for x in subset),
            "stale_references": sum(x["stale_reference_count"] for x in subset),
            "first_pass_defects": sum(x["first_pass_defects"] for x in subset),
            "median_preparation_seconds": (
                median(x["preparation_seconds"] for x in subset) if eligible else None
            ),
            "median_context_bytes": (
                median(x["context_bytes"] for x in subset) if eligible else None
            ),
            "declared_success_rate": (
                round(sum(x["verified_outcome"] == "PASS" for x in subset) / len(subset), 4)
                if eligible else None
            ),
        }

    def reduction(name: str) -> float | None:
        if not eligible:
            return None
        control, atlas = (metrics[side][name] for side in ("CONTROL", "ATLAS"))
        return round(100 * (control - atlas) / control, 2) if control > 0 else None

    preparation_reduction = reduction("median_preparation_seconds")
    context_reduction = reduction("median_context_bytes")
    quality_safe = eligible and all((
        metrics["ATLAS"]["reported_pass_count"] >= metrics["CONTROL"]["reported_pass_count"],
        metrics["ATLAS"]["stale_references"] <= metrics["CONTROL"]["stale_references"],
        metrics["ATLAS"]["first_pass_defects"] <= metrics["CONTROL"]["first_pass_defects"],
    ))
    candidate = (
        bool(quality_safe)
        and metrics["ATLAS"]["atlas_reference_used_count"] > 0
        and (
            preparation_reduction is not None and preparation_reduction >= 25
            or context_reduction is not None and context_reduction >= 20
        )
    )
    return {
        "kind": "atlas_task_value_comparison",
        "schema_version": 1,
        "authority": "EVALUATION_ONLY",
        "study_id": study["study_id"],
        "study_sha256": _digest(study),
        "observed_pairs": len(pairs),
        "minimum_pairs_per_condition": MIN_PAIRS,
        "expected_conditions": ["CONTROL", "ATLAS"],
        "observations": len(observations),
        "state": (
            "NO_DATA" if not observations else
            "INSUFFICIENT" if not enough else
            "INCOMPLETE" if not paired or not complete_review else
            "COMPLETE_DECLARED_NOT_ATTESTED"
        ),
        "preparation_reduction_percent": preparation_reduction,
        "context_reduction_percent": context_reduction,
        "declared_quality_noninferiority": quality_safe if eligible else None,
        "declared_candidate_threshold_met": candidate if eligible else None,
        "independent_review_attestation": "NOT_ATTESTED",
        "productivity_benefit": "NOT_ESTABLISHED",
        "policy_mutated": False,
        "metrics": metrics,
        "note": (
            "Matching and reviewer declarations are not authenticated. "
            "Real task assignment, timing, exact HEAD, GitHub/CI outcomes and "
            "independent audits must be externally verified before value claims."
        ),
    }


def load_study(path: str) -> Any:
    with open(path, "rb") as handle:
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise TaskValueError("study file too large")
    try:
        def no_duplicate_keys(pairs):
            result = {}
            for k, v in pairs:
                if k in result:
                    raise TaskValueError("duplicate JSON key")
                result[k] = v
            return result
        return json.loads(content.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (ValueError, UnicodeError) as exc:
        raise TaskValueError("study JSON invalid") from exc
