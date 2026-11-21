"""Public-only Parallel MCP PoC query/response normalization.

Isolated evaluation code, not an Atlas MCP runtime backend or canonical source.
No arbitrary search text or URLs are accepted. Caller-provided web content
remains untrusted; no excerpts or page bodies are stored or emitted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from statistics import median
from math import ceil
import json
import re
from typing import Any
from urllib.parse import urlsplit

from atlas.public_source_benchmark import (
    BenchmarkError, _public_url, _utc, manifest_questions
)

PARALLEL_MCP_URL = "https://mcp.parallel.ai/mcp?tools=web_search"
MAX_RESULT_COUNT = 20
STOPWORDS = frozenset({
    "what", "how", "does", "are", "the", "a", "an", "and", "in",
    "is", "do", "for", "on", "of", "to", "with", "can", "which",
    "be", "as", "or", "from", "by", "about", "that", "its",
})


class PublicMcpError(ValueError):
    """Fail closed with sanitized metadata errors only."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def public_query(manifest: dict, question_id: str) -> dict[str, Any]:
    try:
        entries = manifest_questions(manifest)
    except BenchmarkError as exc:
        raise PublicMcpError("public manifest invalid") from exc
    if not isinstance(question_id, str) or question_id not in entries:
        raise PublicMcpError("unknown approved public question")
    question = entries[question_id]
    keyword_source = re.findall(r"[A-Za-z0-9]{2,}", question["question"])
    keywords: list[str] = []
    for token in keyword_source:
        if token.lower() in STOPWORDS or token.lower() in {s.lower() for s in keywords}:
            continue
        keywords.append(token.lower())
    if len(keywords) < 3:
        raise PublicMcpError("public question has too few search terms")
    host = question["official_hosts"][0]
    return {
        "objective": (
            "Find only the current original official public source for: "
            + question["question"] + " Prefer " + host
        ),
        "search_queries": [
            "site:" + host + " " + " ".join(keywords[:4]),
            " ".join(keywords[:5]),
        ],
    }


def summarize_search_result(
    manifest: dict, question_id: str, *,
    structured: Any,
    latency_ms: int,
    retrieved_at: str,
    is_error: bool = False,
) -> dict[str, Any]:
    """Extract only official public link metadata from an MCP result."""
    try:
        questions = manifest_questions(manifest)
    except BenchmarkError as exc:
        raise PublicMcpError("public manifest invalid") from exc
    if not isinstance(question_id, str) or question_id not in questions:
        raise PublicMcpError("unknown approved public question")
    if type(latency_ms) is not int or not 0 <= latency_ms <= 120000:
        raise PublicMcpError("invalid measured latency")
    try:
        _utc(retrieved_at, "retrieved_at")
    except BenchmarkError as exc:
        raise PublicMcpError("invalid retrieval timestamp") from exc
    if (
        not isinstance(structured, dict)
        or not isinstance(structured.get("results"), list)
        or len(structured["results"]) > 250
    ):
        raise PublicMcpError("invalid external search result")
    raw_results = structured["results"]
    accepted_urls: list[str] = []
    top_official = False
    official_hosts = set(questions[question_id]["official_hosts"])
    for index, item in enumerate(raw_results[:MAX_RESULT_COUNT]):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        try:
            _public_url(url)
        except BenchmarkError:
            continue
        if urlsplit(url).hostname in official_hosts:
            accepted_urls.append(url)
            if index == 0:
                top_official = True
    # Errors cannot claim usable sources even if the untrusted provider sends
    # a partial results array alongside is_error.
    if is_error:
        accepted_urls = []
        top_official = False
    # Deduplicate original public citations while keeping result rank.
    unique_urls = list(dict.fromkeys(accepted_urls))
    return {
        "question_id": question_id,
        "provider": "parallel_mcp",
        "status": "ERROR" if is_error else "SUCCESS",
        "retrieved_at": retrieved_at,
        "latency_ms": latency_ms,
        "results_count": len(raw_results),
        "official_rank1": top_official,
        "official_hit": bool(unique_urls),
        "official_citations": unique_urls[:2],
        "official_citation_sha256": [
            sha256(url.encode()).hexdigest() for url in unique_urls[:2]
        ],
        "source_content_saved": False,
        "answer_verified": False,
        "cost_usd": None,
        "cash_cost_authority": "UNAUTHENTICATED_FREE_TIER_PRICING_NOT_METERED",
    }


def summarize_run(manifest: dict, records: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        questions = manifest_questions(manifest)
    except BenchmarkError as exc:
        raise PublicMcpError("public manifest invalid") from exc
    if not isinstance(records, list) or len(records) > len(questions):
        raise PublicMcpError("invalid run result length")
    seen = set()
    for rec in records:
        if not isinstance(rec, dict) or rec.get("question_id") not in questions:
            raise PublicMcpError("unknown recorded public question")
        if rec["question_id"] in seen:
            raise PublicMcpError("duplicate public question result")
        seen.add(rec["question_id"])
        if rec.get("provider") != "parallel_mcp" or rec.get("answer_verified") is not False:
            raise PublicMcpError("unsupported result authority")
    successful = [r for r in records if r.get("status") == "SUCCESS"]
    sample = sorted(r["latency_ms"] for r in successful)
    return {
        "kind": "atlas_parallel_public_mcp_poc",
        "state": "NO_DATA" if not records else (
            "PARTIAL" if len(records) < len(questions) else "PROVIDER_OBSERVED_UNREVIEWED"
        ),
        "authority": "EVALUATION_ONLY",
        "questions_total": len(questions),
        "questions_attempted": len(records),
        "successful_tool_calls": len(successful),
        "official_host_hits": sum(bool(r["official_hit"]) for r in successful),
        "official_rank1_hits": sum(bool(r["official_rank1"]) for r in successful),
        "median_success_latency_ms": round(median(sample)) if sample else None,
        "p95_success_latency_ms": sample[ceil(0.95 * len(sample)) - 1] if sample else None,
        "sample_latency_ms": [r["latency_ms"] for r in records],
        "provider_rate_limit_count": sum(r.get("status") == "RATE_LIMIT" for r in records),
        "fallback_count": 0,
        "source_review": "NOT_PERFORMED",
        "comparison_baseline": "NOT_COLLECTED",
        "effective_search_quality": "NOT_ESTABLISHED",
        "provider_production_adoption": "NOT_AUTHORIZED",
        "notes": (
            "The anonymous public MCP result is untrusted, output URLs are "
            "metadata only, no excerpt cache. Without reviewed baseline and "
            "source text, neither accuracy nor cost improvement is established."
        ),
    }
