"""Bounded final-audit provider (Issue #47 slice B).

Extends the existing Responses API adapter. It does not open a second audit
framework, does not call the API when idle or over budget, and does not treat
a missing credential as PASS.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from atlas.chat_audit import AuditControlPacket, require_exact_commit_sha
from atlas.codex_audit import (
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MAX_PACKET_CHARS,
    _deterministic_gate_before_codex,
    _revalidate_clean_audited_snapshot,
    collect_audit_evidence_bundle,
    incomplete_evidence_verdict,
)
from atlas.provenance import ValidationError
from atlas.secrets import redact_sensitive_audit_text
from atlas.work_controller import (
    AuditResult,
    CompletionEvent,
    OpenAIResponsesAuditAdapter,
    WorkstreamRecord,
    WorktreeIdentity,
    _contains_unsafe_secret,
    extract_response_output_text,
    normalize_github_repository,
    parse_audit_verdict_payload,
    validate_worktree_identity,
)

FINAL_AUDIT_MODEL = "gpt-5.6-sol"
MAX_FINDINGS_CHARS = 2000
MAX_CHECKPOINT_CHARS = 4000
# Explicit output cap so a per-run hard budget can be checked before the call.
MAX_AUDIT_OUTPUT_TOKENS = 2048
INPUT_USD_PER_TOKEN = 4 / 1_000_000
CACHED_INPUT_USD_PER_TOKEN = 0.40 / 1_000_000
CACHE_WRITE_MULTIPLIER = 1.25
OUTPUT_USD_PER_TOKEN = 20 / 1_000_000

FINAL_AUDIT_DEVELOPER_PROMPT = """You are an independent engineering auditor.
Return only the structured disposition.
Verdict must be PASS, REWORK, or HUMAN_REQUIRED.
PASS only when the evidence suffix satisfies the acceptance criteria.
REWORK when a bounded fix remains in that suffix.
HUMAN_REQUIRED when owner judgment or credentials are required.
Do not invent repository contents that are absent from the evidence suffix.
Do not include secrets, credentials, or local home paths.
"""

AUDIT_DISPOSITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings", "target_sha"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS", "REWORK", "HUMAN_REQUIRED"],
        },
        "findings": {"type": "string"},
        "target_sha": {"type": "string"},
    },
}


@dataclass
class AuditBudget:
    per_run_hard_usd: float
    monthly_hard_usd: float
    month_spent_usd: float = 0.0
    preflight_usd: float = 0.05
    per_run_soft_usd: float | None = None
    monthly_soft_usd: float | None = None

    def blocked_before_call(self, *, request_ceiling_usd: float | None = None) -> str | None:
        """Fail closed before a paid call.

        ``preflight_usd`` is a caller reservation. ``request_ceiling_usd`` is
        the conservative cost of the exact request about to be sent. A
        configured soft limit blocks the same way as a hard limit.
        """
        if (
            self.per_run_soft_usd is not None
            and self.preflight_usd > self.per_run_soft_usd
        ):
            return "per-run soft budget exceeded"
        if (
            self.monthly_soft_usd is not None
            and self.month_spent_usd + self.preflight_usd > self.monthly_soft_usd
        ):
            return "monthly soft budget exceeded"
        if self.preflight_usd > self.per_run_hard_usd:
            return "per-run budget exceeded"
        if self.month_spent_usd + self.preflight_usd > self.monthly_hard_usd:
            return "monthly budget exceeded"
        if request_ceiling_usd is None:
            return None
        if (
            self.per_run_soft_usd is not None
            and request_ceiling_usd > self.per_run_soft_usd
        ):
            return "per-run soft budget exceeded"
        if (
            self.monthly_soft_usd is not None
            and self.month_spent_usd + request_ceiling_usd > self.monthly_soft_usd
        ):
            return "monthly soft budget exceeded"
        if request_ceiling_usd > self.per_run_hard_usd:
            return "per-run budget exceeded"
        if self.month_spent_usd + request_ceiling_usd > self.monthly_hard_usd:
            return "monthly budget exceeded"
        return None

    def reconcile(self, cost_usd: float) -> str | None:
        self.month_spent_usd += cost_usd
        if cost_usd > self.per_run_hard_usd:
            return "per-run budget exceeded after usage"
        if self.month_spent_usd > self.monthly_hard_usd:
            return "monthly budget exceeded after usage"
        return None


@dataclass(frozen=True)
class AuditTelemetry:
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float
    target_sha: str
    verdict: str
    duration_sec: float = 0.0

    def public_record(self) -> dict[str, object]:
        """Numeric telemetry only. No prompts and no credentials."""
        return asdict(self)


def checkpoint_evidence_excerpt(packet: AuditControlPacket) -> str:
    """Bounded prior-checkpoint suffix from the existing control packet."""
    text = "\n".join(
        [
            f"repository={packet.target_repository}",
            f"branch={packet.target_branch}",
            f"target_sha={packet.current_target_sha}",
            f"status={packet.audit_status}",
            f"next_action={packet.next_action}",
        ]
    )
    return _redact(text, MAX_CHECKPOINT_CHARS)


def estimate_audit_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
) -> float:
    """Bill disjoint parts of total input. Do not charge cache tokens twice.

    ordinary = input - cached - cache_write. Cached input is 0.1x the input
    price. Cache writes are 1.25x. Output is priced separately.
    """
    total_input = max(input_tokens, 0)
    cached = min(max(cached_tokens, 0), total_input)
    remaining = total_input - cached
    cache_write = min(max(cache_write_tokens, 0), remaining)
    ordinary = remaining - cache_write
    return (
        ordinary * INPUT_USD_PER_TOKEN
        + cached * CACHED_INPUT_USD_PER_TOKEN
        + cache_write * INPUT_USD_PER_TOKEN * CACHE_WRITE_MULTIPLIER
        + max(output_tokens, 0) * OUTPUT_USD_PER_TOKEN
    )


def request_cost_ceiling_usd(body: dict) -> float:
    """Upper bound for one Responses request before it is sent.

    Every input character is billed as a cache-write token, the expensive
    input class. Output cannot exceed the explicit ``max_output_tokens`` cap.
    A request without that cap is treated as unbounded.
    """
    output_cap = body.get("max_output_tokens")
    if isinstance(output_cap, bool) or not isinstance(output_cap, int) or output_cap <= 0:
        return float("inf")
    input_tokens = len(json.dumps(body, sort_keys=True))
    return estimate_audit_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_cap,
        cached_tokens=0,
        cache_write_tokens=input_tokens,
    )


def extract_responses_usage(payload: dict) -> dict[str, int]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = {}
    cache_write = details.get("cache_write_tokens", usage.get("cache_write_tokens", 0))
    return {
        "input_tokens": _as_token_count(usage.get("input_tokens")),
        "output_tokens": _as_token_count(usage.get("output_tokens")),
        "cached_tokens": _as_token_count(details.get("cached_tokens")),
        "cache_write_tokens": _as_token_count(cache_write),
    }


def build_final_audit_request(
    bundle: dict,
    *,
    model: str = FINAL_AUDIT_MODEL,
) -> dict:
    """Stable instructions first, then the collected evidence bundle."""
    if not str(model or "").strip():
        raise ValidationError("audit model is required")
    assert_bundle_within_collector_bounds(bundle)
    suffix = _redact(json.dumps(bundle, sort_keys=True), 10**9)
    if _contains_unsafe_secret(suffix):
        raise ValidationError(
            "audit evidence contained credential-like material after redaction"
        )
    return {
        "model": model,
        "background": True,
        "store": False,
        "max_output_tokens": MAX_AUDIT_OUTPUT_TOKENS,
        "input": [
            {"role": "developer", "content": FINAL_AUDIT_DEVELOPER_PROMPT},
            {"role": "user", "content": suffix},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "audit_disposition",
                "strict": True,
                "schema": AUDIT_DISPOSITION_SCHEMA,
            }
        },
    }


def assert_bundle_within_collector_bounds(bundle: dict) -> None:
    """Refuse a bundle that bypasses collector size limits."""
    git = bundle.get("git") if isinstance(bundle.get("git"), dict) else {}
    for key in ("diff", "workdir_diff", "staged_diff"):
        text = str(git.get(key) or "")
        if len(text) > DEFAULT_MAX_DIFF_CHARS:
            raise ValidationError(
                f"git.{key} exceeds collector limit {DEFAULT_MAX_DIFF_CHARS}"
            )
    packet = bundle.get("work_packet")
    if isinstance(packet, dict):
        body = str(packet.get("body") or "")
        if len(body) > DEFAULT_MAX_PACKET_CHARS:
            raise ValidationError(
                f"work_packet.body exceeds collector limit {DEFAULT_MAX_PACKET_CHARS}"
            )


def refuse_stale_bundle_head(bundle: dict, event: CompletionEvent) -> None:
    identity = bundle.get("identity") if isinstance(bundle.get("identity"), dict) else {}
    git = bundle.get("git") if isinstance(bundle.get("git"), dict) else {}
    expected = event.head.lower()
    for label, value in (
        ("identity.head", identity.get("head")),
        ("git.head", git.get("head")),
    ):
        observed = str(value or "").strip().lower()
        if not observed or observed.startswith("error:"):
            continue
        if observed != expected:
            raise ValidationError(
                f"stale HEAD: evidence {label}={observed} event={expected}"
            )


class BoundedResponsesAuditProvider(OpenAIResponsesAuditAdapter):
    """Responses API final audit over a bounded evidence bundle.

    Real paid Gate B calls require OPENAI_API_KEY. Without it the result is
    HUMAN_REQUIRED and the transport is not called.
    """

    def __init__(
        self,
        *,
        budget: AuditBudget | None = None,
        model: str = FINAL_AUDIT_MODEL,
        api_key_env: str = "OPENAI_API_KEY",
        git_runner=None,
        command_runner=None,
        evidence_bundle_override: dict | None = None,
        require_identity: bool = True,
        include_tests: bool = True,
        include_ci: bool = True,
        include_work_packet: bool = True,
        base_ref: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(model=model, api_key_env=api_key_env, **kwargs)
        self.budget = budget or AuditBudget(per_run_hard_usd=1.0, monthly_hard_usd=25.0)
        self.last_telemetry: AuditTelemetry | None = None
        self._git_runner = git_runner
        self._command_runner = command_runner
        self.evidence_bundle_override = evidence_bundle_override
        self.require_identity = require_identity
        self.include_tests = include_tests
        self.include_ci = include_ci
        self.include_work_packet = include_work_packet
        self.base_ref = base_ref
        self.last_evidence_bundle: dict | None = None

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        """Collect the Codex evidence bundle, then judge it with Responses API."""
        if self.require_identity:
            validate_worktree_identity(
                record.worktree_path,
                repository=record.repository,
                branch=event.branch,
                expected_head=event.head,
                git_runner=self._git_runner,
            )
        if self.evidence_bundle_override is not None:
            bundle = dict(self.evidence_bundle_override)
        else:
            identity = WorktreeIdentity(
                worktree_path=str(Path(record.worktree_path).resolve()),
                repository=normalize_github_repository(record.repository),
                branch=event.branch,
                head=event.head.lower(),
                toplevel=str(Path(record.worktree_path).resolve()),
            )
            bundle = collect_audit_evidence_bundle(
                event,
                record,
                identity=identity,
                base_ref=self.base_ref,
                git_runner=self._git_runner,
                command_runner=self._command_runner,
                include_tests=self.include_tests,
                include_ci=self.include_ci,
                include_work_packet=self.include_work_packet,
            )
        self.last_evidence_bundle = bundle
        return self.audit_bundle(bundle, event=event, record=record)

    def audit_bundle(
        self,
        bundle: dict,
        *,
        event: CompletionEvent | None = None,
        record: WorkstreamRecord | None = None,
    ) -> AuditResult:
        self.last_request_body = None
        self.last_evidence_bundle = bundle
        if event is not None:
            refuse_stale_bundle_head(bundle, event)
        target_sha = _bundle_target_sha(bundle, event)
        incomplete = incomplete_evidence_verdict(bundle)
        if incomplete is not None:
            return incomplete
        if self.require_identity:
            if event is None or record is None:
                raise ValidationError("identity revalidation requires event and record")
            drift = _revalidate_clean_audited_snapshot(
                event, record, git_runner=self._git_runner
            )
            if drift:
                return self._closed(
                    target_sha,
                    "audit skipped: audited worktree not a clean autonomous "
                    f"snapshot after evidence collection ({drift[:300]})",
                )
        gated = _deterministic_gate_before_codex(bundle)
        if gated is not None:
            return gated
        try:
            body = build_final_audit_request(bundle, model=self.model)
        except ValidationError as exc:
            return self._closed(target_sha, str(exc))
        blocked = self.budget.blocked_before_call(
            request_ceiling_usd=request_cost_ceiling_usd(body)
        )
        if blocked:
            return self._closed(target_sha, blocked)
        api_key = os.environ.get(self._api_key_env, "").strip()
        if not api_key:
            return self._closed(
                target_sha,
                "OPENAI_API_KEY absent; Gate B real audit is HUMAN_REQUIRED",
            )
        self.last_request_body = body
        started = time.perf_counter()
        created = self._create_with_retries(api_key, body)
        response_id = str(created.get("id", "")).strip()
        if not response_id:
            raise ValidationError("OpenAI create response missing id")
        self.last_response_id = response_id
        payload = self._poll_until_terminal(api_key, response_id, created)
        result = self._disposition(response_id, payload, expected_sha=target_sha)
        usage = extract_responses_usage(payload)
        cost = estimate_audit_cost_usd(**usage)
        over = self.budget.reconcile(cost)
        verdict = result.verdict
        findings = result.findings
        if over:
            verdict = "HUMAN_REQUIRED"
            findings = _redact(f"{over}. {findings}", MAX_FINDINGS_CHARS)
        else:
            findings = _redact(findings, MAX_FINDINGS_CHARS)
        if self.require_identity and verdict in {"PASS", "REWORK"}:
            if event is None or record is None:
                raise ValidationError("identity revalidation requires event and record")
            drift = _revalidate_clean_audited_snapshot(
                event, record, git_runner=self._git_runner
            )
            if drift:
                verdict = "HUMAN_REQUIRED"
                findings = _redact(
                    f"{verdict} rejected: audited worktree not a clean "
                    f"autonomous snapshot ({drift[:300]})",
                    MAX_FINDINGS_CHARS,
                )
        self.last_telemetry = AuditTelemetry(
            model=self.model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_tokens=usage["cached_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
            estimated_cost_usd=cost,
            target_sha=target_sha,
            verdict=verdict,
            duration_sec=max(0.0, time.perf_counter() - started),
        )
        return AuditResult(verdict=verdict, findings=findings)

    def _poll_until_terminal(self, api_key: str, response_id: str, created: dict) -> dict:
        status = str(created.get("status", "")).strip()
        payload = created
        polls = 0
        while status in {"queued", "in_progress"}:
            if polls >= self.max_polls:
                payload = dict(payload)
                payload["status"] = status
                payload["_poll_exhausted"] = True
                return payload
            self._sleep(self.poll_interval_sec)
            payload = self.transport.request_json(
                "GET", f"/responses/{response_id}", api_key=api_key
            )
            status = str(payload.get("status", "")).strip()
            self.poll_statuses.append(status)
            polls += 1
        return payload

    def _disposition(self, response_id: str, payload: dict, *, expected_sha: str) -> AuditResult:
        status = str(payload.get("status", "")).strip()
        if payload.get("_poll_exhausted"):
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=f"openai response {response_id} still {status} after max polls",
            )
        if _response_is_refusal(payload):
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=f"openai response {response_id} refusal",
            )
        if status != "completed":
            return self._map_terminal(response_id, status, payload)
        text = extract_response_output_text(payload)
        try:
            result = parse_audit_verdict_payload(text)
            echoed = _target_sha(text)
        except ValidationError as exc:
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=f"openai response {response_id} malformed disposition: {exc}",
            )
        if echoed != expected_sha:
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=(
                    f"openai response {response_id} target_sha mismatch "
                    f"expected={expected_sha}"
                ),
            )
        return result

    def _closed(self, target_sha: str, reason: str) -> AuditResult:
        self.last_telemetry = AuditTelemetry(
            model=self.model,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=0.0,
            target_sha=target_sha,
            verdict="HUMAN_REQUIRED",
        )
        return AuditResult(
            verdict="HUMAN_REQUIRED",
            findings=_redact(reason, MAX_FINDINGS_CHARS),
        )


def run_bounded_final_audit(
    *,
    actionable: bool,
    evidence: dict | None,
    provider: BoundedResponsesAuditProvider,
    event: CompletionEvent | None = None,
    record: WorkstreamRecord | None = None,
) -> dict[str, object]:
    """Idle and non-actionable passes perform zero provider calls."""
    if not actionable or evidence is None:
        return {
            "action": "idle_noop",
            "api_calls": 0,
            "verdict": None,
            "telemetry": None,
        }
    result = provider.audit_bundle(evidence, event=event, record=record)
    called = provider.last_request_body is not None
    telemetry = (
        provider.last_telemetry.public_record() if provider.last_telemetry else None
    )
    return {
        "action": "audited" if called else "not_called",
        "api_calls": 1 if called else 0,
        "verdict": result.verdict,
        "findings": result.findings,
        "telemetry": telemetry,
    }


def _as_token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value < 0:
        return 0
    return int(value)


def _bundle_target_sha(bundle: dict, event: CompletionEvent | None) -> str:
    if event is not None:
        return require_exact_commit_sha(event.head, label="event.head")
    identity = bundle.get("identity") if isinstance(bundle.get("identity"), dict) else {}
    git = bundle.get("git") if isinstance(bundle.get("git"), dict) else {}
    raw = str(identity.get("head") or git.get("head") or "")
    return require_exact_commit_sha(raw, label="bundle head")


def _redact(text: str, limit: int) -> str:
    try:
        return redact_sensitive_audit_text(text or "", max_chars=limit)
    except Exception:
        return "<redacted>"


def _response_is_refusal(payload: dict) -> bool:
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") == "refusal":
            return True
        for part in item.get("content") or []:
            if isinstance(part, dict) and str(part.get("type") or "") == "refusal":
                return True
    return False


def _target_sha(text: str) -> str:
    raw = text.strip()
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValidationError("audit response did not contain a JSON object")
        raw = match.group(0)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValidationError("audit response JSON must be an object")
    return require_exact_commit_sha(str(data.get("target_sha", "")), label="target_sha")
