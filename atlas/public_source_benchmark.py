"""Offline public-source search comparison; no network or provider invocations.

All inputs are untrusted evidence. No caller-supplied model output can grant
GitHub/source trust, product PASS, deployment, provider execution or release.
The evaluator emits only bounded aggregate data, never prompts or raw URLs.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from statistics import median
from typing import Any, Iterable
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MIN_QUESTIONS = 30
MAX_QUESTIONS = 100
MAX_PROVIDERS = 4
MAX_SOURCE_SIZE = 1024 * 1024
CATEGORIES = frozenset({"security-advisory", "vendor-documentation", "api-changelog"})
STATUS = frozenset({"SUCCESS", "ERROR", "TIMEOUT", "RATE_LIMIT"})
VERDICT = frozenset({"AGREES", "CONTRADICTS", "OMITTED", "UNVERIFIED"})
IDENTITY = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
DATE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")
SECRET = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]+PRIVATE KEY-----|"
    r"(?:password|secret|access[_-]?token|api[_-]?key)\s*[:=]|"
    r"\b(?:ghp_|gho_|github_pat_|AKIA)[A-Za-z0-9_-]{8,})"
)


class BenchmarkError(ValueError):
    """Fail closed without echoing source inputs into diagnostics."""


def _utc(value: Any, name: str) -> str:
    if not isinstance(value, str) or DATE.fullmatch(value) is None:
        raise BenchmarkError(f"{name} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkError(f"{name} is not a valid UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise BenchmarkError(f"{name} must use UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _host(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 253 or not HOST.fullmatch(value):
        raise BenchmarkError("official hostname invalid")
    if value.lower() != value or ".." in value or "." not in value:
        raise BenchmarkError("official hostname must be a public DNS hostname")
    if any(part.startswith("-") or part.endswith("-") for part in value.split(".")):
        raise BenchmarkError("official hostname invalid")
    if value.endswith((".local", ".internal", ".localhost", ".test", ".invalid")):
        raise BenchmarkError("official hostname is not public")
    if any(part.isdecimal() for part in value.split(".")):
        raise BenchmarkError("official hostname may not be an IP address")
    return value


def _public_url(value: Any, *, allowed_hosts: set[str] | None = None) -> str:
    if not isinstance(value, str) or len(value) > 1500 or any(
        ord(ch) < 33 or ord(ch) > 126 for ch in value
    ):
        raise BenchmarkError("source URL must be a bounded public HTTPS URL")
    try:
        parsed = urlsplit(value)
        host = _host(parsed.hostname)
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise BenchmarkError("source URL invalid") from exc
    if (
        parsed.scheme != "https" or parsed.netloc != host or port is not None
        or parsed.username is not None or parsed.password is not None
        or not parsed.path.startswith("/") or parsed.query or parsed.fragment
        or "\\" in parsed.path or "%" in parsed.path
    ):
        raise BenchmarkError("source URL must be plain HTTPS with no credentials, query or fragment")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise BenchmarkError("primary source URL must match the declared official host")
    return value


def _finite_cost(raw: Any) -> Decimal:
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (int, float, str)):
        raise BenchmarkError("cost_usd must be a measured nonnegative amount")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise BenchmarkError("cost_usd invalid") from exc
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("1000"):
        raise BenchmarkError("cost_usd invalid")
    return value


def manifest_questions(manifest: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "dataset_id", "providers", "questions", "scope"
    }:
        raise BenchmarkError("manifest fields invalid")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["scope"] != "PUBLIC_ONLY":
        raise BenchmarkError("manifest schema or privacy scope invalid")
    if not isinstance(manifest["dataset_id"], str) or not IDENTITY.fullmatch(manifest["dataset_id"]):
        raise BenchmarkError("dataset identifier invalid")
    providers = manifest["providers"]
    if (
        not isinstance(providers, list) or not 2 <= len(providers) <= MAX_PROVIDERS
        or any(not isinstance(v, str) or IDENTITY.fullmatch(v) is None for v in providers)
        or len(providers) != len(set(providers))
    ):
        raise BenchmarkError("must compare two to four distinct providers")
    questions = manifest["questions"]
    if not isinstance(questions, list) or not MIN_QUESTIONS <= len(questions) <= MAX_QUESTIONS:
        raise BenchmarkError("30 to 100 public questions required")
    ids: dict[str, dict[str, Any]] = {}
    for q in questions:
        if not isinstance(q, dict) or set(q) != {
            "id", "category", "question", "official_hosts"
        }:
            raise BenchmarkError("question fields invalid")
        identifier = q["id"]
        if not isinstance(identifier, str) or IDENTITY.fullmatch(identifier) is None:
            raise BenchmarkError("question id invalid")
        if identifier in ids:
            raise BenchmarkError("duplicate question id")
        sentence = q["question"]
        if (
            not isinstance(sentence, str) or not 15 <= len(sentence) <= 240
            or "\n" in sentence or "\r" in sentence or SECRET.search(sentence)
        ):
            raise BenchmarkError("question is not a bounded public prompt")
        if not isinstance(q["category"], str) or q["category"] not in CATEGORIES:
            raise BenchmarkError("unsupported public question category")
        hosts = q["official_hosts"]
        if (
            not isinstance(hosts, list) or not 1 <= len(hosts) <= 3
            or any(not isinstance(h, str) for h in hosts)
        ):
            raise BenchmarkError("official source host list invalid")
        for h in hosts:
            _host(h)
        if len(set(hosts)) != len(hosts):
            raise BenchmarkError("duplicate official source host")
        ids[identifier] = q
    return ids


def _validate_observation(raw: Any, questions: dict[str, dict[str, Any]], providers: set[str]) -> dict:
    required = {
        "question_id", "provider", "status", "accessed_at", "latency_ms",
        "cost_usd", "result_url", "verdict", "review", "fallback_used",
        "public_only"
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise BenchmarkError("observation fields invalid")
    qid, provider = raw["question_id"], raw["provider"]
    if not isinstance(qid, str) or qid not in questions:
        raise BenchmarkError("unknown question identity")
    if not isinstance(provider, str) or provider not in providers:
        raise BenchmarkError("unknown provider identity")
    if raw["public_only"] is not True or not isinstance(raw["fallback_used"], bool):
        raise BenchmarkError("observation privacy or fallback state invalid")
    if (not isinstance(raw["status"], str) or raw["status"] not in STATUS or not isinstance(raw["verdict"], str) or raw["verdict"] not in VERDICT):
        raise BenchmarkError("observation status or verdict invalid")
    _utc(raw["accessed_at"], "accessed_at")
    latency = raw["latency_ms"]
    if isinstance(latency, bool) or not isinstance(latency, int) or not 0 <= latency <= 3600000:
        raise BenchmarkError("latency_ms invalid")
    cost = _finite_cost(raw["cost_usd"])
    result_url = raw["result_url"]
    if result_url is not None:
        _public_url(result_url)
    if raw["status"] != "SUCCESS" and result_url is not None:
        raise BenchmarkError("failed observations cannot claim a fetched URL")
    if raw["status"] != "SUCCESS" and raw["verdict"] == "AGREES":
        raise BenchmarkError("failed observations cannot claim agreement")
    review = raw["review"]
    if review is None:
        if raw["verdict"] != "UNVERIFIED":
            raise BenchmarkError("verdict requires independent-review declaration")
    else:
        if not isinstance(review, dict) or set(review) != {
            "reviewer_id", "reviewed_at", "primary_url"
        }:
            raise BenchmarkError("review metadata invalid")
        reviewer = review["reviewer_id"]
        if not isinstance(reviewer, str) or IDENTITY.fullmatch(reviewer) is None:
            raise BenchmarkError("reviewer id invalid")
        _utc(review["reviewed_at"], "reviewed_at")
        _public_url(review["primary_url"], allowed_hosts=set(questions[qid]["official_hosts"]))
        if raw["verdict"] == "UNVERIFIED":
            raise BenchmarkError("reviewed rows must have a determinate verdict")
    return {
        "id": qid, "provider": provider, "status": raw["status"],
        "latency": latency, "cost": cost, "verdict": raw["verdict"],
        "reviewed": review is not None, "fallback": raw["fallback_used"],
        "official_hit": result_url is not None and urlsplit(result_url).hostname
        in questions[qid]["official_hosts"],
    }


def _fingerprint(value: Any) -> str:
    """Digest stable canonical JSON without exposing the original evidence."""
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


def evaluate(manifest: Any, observations: Iterable[Any]) -> dict[str, Any]:
    """Return aggregates only; never disclose question text, raw URLs or reviews."""
    questions = manifest_questions(manifest)
    providers = manifest["providers"]
    seen: dict[tuple[str, str], dict] = {}
    hashes: dict[tuple[str, str], str] = {}
    for raw in observations:
        normalized = _validate_observation(raw, questions, set(providers))
        key = (normalized["provider"], normalized["id"])
        if key in seen:
            raise BenchmarkError("duplicate provider/question observation")
        seen[key] = normalized
        hashes[key] = _fingerprint(raw)

    complete = all(
        (provider, qid) in seen for provider in providers for qid in questions
    )
    review_complete = complete and all(rec["reviewed"] for rec in seen.values())
    summary: dict[str, Any] = {}
    for provider in providers:
        subset = [rec for (who, _), rec in seen.items() if who == provider]
        statuses = Counter(rec["status"] for rec in subset)
        summary[provider] = {
            "observations": len(subset),
            "coverage": round(len(subset) / len(questions), 4),
            "successes": statuses["SUCCESS"],
            "timeouts": statuses["TIMEOUT"],
            "rate_limits": statuses["RATE_LIMIT"],
            "errors": statuses["ERROR"],
            "declared_contradictions": sum(rec["verdict"] == "CONTRADICTS" for rec in subset),
            "declared_omissions": sum(rec["verdict"] == "OMITTED" for rec in subset),
            "unverified": sum(rec["verdict"] == "UNVERIFIED" for rec in subset),
            "official_source_hits": sum(rec["official_hit"] for rec in subset),
            "independently_reviewed_declarations": sum(rec["reviewed"] for rec in subset),
            "fallback_count": sum(rec["fallback"] for rec in subset),
            "median_latency_ms": int(median(rec["latency"] for rec in subset))
            if len(subset) == len(questions) else None,
            "reported_cost_usd": str(sum((rec["cost"] for rec in subset), Decimal("0")))
            if len(subset) == len(questions) else None,
            "declared_agreement_rate": round(
                sum(rec["verdict"] == "AGREES" for rec in subset) / len(questions), 4
            ) if review_complete else None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "atlas_public_source_search_comparison",
        "authority": "EVALUATION_ONLY",
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": _fingerprint(manifest),
        "observation_set_sha256": (
            _fingerprint([[p, q, hashes[(p, q)]] for p, q in sorted(hashes)])
            if hashes else None
        ),
        "questions": len(questions),
        "expected_observations": len(questions) * len(providers),
        "observed": len(seen),
        "state": (
            "NO_DATA" if not seen else
            "INCOMPLETE" if not complete else
            "COMPLETE_REVIEW_DECLARED" if review_complete else
            "COMPLETE_UNVERIFIED"
        ),
        "privacy": "PUBLIC_ONLY_DECLARED_NOT_ATTESTED",
        "primary_source_agreement_attestation": "NOT_ATTESTED",
        "benchmark_pass": "NOT_ESTABLISHED",
        "providers": summary,
        "note": (
            "The caller supplies independent-review declarations. This offline "
            "tool cannot authenticate reviewer independence, confirm live source "
            "content, or infer cost savings; manual source checks and real "
            "provider runs are required before adoption."
        ),
    }


def read_json_file(path: str) -> Any:
    with open(path, "rb") as stream:
        payload = stream.read(MAX_SOURCE_SIZE + 1)
    if len(payload) > MAX_SOURCE_SIZE:
        raise BenchmarkError("benchmark file exceeds limit")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BenchmarkError("benchmark JSON invalid") from exc


def read_observations(path: str) -> list[Any]:
    with open(path, "rb") as stream:
        payload = stream.read(MAX_SOURCE_SIZE + 1)
    if len(payload) > MAX_SOURCE_SIZE:
        raise BenchmarkError("observation file exceeds limit")
    if not payload:
        return []
    result = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except ValueError as exc:
            raise BenchmarkError("observation JSONL invalid") from exc
    return result
