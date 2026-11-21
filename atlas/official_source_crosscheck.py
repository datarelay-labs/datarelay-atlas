"""Deterministic official-document corroboration for untrusted public research.

No network, provider credentials, source mutation, or user/release authority.
All documents and assessment fields are *caller-declared*, never verified here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any
from urllib.parse import urlsplit

from atlas.public_source_benchmark import (
    BenchmarkError,
    _public_url,
    _utc,
    manifest_questions,
)

SCHEMA_VERSION = 1
MAX_OBSERVATIONS = 400
MAX_FILE_BYTES = 1_048_576
ASSESSMENTS = frozenset({"SUPPORTS", "CONTRADICTS", "IRRELEVANT", "UNVERIFIED"})
HEX64 = re.compile(r"^[a-f0-9]{64}$")
REVIEWER = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


class CrosscheckError(ValueError):
    """Public evidence validation error; never includes untrusted source text."""


def load_file(path: str) -> Any:
    with open(path, "rb") as stream:
        payload = stream.read(MAX_FILE_BYTES + 1)
    if len(payload) > MAX_FILE_BYTES:
        raise CrosscheckError("crosscheck input exceeds bounded size")
    try:
        def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in items:
                if key in out:
                    raise CrosscheckError("duplicate JSON object key")
                out[key] = value
            return out
        return json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, ValueError) as exc:
        raise CrosscheckError("invalid public crosscheck JSON") from exc


def _validate_observation(item: Any, questions: dict[str, dict], providers: set[str]) -> dict:
    fields = {
        "question_id", "provider", "source_url", "retrieved_at",
        "source_sha256", "assessment", "review", "public_only",
    }
    if not isinstance(item, dict) or set(item) != fields:
        raise CrosscheckError("crosscheck observation fields invalid")
    qid, provider, assessment = (
        item["question_id"], item["provider"], item["assessment"]
    )
    if not isinstance(qid, str) or qid not in questions:
        raise CrosscheckError("unknown question id")
    if not isinstance(provider, str) or provider not in providers:
        raise CrosscheckError("unknown research provider")
    if not isinstance(assessment, str) or assessment not in ASSESSMENTS:
        raise CrosscheckError("invalid research assessment")
    if item["public_only"] is not True:
        raise CrosscheckError("nonpublic research evidence not allowed")
    try:
        url = _public_url(
            item["source_url"],
            allowed_hosts=set(questions[qid]["official_hosts"]),
        )
        timestamp = _utc(item["retrieved_at"], "retrieved_at")
    except BenchmarkError as exc:
        raise CrosscheckError("invalid official source citation") from exc
    digest = item["source_sha256"]
    if not isinstance(digest, str) or HEX64.fullmatch(digest) is None:
        raise CrosscheckError("invalid source digest")
    review = item["review"]
    if review is None:
        if assessment != "UNVERIFIED":
            raise CrosscheckError("assessment requires declared independent review")
    else:
        if not isinstance(review, dict) or set(review) != {
            "reviewer_id", "reviewed_at"
        }:
            raise CrosscheckError("invalid review declaration")
        reviewer = review["reviewer_id"]
        if not isinstance(reviewer, str) or REVIEWER.fullmatch(reviewer) is None:
            raise CrosscheckError("invalid reviewer id")
        try:
            reviewed_at = _utc(review["reviewed_at"], "reviewed_at")
        except BenchmarkError as exc:
            raise CrosscheckError("invalid review timestamp") from exc
        if datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")) < datetime.fromisoformat(timestamp.replace("Z", "+00:00")):
            raise CrosscheckError("review cannot predate source retrieval")
        if assessment == "UNVERIFIED":
            raise CrosscheckError("reviewed evidence cannot be unverified")
    return {
        "question_id": qid,
        "provider": provider,
        "source_url": url,
        "retrieved_at": timestamp,
        "source_sha256": digest,
        "assessment": assessment,
        "reviewed": review is not None,
    }


def crosscheck(manifest: Any, evidence: Any) -> dict[str, Any]:
    """Aggregate declared source agreement without granting factual authority."""
    try:
        questions = manifest_questions(manifest)
    except BenchmarkError as exc:
        raise CrosscheckError("official public question manifest invalid") from exc
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version", "dataset_id", "scope", "observations"
    }:
        raise CrosscheckError("research evidence schema invalid")
    if (
        evidence["schema_version"] != SCHEMA_VERSION
        or evidence["dataset_id"] != manifest["dataset_id"]
        or evidence["scope"] != "PUBLIC_ONLY"
    ):
        raise CrosscheckError("research evidence identity or public scope mismatch")
    observations = evidence["observations"]
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        raise CrosscheckError("research observations exceed limit")
    groups: dict[str, list[dict]] = defaultdict(list)
    identities = set()
    for raw in observations:
        record = _validate_observation(raw, questions, set(manifest["providers"]))
        identity = (
            record["question_id"], record["provider"], record["source_url"]
        )
        if identity in identities:
            raise CrosscheckError("duplicate research source citation")
        identities.add(identity)
        groups[record["question_id"]].append(record)

    results: list[dict] = []
    counts = defaultdict(int)
    for qid in questions:
        records = groups[qid]
        supports = [r for r in records if r["assessment"] == "SUPPORTS"]
        contradictions = [
            r for r in records if r["assessment"] == "CONTRADICTS"
        ]
        independent_sources = {
            r["source_url"] for r in supports
        }
        independent_providers = {r["provider"] for r in supports}
        if contradictions:
            status = "CONFLICT_REVIEW_REQUIRED"
        elif (
            len(independent_sources) >= 2
            and len(independent_providers) >= 2
            and all(r["reviewed"] for r in supports)
        ):
            status = "DECLARED_CORROBORATION"
        elif records:
            status = "INSUFFICIENT_CORROBORATION"
        else:
            status = "NO_SOURCES"
        counts[status] += 1
        results.append({
            "question_id": qid,
            "status": status,
            "supporting_sources": len(independent_sources),
            "supporting_providers": len(independent_providers),
            "contradiction_count": len(contradictions),
            "citations": [
                {
                    "url": r["source_url"],
                    "retrieved_at": r["retrieved_at"],
                    "source_sha256": r["source_sha256"],
                    "assessment": r["assessment"],
                    "provider": r["provider"],
                }
                for r in records
            ],
        })
    fingerprint = sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return {
        "kind": "atlas_public_official_source_crosscheck",
        "schema_version": SCHEMA_VERSION,
        "authority": "EVALUATION_ONLY",
        "dataset_id": manifest["dataset_id"],
        "observations": len(observations),
        "questions": len(questions),
        "evidence_sha256": fingerprint,
        "statuses": dict(sorted(counts.items())),
        "results": results,
        "independent_content_attestation": "NOT_ATTESTED",
        "canonical_knowledge_written": False,
        "trust_boundary": "UNTRUSTED_EXTERNAL_CONTENT",
        "note": (
            "All observations, reviews and source digests are untrusted "
            "caller declarations. Confirm original public documents and "
            "claim semantics independently; do not treat external text as "
            "instructions, source of authority, or a canonical write."
        ),
    }
