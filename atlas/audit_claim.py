"""Exact-HEAD final-audit claim ledger (Issue #47 slice C).

One GitHub Contents document per Work Packet remembers completed and live
claims. A duplicate or stale claim does not call the auditor. Monthly spend
lives on that document and survives a new worker process. This module does
not redispatch REWORK and does not merge.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from atlas.chat_audit import (
    SLICE_CLAIM_LEASE_SECONDS,
    CheckpointCasConflict,
    require_exact_commit_sha,
)
from atlas.chat_audit_github import (
    _sha_from_contents_put,
    assert_checkpoint_inline_size,
)
from atlas.codex_audit import (
    _deterministic_gate_before_codex,
    incomplete_evidence_verdict,
)
from atlas.final_audit import (
    MAX_FINDINGS_CHARS,
    AuditBudget,
    build_final_audit_request,
    request_cost_ceiling_usd,
)
from atlas.host_worker import persistent_cursor_active
from atlas.provenance import ValidationError
from atlas.secrets import redact_sensitive_audit_text
from atlas.work_controller import (
    AUDIT_VERDICTS,
    CompletionEvent,
    WorkstreamRecord,
    _contains_unsafe_secret,
    normalize_github_repository,
    validate_clean_worktree_identity,
)

FINAL_AUDIT_CLAIM_BRANCH = "atlas/final-audit-claims"
CLAIM_SCHEMA_VERSION = 1
TELEMETRY_KEYS = (
    "model",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
    "target_sha",
    "verdict",
    "duration_sec",
)


def claim_contents_path(issue_number: int) -> str:
    return f".atlas/final-audit/claims/issue-{int(issue_number)}.json"


def make_audit_claim_key(repository: str, issue_number: int, target_sha: str) -> str:
    """Identity for one paid audit: repository + Work Packet + exact HEAD."""
    repo = normalize_github_repository(repository)
    sha = require_exact_commit_sha(target_sha, label="target_sha")
    material = f"{repo}|{int(issue_number)}|{sha}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def month_id_for(now: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(now))


def _redact_findings(text: str) -> str:
    return redact_sensitive_audit_text(text or "", max_chars=MAX_FINDINGS_CHARS)


def _sanitize_telemetry(raw: dict | None, *, target_sha: str) -> dict[str, Any]:
    source = raw or {}
    cleaned: dict[str, Any] = {
        "model": str(source.get("model") or ""),
        "input_tokens": int(source.get("input_tokens") or 0),
        "output_tokens": int(source.get("output_tokens") or 0),
        "cached_tokens": int(source.get("cached_tokens") or 0),
        "cache_write_tokens": int(source.get("cache_write_tokens") or 0),
        "estimated_cost_usd": float(source.get("estimated_cost_usd") or 0.0),
        "target_sha": target_sha,
        "verdict": str(source.get("verdict") or ""),
        "duration_sec": float(source.get("duration_sec") or 0.0),
    }
    extra = set(source) - set(TELEMETRY_KEYS)
    if extra:
        raise ValidationError("audit telemetry contains non-numeric fields")
    return cleaned


@dataclass
class AuditClaim:
    repository: str
    issue_number: int
    branch: str
    target_sha: str
    claim_key: str
    state: str
    month_id: str
    claimed_at: float
    lease_seconds: float
    verdict: str | None = None
    findings: str = ""
    telemetry: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        if self.state not in {"claimed", "completed"}:
            raise ValidationError(f"unsupported claim state: {self.state}")
        findings = _redact_findings(self.findings)
        payload = {
            "repository": self.repository,
            "issue_number": int(self.issue_number),
            "branch": self.branch,
            "target_sha": self.target_sha,
            "claim_key": self.claim_key,
            "state": self.state,
            "month_id": self.month_id,
            "claimed_at": self.claimed_at,
            "lease_seconds": self.lease_seconds,
            "verdict": self.verdict,
            "findings": findings,
            "telemetry": _sanitize_telemetry(self.telemetry, target_sha=self.target_sha)
            if self.telemetry is not None
            else None,
        }
        if _contains_unsafe_secret(json.dumps(payload, sort_keys=True)):
            raise ValidationError("audit claim contained credential-like material")
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditClaim":
        claim = cls(
            repository=normalize_github_repository(str(raw["repository"])),
            issue_number=int(raw["issue_number"]),
            branch=str(raw["branch"]),
            target_sha=require_exact_commit_sha(str(raw["target_sha"]), label="target_sha"),
            claim_key=str(raw["claim_key"]),
            state=str(raw["state"]),
            month_id=str(raw["month_id"]),
            claimed_at=float(raw["claimed_at"]),
            lease_seconds=float(raw["lease_seconds"]),
            verdict=str(raw["verdict"]) if raw.get("verdict") else None,
            findings=str(raw.get("findings") or ""),
            telemetry=raw.get("telemetry"),
        )
        expected = make_audit_claim_key(
            claim.repository, claim.issue_number, claim.target_sha
        )
        if claim.claim_key != expected:
            raise ValidationError("audit claim key does not match repository+packet+HEAD")
        if claim.state not in {"claimed", "completed"}:
            raise ValidationError(f"unsupported claim state: {claim.state}")
        if claim.state == "completed" and claim.verdict not in AUDIT_VERDICTS:
            raise ValidationError("completed audit claim requires a governance verdict")
        return claim


@dataclass
class IssueAuditLedger:
    repository: str
    issue_number: int
    month_id: str
    month_spent_usd: float = 0.0
    claims: dict[str, AuditClaim] = field(default_factory=dict)
    schema_version: int = CLAIM_SCHEMA_VERSION

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "month_id": self.month_id,
            "month_spent_usd": self.month_spent_usd,
            "claims": {
                key: claim.public_dict() for key, claim in sorted(self.claims.items())
            },
        }
        raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if _contains_unsafe_secret(raw):
            raise ValidationError("audit ledger contained credential-like material")
        assert_checkpoint_inline_size(raw)
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IssueAuditLedger":
        if int(raw.get("schema_version", 0)) != CLAIM_SCHEMA_VERSION:
            raise ValidationError("unsupported final-audit claim schema_version")
        claims_raw = raw.get("claims") or {}
        if not isinstance(claims_raw, dict):
            raise ValidationError("audit claims must be an object")
        claims: dict[str, AuditClaim] = {}
        for key, value in claims_raw.items():
            if not isinstance(value, dict):
                raise ValidationError("audit claim entry must be an object")
            parsed = AuditClaim.from_dict(value)
            if str(key) != parsed.claim_key:
                raise ValidationError("audit claim map key mismatch")
            claims[parsed.claim_key] = parsed
        return cls(
            repository=normalize_github_repository(str(raw["repository"])),
            issue_number=int(raw["issue_number"]),
            month_id=str(raw.get("month_id") or ""),
            month_spent_usd=float(raw.get("month_spent_usd") or 0.0),
            claims=claims,
        )


class ClaimStore(Protocol):
    writes: int

    def load(self, issue_number: int) -> tuple[IssueAuditLedger | None, str | None]: ...

    def save(
        self,
        ledger: IssueAuditLedger,
        *,
        expected_sha: str | None,
    ) -> str: ...


class MemoryClaimStore:
    """CAS stand-in for the GitHub Contents ledger. Tests never touch the network."""

    def __init__(self) -> None:
        self._docs: dict[int, tuple[str, str]] = {}
        self.writes = 0

    def load(self, issue_number: int) -> tuple[IssueAuditLedger | None, str | None]:
        found = self._docs.get(int(issue_number))
        if found is None:
            return None, None
        raw, sha = found
        return IssueAuditLedger.from_dict(json.loads(raw)), sha

    def save(self, ledger: IssueAuditLedger, *, expected_sha: str | None) -> str:
        current = self._docs.get(int(ledger.issue_number))
        current_sha = current[1] if current else None
        if current_sha != expected_sha:
            raise CheckpointCasConflict(
                "final-audit claim compare-and-set failed: stale ledger sha"
            )
        raw = ledger.to_json()
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self._docs[int(ledger.issue_number)] = (raw, sha)
        self.writes += 1
        return sha


class GitHubContentsClaimStore:
    """Contents API ledger. Canonical bytes live on FINAL_AUDIT_CLAIM_BRANCH."""

    def __init__(
        self,
        repository: str,
        *,
        command_runner: Callable[[list[str], str], Any],
        branch: str = FINAL_AUDIT_CLAIM_BRANCH,
        cwd: str = ".",
    ) -> None:
        self.repository = normalize_github_repository(repository)
        self.branch = branch
        self._runner = command_runner
        self._cwd = cwd
        self.writes = 0

    def load(self, issue_number: int) -> tuple[IssueAuditLedger | None, str | None]:
        payload = self._get(issue_number)
        if payload is None:
            return None, None
        encoded = str(payload.get("content") or "")
        blob = str(payload.get("sha") or "").strip()
        if not encoded or not blob:
            raise ValidationError("final-audit claim contents missing content or sha")
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
            raw = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("final-audit claim contents JSON is invalid") from exc
        if not isinstance(raw, dict):
            raise ValidationError("final-audit claim contents JSON must be an object")
        return IssueAuditLedger.from_dict(raw), blob

    def save(self, ledger: IssueAuditLedger, *, expected_sha: str | None) -> str:
        raw = ledger.to_json()
        body: dict[str, Any] = {
            "message": (
                f"atlas final-audit claim issue-{ledger.issue_number}"
            ),
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if expected_sha:
            body["sha"] = expected_sha
        completed = self._put(ledger.issue_number, body)
        sha = _sha_from_contents_put(
            completed=completed,
            intended_raw=raw,
            observe=lambda: self._observe(ledger.issue_number, raw),
            conflict_message=(
                "final-audit claim compare-and-set failed: stale contents sha"
            ),
            failure_label="gh api contents PUT failed for final-audit claim",
        )
        self.writes += 1
        return sha

    def _endpoint(self, issue_number: int) -> str:
        return f"repos/{self.repository}/contents/{claim_contents_path(issue_number)}"

    def _get(self, issue_number: int) -> dict[str, Any] | None:
        completed = self._runner(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"{self._endpoint(issue_number)}?ref={self.branch}",
            ],
            self._cwd,
        )
        detail = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if completed.returncode != 0:
            if "404" in detail or "Not Found" in detail:
                return None
            raise ValidationError((detail or "contents GET failed").strip()[:500])
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValidationError("contents GET returned non-object JSON")
        return payload

    def _put(self, issue_number: int, body: dict[str, Any]) -> Any:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            path = handle.name
        try:
            return self._runner(
                [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    "-H",
                    "Accept: application/vnd.github+json",
                    self._endpoint(issue_number),
                    "--input",
                    path,
                ],
                self._cwd,
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def _observe(self, issue_number: int, intended_raw: str) -> tuple[str, str | None]:
        payload = self._get(issue_number)
        if payload is None:
            return "absent", None
        encoded = str(payload.get("content") or "")
        blob = str(payload.get("sha") or "").strip()
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return "unknown", None
        if decoded == intended_raw and blob:
            return "match", blob
        if decoded == intended_raw:
            return "unverified", None
        return "differ", None


@dataclass(frozen=True)
class WorkPacketSnapshot:
    repository: str
    issue_number: int
    branch: str
    head: str
    status: str


def _spent_this_month(ledger: IssueAuditLedger | None, now: float) -> float:
    if ledger is None or ledger.month_id != month_id_for(now):
        return 0.0
    return float(ledger.month_spent_usd)


def _claim_is_live(claim: AuditClaim, now: float) -> bool:
    if claim.state != "claimed":
        return False
    if claim.lease_seconds <= 0:
        return False
    age = now - float(claim.claimed_at)
    return 0 <= age < float(claim.lease_seconds)


def _result(
    action: str,
    *,
    auditor_calls: int = 0,
    checkpoint_writes: int = 0,
    verdict: str | None = None,
    findings: str | None = None,
    telemetry: dict | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "auditor_calls": auditor_calls,
        "checkpoint_writes": checkpoint_writes,
        "verdict": verdict,
        "findings": findings,
        "telemetry": telemetry,
    }


def run_exact_head_audit(
    *,
    audit_requested: bool,
    repository: str,
    issue_number: int,
    branch: str,
    head: str,
    packets: list[WorkPacketSnapshot],
    worktree_path: str,
    store: ClaimStore,
    auditor: Any,
    evidence_bundle: dict | None = None,
    budget: AuditBudget | None = None,
    git_runner=None,
    list_sessions=None,
    list_processes=None,
    now: Callable[[], float] | None = None,
    api_key_env: str = "OPENAI_API_KEY",
    lease_seconds: float = SLICE_CLAIM_LEASE_SECONDS,
    event: CompletionEvent | None = None,
    record: WorkstreamRecord | None = None,
) -> dict[str, Any]:
    """One run-once pass. Idle and refused passes do not call the auditor."""
    clock = now or time.time
    if not audit_requested:
        return _result("idle_noop")
    repo = normalize_github_repository(repository)
    target = require_exact_commit_sha(head, label="head")
    branch_name = str(branch or "").strip()
    if not branch_name:
        raise ValidationError("exact-head audit requires a branch")
    if len(packets) != 1:
        return _result("ambiguous_packet")
    packet = packets[0]
    packet_repo = normalize_github_repository(packet.repository)
    packet_head = require_exact_commit_sha(packet.head, label="packet.head")
    if (
        packet_repo != repo
        or int(packet.issue_number) != int(issue_number)
        or packet.branch != branch_name
        or packet_head != target
        or packet.status != "ACTIVE"
    ):
        return _result("stale_head")
    if persistent_cursor_active(
        worktree_path,
        list_sessions=list_sessions,
        list_processes=list_processes,
    ):
        return _result("cursor_active_noop")
    try:
        validate_clean_worktree_identity(
            worktree_path,
            repository=repo,
            branch=branch_name,
            expected_head=target,
            git_runner=git_runner,
        )
    except ValidationError as exc:
        return _result("identity_refused", findings=_redact_findings(str(exc)))

    key = make_audit_claim_key(repo, issue_number, target)
    ledger, blob_sha = store.load(issue_number)
    writes_before = store.writes
    if ledger is not None and (
        ledger.repository != repo or int(ledger.issue_number) != int(issue_number)
    ):
        raise ValidationError("audit ledger identity does not match the request")
    existing = ledger.claims.get(key) if ledger is not None else None
    if existing is not None and existing.state == "completed":
        return _result(
            "duplicate_completed",
            verdict=existing.verdict,
            findings=existing.findings,
            telemetry=existing.telemetry,
            checkpoint_writes=store.writes - writes_before,
        )
    if existing is not None and existing.state == "claimed":
        if _claim_is_live(existing, clock()):
            return _result("duplicate_claim", checkpoint_writes=store.writes - writes_before)
        reconciled = AuditClaim(
            repository=repo,
            issue_number=issue_number,
            branch=branch_name,
            target_sha=target,
            claim_key=key,
            state="completed",
            month_id=existing.month_id,
            claimed_at=existing.claimed_at,
            lease_seconds=existing.lease_seconds,
            verdict="HUMAN_REQUIRED",
            findings=_redact_findings(
                "interrupted exact-head audit reconciled without a second paid call"
            ),
            telemetry=_sanitize_telemetry(
                {"verdict": "HUMAN_REQUIRED", "duration_sec": 0.0},
                target_sha=target,
            ),
        )
        assert ledger is not None
        ledger.claims[key] = reconciled
        store.save(ledger, expected_sha=blob_sha)
        return _result(
            "reconciled",
            verdict="HUMAN_REQUIRED",
            findings=reconciled.findings,
            telemetry=reconciled.telemetry,
            checkpoint_writes=store.writes - writes_before,
        )

    if evidence_bundle is None:
        raise ValidationError("exact-head audit requires an evidence bundle")
    blocked_evidence = incomplete_evidence_verdict(evidence_bundle)
    if blocked_evidence is None:
        blocked_evidence = _deterministic_gate_before_codex(evidence_bundle)
    if blocked_evidence is not None:
        return _result(
            "evidence_blocked",
            verdict=blocked_evidence.verdict,
            findings=_redact_findings(blocked_evidence.findings),
        )

    spent = _spent_this_month(ledger, clock())
    active_budget = budget or AuditBudget(per_run_hard_usd=1.0, monthly_hard_usd=25.0)
    active_budget = AuditBudget(
        per_run_hard_usd=active_budget.per_run_hard_usd,
        monthly_hard_usd=active_budget.monthly_hard_usd,
        month_spent_usd=spent,
        preflight_usd=active_budget.preflight_usd,
        per_run_soft_usd=active_budget.per_run_soft_usd,
        monthly_soft_usd=active_budget.monthly_soft_usd,
    )
    request = build_final_audit_request(evidence_bundle)
    ceiling = request_cost_ceiling_usd(request)
    budget_block = active_budget.blocked_before_call(request_ceiling_usd=ceiling)
    if budget_block:
        return _result("budget_blocked", findings=budget_block)
    if not os.environ.get(api_key_env, "").strip():
        return _result(
            "missing_key",
            verdict="HUMAN_REQUIRED",
            findings="OPENAI_API_KEY absent; Gate B real audit is HUMAN_REQUIRED",
        )

    current_month = month_id_for(clock())
    if ledger is None:
        ledger = IssueAuditLedger(
            repository=repo,
            issue_number=issue_number,
            month_id=current_month,
            month_spent_usd=0.0,
        )
    elif ledger.month_id != current_month:
        ledger.month_id = current_month
        ledger.month_spent_usd = 0.0
    claimed_at = clock()
    ledger.claims[key] = AuditClaim(
        repository=repo,
        issue_number=issue_number,
        branch=branch_name,
        target_sha=target,
        claim_key=key,
        state="claimed",
        month_id=ledger.month_id,
        claimed_at=claimed_at,
        lease_seconds=lease_seconds,
    )
    blob_sha = store.save(ledger, expected_sha=blob_sha)
    if hasattr(auditor, "budget"):
        auditor.budget = active_budget
    started = clock()
    result = auditor.audit_bundle(evidence_bundle, event=event, record=record)
    elapsed = max(0.0, clock() - started)
    telemetry_raw: dict[str, Any] = {
        "verdict": result.verdict,
        "duration_sec": elapsed,
        "target_sha": target,
    }
    last = getattr(auditor, "last_telemetry", None)
    if last is not None:
        telemetry_raw.update(last.public_record())
        telemetry_raw["duration_sec"] = float(telemetry_raw.get("duration_sec") or elapsed)
        telemetry_raw["verdict"] = result.verdict
    cost = float(telemetry_raw.get("estimated_cost_usd") or 0.0)
    over = active_budget.reconcile(cost)
    verdict = result.verdict
    findings = result.findings
    if over:
        verdict = "HUMAN_REQUIRED"
        findings = f"{over}. {findings}"
    telemetry_raw["verdict"] = verdict
    telemetry_raw["estimated_cost_usd"] = cost
    completed = AuditClaim(
        repository=repo,
        issue_number=issue_number,
        branch=branch_name,
        target_sha=target,
        claim_key=key,
        state="completed",
        month_id=ledger.month_id,
        claimed_at=claimed_at,
        lease_seconds=lease_seconds,
        verdict=verdict,
        findings=_redact_findings(findings),
        telemetry=_sanitize_telemetry(telemetry_raw, target_sha=target),
    )
    ledger.claims[key] = completed
    ledger.month_spent_usd = active_budget.month_spent_usd
    store.save(ledger, expected_sha=blob_sha)
    if hasattr(auditor, "calls"):
        called = int(auditor.calls)
    else:
        called = 1 if getattr(auditor, "last_request_body", None) is not None else 0
    return _result(
        "audited",
        auditor_calls=called,
        checkpoint_writes=store.writes - writes_before,
        verdict=verdict,
        findings=completed.findings,
        telemetry=completed.telemetry,
    )
