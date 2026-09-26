"""Exact-HEAD final-audit claim ledger (Issue #47 slice C).

One GitHub Contents document per Work Packet remembers completed and live
claims. A duplicate or stale claim does not call the auditor. Monthly spend
lives on a repository-month document and survives a new worker process.
REWORK redispatch lives in ``atlas.audit_disposition`` and is not done here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
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


def budget_contents_path(month_id: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", month_id or ""):
        raise ValidationError("budget month_id must be YYYY-MM")
    return f".atlas/final-audit/budgets/month-{month_id}.json"


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


_DISPOSITION_VERDICT = {
    "redispatched": "REWORK",
    "dispatch_blocked": "HUMAN_REQUIRED",
    "pass_checkpoint": "PASS",
    "human_required": "HUMAN_REQUIRED",
    "dispatch_started": "REWORK",
}
_MAX_DISPOSITION_FINDINGS = 4000


def validate_stored_disposition(key: str, value: dict[str, Any]) -> dict[str, Any]:
    """Reject a durable disposition that cannot prove its own identity."""
    action = str(value.get("action") or "")
    expected_verdict = _DISPOSITION_VERDICT.get(action)
    if expected_verdict is None:
        raise ValidationError("unsupported audit disposition action")
    verdict = str(value.get("verdict") or "")
    if verdict != expected_verdict:
        raise ValidationError("audit disposition verdict does not match its action")
    target_sha = require_exact_commit_sha(
        str(value.get("target_sha") or ""), label="disposition.target_sha"
    )
    repository = normalize_github_repository(str(value.get("repository") or ""))
    try:
        issue_number = int(value.get("issue_number"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("audit disposition issue_number is invalid") from exc
    if make_audit_claim_key(repository, issue_number, target_sha) != str(key):
        raise ValidationError("audit disposition identity does not match its key")
    attempt = value.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 100:
        raise ValidationError("audit disposition attempt is invalid")
    findings = value.get("findings")
    if not isinstance(findings, str) or len(findings) > _MAX_DISPOSITION_FINDINGS:
        raise ValidationError("audit disposition findings are unbounded")
    if _contains_unsafe_secret(findings):
        raise ValidationError("audit disposition findings look like secrets")
    return {
        "action": action,
        "verdict": verdict,
        "target_sha": target_sha,
        "repository": repository,
        "issue_number": issue_number,
        "attempt": attempt,
        "findings": findings,
    }


@dataclass
class IssueAuditLedger:
    repository: str
    issue_number: int
    month_id: str
    month_spent_usd: float = 0.0
    claims: dict[str, AuditClaim] = field(default_factory=dict)
    dispositions: dict[str, dict[str, Any]] = field(default_factory=dict)
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
            "dispositions": {
                key: dict(value) for key, value in sorted(self.dispositions.items())
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
        dispositions_raw = raw.get("dispositions") or {}
        if not isinstance(dispositions_raw, dict):
            raise ValidationError("audit dispositions must be an object")
        dispositions: dict[str, dict[str, Any]] = {}
        for key, value in dispositions_raw.items():
            if not isinstance(value, dict):
                raise ValidationError("audit disposition entry must be an object")
            dispositions[str(key)] = validate_stored_disposition(str(key), value)
        return cls(
            repository=normalize_github_repository(str(raw["repository"])),
            issue_number=int(raw["issue_number"]),
            month_id=str(raw.get("month_id") or ""),
            month_spent_usd=float(raw.get("month_spent_usd") or 0.0),
            claims=claims,
            dispositions=dispositions,
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

    def load_month_spend(self, month_id: str) -> tuple[float, str | None]: ...

    def load_month_budget(self, month_id: str) -> tuple[dict[str, Any], str | None]: ...

    def save_month_spend(
        self,
        month_id: str,
        spent_usd: float,
        *,
        repository: str,
        expected_sha: str | None,
    ) -> str: ...

    def save_month_budget(
        self,
        month_id: str,
        *,
        repository: str,
        spent_usd: float,
        reservations: dict[str, Any],
        expected_sha: str | None,
    ) -> str: ...


class MemoryClaimStore:
    """CAS stand-in for the GitHub Contents ledger. Tests never touch the network."""

    def __init__(self) -> None:
        self._docs: dict[int, tuple[str, str]] = {}
        self._budgets: dict[str, tuple[str, str]] = {}
        self._budget_lock = threading.Lock()
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

    def load_month_spend(self, month_id: str) -> tuple[float, str | None]:
        budget, sha = self.load_month_budget(month_id)
        return float(budget["month_spent_usd"]), sha

    def load_month_budget(self, month_id: str) -> tuple[dict[str, Any], str | None]:
        with self._budget_lock:
            found = self._budgets.get(month_id)
            if found is None:
                return {"month_spent_usd": 0.0, "reservations": {}}, None
            raw, sha = found
            payload = json.loads(raw)
            return {
                "month_spent_usd": float(payload.get("month_spent_usd") or 0.0),
                "reservations": dict(payload.get("reservations") or {}),
            }, sha

    def save_month_spend(
        self,
        month_id: str,
        spent_usd: float,
        *,
        repository: str,
        expected_sha: str | None,
    ) -> str:
        current, _sha = self.load_month_budget(month_id)
        return self.save_month_budget(
            month_id,
            repository=repository,
            spent_usd=spent_usd,
            reservations=dict(current.get("reservations") or {}),
            expected_sha=expected_sha,
        )

    def save_month_budget(
        self,
        month_id: str,
        *,
        repository: str,
        spent_usd: float,
        reservations: dict[str, Any],
        expected_sha: str | None,
    ) -> str:
        with self._budget_lock:
            current = self._budgets.get(month_id)
            current_sha = current[1] if current else None
            if current_sha != expected_sha:
                raise CheckpointCasConflict(
                    "final-audit budget compare-and-set failed: stale budget sha"
                )
            raw = _budget_json(
                repository=repository,
                month_id=month_id,
                spent_usd=spent_usd,
                reservations=reservations,
            )
            sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            self._budgets[month_id] = (raw, sha)
            self.writes += 1
            return sha


def _budget_json(
    *,
    repository: str,
    month_id: str,
    spent_usd: float,
    reservations: dict[str, Any] | None = None,
) -> str:
    payload = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "repository": normalize_github_repository(repository),
        "month_id": month_id,
        "month_spent_usd": float(spent_usd),
        "reservations": reservations or {},
    }
    raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if _contains_unsafe_secret(raw):
        raise ValidationError("audit budget contained credential-like material")
    assert_checkpoint_inline_size(raw)
    return raw


def _decode_contents(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = str(payload.get("content") or "")
    if not encoded:
        raise ValidationError("contents payload missing content")
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        raw = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("contents JSON is invalid") from exc
    if not isinstance(raw, dict):
        raise ValidationError("contents JSON must be an object")
    return raw


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

    def _run(self, argv: list[str]) -> Any:
        return self._runner(argv, self._cwd)

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

    def ensure_claim_branch(self) -> dict[str, Any]:
        """Create the claim-state branch once. Never write the default branch.

        Absent, existing, and concurrent-create results are explicit. A failed
        create stays failed; contents are not redirected onto another branch.
        """
        meta = self._gh_json(["repos/" + self.repository], "repo metadata")
        default_branch = str(meta.get("default_branch") or "").strip()
        if not default_branch:
            raise ValidationError("repository default_branch is required")
        if self.branch == default_branch:
            raise ValidationError(
                "final-audit claim branch must not be the repository default branch"
            )
        ref_payload = self._gh_json(
            [f"repos/{self.repository}/git/ref/heads/{default_branch}"],
            "default branch ref",
        )
        obj = ref_payload.get("object") if isinstance(ref_payload, dict) else None
        base_sha = str((obj or {}).get("sha") or "").strip()
        if not base_sha:
            raise ValidationError("default branch SHA missing")
        probe = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}/git/ref/heads/{self.branch}",
            ]
        )
        if probe.returncode == 0:
            return {"action": "exists", "branch": self.branch, "base_sha": base_sha}
        created = self._post_json(
            f"repos/{self.repository}/git/refs",
            {"ref": f"refs/heads/{self.branch}", "sha": base_sha},
        )
        if created.returncode != 0:
            detail = f"{created.stderr or ''}\n{created.stdout or ''}"
            already_exists = (
                "Reference already exists" in detail
                or '"message":"Reference already exists"' in detail
                or ("already exists" in detail.lower() and "422" in detail)
            )
            if already_exists:
                verify = self._run(
                    [
                        "gh",
                        "api",
                        "-H",
                        "Accept: application/vnd.github+json",
                        f"repos/{self.repository}/git/ref/heads/{self.branch}",
                    ]
                )
                if verify.returncode == 0:
                    return {
                        "action": "exists_race",
                        "branch": self.branch,
                        "base_sha": base_sha,
                    }
            raise ValidationError(
                (created.stderr or created.stdout or "").strip()[:500]
                or "failed to bootstrap final-audit claim branch"
            )
        return {"action": "created", "branch": self.branch, "base_sha": base_sha}

    def load_month_spend(self, month_id: str) -> tuple[float, str | None]:
        budget, sha = self.load_month_budget(month_id)
        return float(budget["month_spent_usd"]), sha

    def load_month_budget(self, month_id: str) -> tuple[dict[str, Any], str | None]:
        payload = self._get_path(budget_contents_path(month_id))
        if payload is None:
            return {"month_spent_usd": 0.0, "reservations": {}}, None
        decoded = _decode_contents(payload)
        if str(decoded.get("month_id") or "") != month_id:
            raise ValidationError("budget document month_id mismatch")
        blob = str(payload.get("sha") or "").strip()
        if not blob:
            raise ValidationError("budget contents response missing blob sha")
        reservations = decoded.get("reservations") or {}
        if not isinstance(reservations, dict):
            raise ValidationError("budget reservations must be an object")
        return {
            "month_spent_usd": float(decoded.get("month_spent_usd") or 0.0),
            "reservations": reservations,
        }, blob

    def save_month_spend(
        self,
        month_id: str,
        spent_usd: float,
        *,
        repository: str,
        expected_sha: str | None,
    ) -> str:
        current, _sha = self.load_month_budget(month_id)
        return self.save_month_budget(
            month_id,
            repository=repository,
            spent_usd=spent_usd,
            reservations=dict(current.get("reservations") or {}),
            expected_sha=expected_sha,
        )

    def save_month_budget(
        self,
        month_id: str,
        *,
        repository: str,
        spent_usd: float,
        reservations: dict[str, Any],
        expected_sha: str | None,
    ) -> str:
        self.ensure_claim_branch()
        raw = _budget_json(
            repository=repository,
            month_id=month_id,
            spent_usd=spent_usd,
            reservations=reservations,
        )
        sha = self._put_raw(
            budget_contents_path(month_id),
            raw,
            message=f"atlas final-audit budget {month_id}",
            expected_sha=expected_sha,
        )
        self.writes += 1
        return sha

    def save(self, ledger: IssueAuditLedger, *, expected_sha: str | None) -> str:
        self.ensure_claim_branch()
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
        if body["branch"] != self.branch or body["branch"] == "":
            raise ValidationError("refusing to write claim contents off the claim branch")
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

    def _gh_json(self, endpoint: list[str], label: str) -> dict[str, Any]:
        completed = self._run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", *endpoint],
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(detail[:500] or f"gh api failed for {label}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{label} returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError(f"{label} returned non-object JSON")
        return payload

    def _post_json(self, endpoint: str, body: dict[str, Any]) -> Any:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            path = handle.name
        try:
            return self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    "-H",
                    "Accept: application/vnd.github+json",
                    endpoint,
                    "--input",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def _get_path(self, contents_path: str) -> dict[str, Any] | None:
        completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}/contents/{contents_path}?ref={self.branch}",
            ],
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

    def _put_raw(
        self,
        contents_path: str,
        raw: str,
        *,
        message: str,
        expected_sha: str | None,
    ) -> str:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if expected_sha:
            body["sha"] = expected_sha
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"repos/{self.repository}/contents/{contents_path}",
                    "--input",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        return _sha_from_contents_put(
            completed=completed,
            intended_raw=raw,
            observe=lambda: self._observe_path(contents_path, raw),
            conflict_message=(
                "final-audit budget compare-and-set failed: stale contents sha"
            ),
            failure_label="gh api contents PUT failed for final-audit budget",
        )

    def _observe_path(self, contents_path: str, intended_raw: str) -> tuple[str, str | None]:
        payload = self._get_path(contents_path)
        if payload is None:
            return "absent", None
        try:
            decoded = json.dumps(_decode_contents(payload), sort_keys=True)
        except ValidationError:
            return "unknown", None
        blob = str(payload.get("sha") or "").strip()
        intended = json.dumps(json.loads(intended_raw), sort_keys=True)
        if decoded == intended and blob:
            return "match", blob
        return "differ", None

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


def _claim_is_live(claim: AuditClaim, now: float) -> bool:
    if claim.state != "claimed":
        return False
    if claim.lease_seconds <= 0:
        return False
    age = now - float(claim.claimed_at)
    return 0 <= age < float(claim.lease_seconds)


def _month_budget(store: ClaimStore, month_id: str) -> tuple[dict[str, Any], str | None]:
    budget, sha = store.load_month_budget(month_id)
    reservations = budget.get("reservations") or {}
    if not isinstance(reservations, dict):
        raise ValidationError("budget reservations must be an object")
    return {
        "month_spent_usd": float(budget.get("month_spent_usd") or 0.0),
        "reservations": dict(reservations),
    }, sha


def _budget_for(snapshot: AuditBudget, spent: float) -> AuditBudget:
    return AuditBudget(
        per_run_hard_usd=snapshot.per_run_hard_usd,
        monthly_hard_usd=snapshot.monthly_hard_usd,
        month_spent_usd=spent,
        preflight_usd=snapshot.preflight_usd,
        per_run_soft_usd=snapshot.per_run_soft_usd,
        monthly_soft_usd=snapshot.monthly_soft_usd,
    )


def _reserve_ceiling(
    store: ClaimStore,
    *,
    month_id: str,
    repository: str,
    claim_key: str,
    issue_number: int,
    target_sha: str,
    ceiling: float,
    limits: AuditBudget,
) -> tuple[str, dict[str, Any]]:
    """CAS-reserve the request ceiling before a paid call.

    A lost compare-and-set reloads once. A reservation already bound to this
    claim is not charged again.
    """
    last_block = "monthly budget exceeded"
    for _attempt in range(2):
        budget, sha = _month_budget(store, month_id)
        reservations = dict(budget["reservations"])
        existing = reservations.get(claim_key)
        if isinstance(existing, dict):
            return "already", budget
        spent = float(budget["month_spent_usd"])
        blocked = _budget_for(limits, spent).blocked_before_call(
            request_ceiling_usd=ceiling
        )
        if blocked:
            return "blocked", {"findings": blocked, **budget}
        reservations[claim_key] = {
            "issue_number": int(issue_number),
            "target_sha": target_sha,
            "ceiling_usd": float(ceiling),
            "accounted_usd": float(ceiling),
            "state": "reserved",
        }
        try:
            store.save_month_budget(
                month_id,
                repository=repository,
                spent_usd=spent + float(ceiling),
                reservations=reservations,
                expected_sha=sha,
            )
            return "reserved", _month_budget(store, month_id)[0]
        except CheckpointCasConflict:
            last_block = "monthly budget reservation lost compare-and-set"
            continue
    return "lost", {"findings": last_block}


def _settle_reservation(
    store: ClaimStore,
    *,
    month_id: str,
    repository: str,
    claim_key: str,
    actual_usd: float,
) -> float:
    """Move one reservation from its ceiling to the actual cost, exactly once."""
    for _attempt in range(2):
        budget, sha = _month_budget(store, month_id)
        reservations = dict(budget["reservations"])
        slot = reservations.get(claim_key)
        if not isinstance(slot, dict):
            raise ValidationError("missing monthly reservation for exact-head claim")
        if str(slot.get("state") or "") == "settled":
            return float(budget["month_spent_usd"])
        accounted = float(slot.get("accounted_usd") or 0.0)
        spent = float(budget["month_spent_usd"])
        updated = dict(slot)
        updated["accounted_usd"] = float(actual_usd)
        updated["state"] = "settled"
        reservations[claim_key] = updated
        try:
            store.save_month_budget(
                month_id,
                repository=repository,
                spent_usd=spent + float(actual_usd) - accounted,
                reservations=reservations,
                expected_sha=sha,
            )
            return spent + float(actual_usd) - accounted
        except CheckpointCasConflict:
            continue
    raise CheckpointCasConflict(
        "final-audit budget compare-and-set failed while settling reservation"
    )


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
    interrupted_claim: AuditClaim | None = None
    if existing is not None and existing.state == "claimed":
        if _claim_is_live(existing, clock()):
            return _result("duplicate_claim", checkpoint_writes=store.writes - writes_before)
        interrupted_claim = existing

    if evidence_bundle is not None:
        blocked_evidence = incomplete_evidence_verdict(evidence_bundle)
        if blocked_evidence is None:
            blocked_evidence = _deterministic_gate_before_codex(evidence_bundle)
        if blocked_evidence is not None:
            return _result(
                "evidence_blocked",
                verdict=blocked_evidence.verdict,
                findings=_redact_findings(blocked_evidence.findings),
            )

    current_month = month_id_for(clock())

    def _finish_without_call(
        *,
        action: str,
        verdict: str,
        findings: str,
        actual_usd: float,
        claimed_at: float,
    ) -> dict[str, Any]:
        _settle_reservation(
            store,
            month_id=current_month,
            repository=repo,
            claim_key=key,
            actual_usd=actual_usd,
        )
        nonlocal ledger, blob_sha
        if ledger is None:
            ledger = IssueAuditLedger(
                repository=repo,
                issue_number=issue_number,
                month_id=current_month,
            )
        telemetry = _sanitize_telemetry(
            {
                "verdict": verdict,
                "estimated_cost_usd": actual_usd,
                "target_sha": target,
                "duration_sec": 0.0,
            },
            target_sha=target,
        )
        ledger.claims[key] = AuditClaim(
            repository=repo,
            issue_number=issue_number,
            branch=branch_name,
            target_sha=target,
            claim_key=key,
            state="completed",
            month_id=current_month,
            claimed_at=claimed_at,
            lease_seconds=lease_seconds,
            verdict=verdict,
            findings=_redact_findings(findings),
            telemetry=telemetry,
        )
        store.save(ledger, expected_sha=blob_sha)
        return _result(
            action,
            verdict=verdict,
            findings=ledger.claims[key].findings,
            telemetry=telemetry,
            checkpoint_writes=store.writes - writes_before,
        )

    held = _month_budget(store, current_month)[0]["reservations"].get(key)
    if isinstance(held, dict):
        claimed_at = existing.claimed_at if existing is not None else clock()
        recorded = existing.telemetry if existing is not None else None
        if str(held.get("state") or "") == "settled":
            actual = float(held.get("accounted_usd") or 0.0)
            verdict = existing.verdict if existing and existing.verdict else "HUMAN_REQUIRED"
            return _finish_without_call(
                action="reservation_settled",
                verdict=verdict,
                findings=(existing.findings if existing else "")
                or "reserved audit usage already settled",
                actual_usd=actual,
                claimed_at=claimed_at,
            )
        if isinstance(recorded, dict) and "estimated_cost_usd" in recorded:
            return _finish_without_call(
                action="usage_reconciled",
                verdict=str(recorded.get("verdict") or "HUMAN_REQUIRED"),
                findings=(existing.findings if existing else "")
                or "reconciled reserved audit usage without a second paid call",
                actual_usd=float(recorded.get("estimated_cost_usd") or 0.0),
                claimed_at=claimed_at,
            )
        return _finish_without_call(
            action="reservation_held",
            verdict="HUMAN_REQUIRED",
            findings=(
                "interrupted exact-head audit kept its monthly reservation "
                "and made no second paid call"
            ),
            actual_usd=float(held.get("accounted_usd") or held.get("ceiling_usd") or 0.0),
            claimed_at=claimed_at,
        )

    if not isinstance(held, dict) and interrupted_claim is not None:
        assert ledger is not None
        reconciled = AuditClaim(
            repository=repo,
            issue_number=issue_number,
            branch=branch_name,
            target_sha=target,
            claim_key=key,
            state="completed",
            month_id=interrupted_claim.month_id,
            claimed_at=interrupted_claim.claimed_at,
            lease_seconds=interrupted_claim.lease_seconds,
            verdict="HUMAN_REQUIRED",
            findings=_redact_findings(
                "interrupted exact-head audit reconciled without a second paid call"
            ),
            telemetry=_sanitize_telemetry(
                {"verdict": "HUMAN_REQUIRED", "duration_sec": 0.0, "target_sha": target},
                target_sha=target,
            ),
        )
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
        if not os.environ.get(api_key_env, "").strip():
            return _result(
                "missing_key",
                verdict="HUMAN_REQUIRED",
                findings="OPENAI_API_KEY absent; Gate B real audit is HUMAN_REQUIRED",
            )
        raise ValidationError("exact-head audit requires an evidence bundle")
    limits = budget or AuditBudget(per_run_hard_usd=1.0, monthly_hard_usd=25.0)
    request = build_final_audit_request(evidence_bundle)
    ceiling = request_cost_ceiling_usd(request)
    if not os.environ.get(api_key_env, "").strip():
        return _result(
            "missing_key",
            verdict="HUMAN_REQUIRED",
            findings="OPENAI_API_KEY absent; Gate B real audit is HUMAN_REQUIRED",
        )
    reserved, reservation_state = _reserve_ceiling(
        store,
        month_id=current_month,
        repository=repo,
        claim_key=key,
        issue_number=issue_number,
        target_sha=target,
        ceiling=ceiling,
        limits=limits,
    )
    if reserved != "reserved":
        return _result(
            "budget_blocked",
            findings=str(reservation_state.get("findings") or "monthly budget exceeded"),
            checkpoint_writes=store.writes - writes_before,
        )

    pre_reserve_spent = float(reservation_state["month_spent_usd"]) - ceiling
    active_budget = _budget_for(limits, pre_reserve_spent)
    if ledger is None:
        ledger = IssueAuditLedger(
            repository=repo,
            issue_number=issue_number,
            month_id=current_month,
        )
    elif ledger.month_id != current_month:
        ledger.month_id = current_month
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
    provider_owns_budget = hasattr(auditor, "budget")
    if provider_owns_budget:
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
    verdict = result.verdict
    findings = result.findings
    if not provider_owns_budget:
        over = active_budget.reconcile(cost)
        if over:
            verdict = "HUMAN_REQUIRED"
            findings = f"{over}. {findings}"
    telemetry_raw["verdict"] = verdict
    telemetry_raw["estimated_cost_usd"] = cost
    usage_telemetry = _sanitize_telemetry(telemetry_raw, target_sha=target)
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
        verdict=verdict,
        findings=_redact_findings(findings),
        telemetry=usage_telemetry,
    )
    blob_sha = store.save(ledger, expected_sha=blob_sha)
    try:
        _settle_reservation(
            store,
            month_id=current_month,
            repository=repo,
            claim_key=key,
            actual_usd=cost,
        )
    except CheckpointCasConflict:
        if hasattr(auditor, "calls"):
            called = int(auditor.calls)
        else:
            called = 1 if getattr(auditor, "last_request_body", None) is not None else 0
        return _result(
            "budget_settle_pending",
            auditor_calls=called,
            checkpoint_writes=store.writes - writes_before,
            verdict=verdict,
            findings=_redact_findings(findings),
            telemetry=usage_telemetry,
        )
    ledger.claims[key] = AuditClaim(
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
        telemetry=usage_telemetry,
    )
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
        findings=ledger.claims[key].findings,
        telemetry=usage_telemetry,
    )
