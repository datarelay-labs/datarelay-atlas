"""Continuous Chat Audit & Session Supervisor PoC v0 (ADR-0007).

Durable Audit Control Packet + bounded delta-first slices + optional rollover.
Chat conversations are execution instances; checkpoints are canonical.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from atlas.provenance import ValidationError
from atlas.secrets import sanitize_durable_text
from atlas.work_controller import (
    GitRunner,
    WorktreeIdentity,
    default_git_runner,
    heads_match,
    normalize_github_repository,
)

CHAT_AUDIT_SCHEMA_VERSION = 1
STORE_FILENAME = "chat-audit.json"
HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")
RESUME_COMMAND = "/chat-audit-resume"
SLICE_CLAIM_LEASE_SECONDS = 900


class CheckpointCasConflict(ValidationError):
    """Stale canonical checkpoint write refused (compare-and-set)."""

DEFAULT_AUDIT_UNITS = (
    "changed_code",
    "affected_contracts",
    "affected_tests_ci",
    "security_impact",
    "docs_spec_drift",
)
FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FINDING_SEVERITIES = frozenset({"P0", "P1", "P2", "P3", "INFO"})
ALLOWED_MODES = frozenset({"delta", "full"})
CLAIM_STATES = frozenset(
    {"executing", "timed_out", "awaiting_evidence", "failed"}
)


def build_audit_queue(*, include_release_readiness: bool = False) -> list[str]:
    queue = list(DEFAULT_AUDIT_UNITS)
    if include_release_readiness:
        queue.append("release_readiness")
    return queue


AUDIT_STATUSES = frozenset(
    {
        "IDLE",
        "IN_SLICE",
        "AWAITING_EVIDENCE",
        "SLICE_COMPLETE",
        "PASSED",
        "FINDINGS",
        "FAILED_CLOSED",
    }
)
SESSION_STATES = frozenset(
    {
        "ACTIVE",
        "STALLED",
        "TIMEOUT",
        "ROLLOVER_REQUIRED",
        "RESUMED",
    }
)
EVIDENCE_STATUSES = frozenset({"COMPLETE", "TRUNCATED", "MISSING"})


@dataclass
class AuditFinding:
    finding_id: str
    unit: str
    summary: str
    severity: str = "P2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditFinding":
        if not isinstance(raw, dict):
            raise ValidationError("finding must be an object")
        finding_id = str(raw.get("finding_id", "")).strip()
        if not FINDING_ID_RE.match(finding_id):
            raise ValidationError(
                "finding_id must be a bounded [A-Za-z0-9._:-] identifier"
            )
        from atlas.secrets import contains_unsafe_secret

        if contains_unsafe_secret(finding_id):
            raise ValidationError(
                "finding_id must not contain credential-like material"
            )
        severity = str(raw.get("severity", "P2")).strip().upper()
        if severity not in FINDING_SEVERITIES:
            raise ValidationError(
                f"unsupported finding severity: {severity!r}"
            )
        unit = str(raw.get("unit", "")).strip()
        if unit not in DEFAULT_AUDIT_UNITS and unit != "release_readiness":
            raise ValidationError(f"unsupported finding unit: {unit!r}")
        return cls(
            finding_id=finding_id,
            unit=unit,
            summary=str(raw.get("summary", "")),
            severity=severity,
        )


@dataclass
class AuditEvidence:
    status: str
    unit: str
    target_sha: str
    notes: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEvidence":
        if not isinstance(raw, dict):
            raise ValidationError("evidence must be an object")
        return cls(
            status=str(raw["status"]),
            unit=str(raw["unit"]),
            target_sha=str(raw["target_sha"]),
            notes=str(raw.get("notes", "")),
            truncated=bool(raw.get("truncated", False)),
        )

    def is_passable(self) -> bool:
        return (
            self.status == "COMPLETE"
            and not self.truncated
            and bool(self.notes.strip())
        )


@dataclass
class SliceResult:
    unit: str
    target_sha: str
    outcome: str  # PASS | FINDING | TIMEOUT | REJECTED
    findings: list[AuditFinding] = field(default_factory=list)
    evidence: AuditEvidence | None = None
    audit_request: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "target_sha": self.target_sha,
            "outcome": self.outcome,
            "findings": [f.to_dict() for f in self.findings],
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "audit_request": self.audit_request,
        }


@dataclass
class SessionState:
    state: str = "ACTIVE"
    last_resume_command: str = RESUME_COMMAND
    rollover_count: int = 0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SessionState":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValidationError("session must be an object")
        state = str(raw.get("state", "ACTIVE"))
        if state not in SESSION_STATES:
            raise ValidationError(f"unsupported session state: {state}")
        rollover_raw = raw.get("rollover_count", 0)
        if isinstance(rollover_raw, bool) or not isinstance(rollover_raw, int):
            raise ValidationError("session.rollover_count must be an integer")
        if rollover_raw < 0:
            raise ValidationError("session.rollover_count must be non-negative")
        return cls(
            state=state,
            last_resume_command=str(raw.get("last_resume_command", RESUME_COMMAND)),
            rollover_count=rollover_raw,
            notes=str(raw.get("notes", "")),
        )


@dataclass
class AuditControlPacket:
    target_repository: str
    target_branch: str
    current_target_sha: str
    last_audited_sha: str | None = None
    audit_status: str = "IDLE"
    audit_queue: list[str] = field(default_factory=list)
    current_unit: str | None = None
    current_unit_index: int = 0
    open_findings: list[AuditFinding] = field(default_factory=list)
    next_action: str = "initialize_or_run_next_slice"
    last_completed_slice: dict[str, Any] | None = None
    idempotency_run_key: str = ""
    mode: str = "delta"
    include_release_readiness: bool = False
    session: SessionState = field(default_factory=SessionState)
    schema_version: int = CHAT_AUDIT_SCHEMA_VERSION
    # Completed unit outcomes keyed for idempotent replay within a run.
    completed_units: dict[str, dict[str, Any]] = field(default_factory=dict)
    no_change_runs: int = 0
    slice_claim: dict[str, Any] | None = None
    # Monotonic observability counter (Contents blob SHA is the CAS token).
    canonical_revision: int = 0
    # Rotating start index so HEAD advances cannot starve later audit units.
    fair_start_index: int = 0
    # Last same-HEAD coordination refresh evidence (PR/CI/review/work-packet).
    last_coordination_refresh: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_repository": self.target_repository,
            "target_branch": self.target_branch,
            "last_audited_sha": self.last_audited_sha,
            "current_target_sha": self.current_target_sha,
            "audit_status": self.audit_status,
            "audit_queue": list(self.audit_queue),
            "current_unit": self.current_unit,
            "current_unit_index": self.current_unit_index,
            "open_findings": [f.to_dict() for f in self.open_findings],
            "next_action": self.next_action,
            "last_completed_slice": copy.deepcopy(self.last_completed_slice),
            "idempotency_run_key": self.idempotency_run_key,
            "mode": self.mode,
            "include_release_readiness": self.include_release_readiness,
            "session": self.session.to_dict(),
            "completed_units": copy.deepcopy(self.completed_units),
            "no_change_runs": self.no_change_runs,
            "slice_claim": copy.deepcopy(self.slice_claim),
            "canonical_revision": self.canonical_revision,
            "fair_start_index": self.fair_start_index,
            "last_coordination_refresh": copy.deepcopy(
                self.last_coordination_refresh
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditControlPacket":
        version = int(raw.get("schema_version", 0))
        if version != CHAT_AUDIT_SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported chat-audit schema_version: {version}"
            )
        status = str(raw.get("audit_status", "IDLE"))
        if status not in AUDIT_STATUSES:
            raise ValidationError(f"unsupported audit_status: {status}")
        if "audit_queue" not in raw:
            raise ValidationError("audit_queue is required")
        queue = [str(u) for u in raw["audit_queue"]]
        if not queue:
            raise ValidationError("audit_queue must be non-empty")
        include_release = bool(raw.get("include_release_readiness", False))
        expected_queue = build_audit_queue(
            include_release_readiness=include_release
        )
        if queue != expected_queue:
            raise ValidationError(
                "audit_queue must exactly match the required unit set/order "
                f"for include_release_readiness={include_release}"
            )
        run_key = str(raw.get("idempotency_run_key", "")).strip()
        if not run_key:
            raise ValidationError("idempotency_run_key is required")
        mode = str(raw.get("mode", "delta")).strip()
        if mode not in ALLOWED_MODES:
            raise ValidationError(f"unsupported mode: {mode!r}")
        target_repository = normalize_github_repository(
            str(raw["target_repository"])
        )
        target_branch = str(raw["target_branch"]).strip()
        if not target_branch:
            raise ValidationError("target_branch is required")
        current_target_sha = require_exact_commit_sha(
            str(raw["current_target_sha"]),
            label="current_target_sha",
        )
        last_audited_sha = None
        if raw.get("last_audited_sha"):
            last_audited_sha = require_exact_commit_sha(
                str(raw["last_audited_sha"]),
                label="last_audited_sha",
            )
        expected_run_key = make_run_key(
            target_repository, target_branch, current_target_sha
        )
        if run_key != expected_run_key:
            raise ValidationError(
                "idempotency_run_key must match repository+branch+current_target_sha"
            )
        current_unit = (
            str(raw["current_unit"]) if raw.get("current_unit") else None
        )
        if status == "IN_SLICE" and current_unit is None:
            raise ValidationError("IN_SLICE requires current_unit")
        if current_unit is not None and current_unit not in queue:
            raise ValidationError(
                f"current_unit {current_unit!r} is not in audit_queue"
            )
        current_unit_index = int(raw.get("current_unit_index", 0))
        if current_unit_index < 0 or current_unit_index >= len(queue):
            raise ValidationError("current_unit_index out of range")
        if current_unit is not None and queue[current_unit_index] != current_unit:
            raise ValidationError(
                "current_unit_index does not match current_unit"
            )
        no_change_runs = int(raw.get("no_change_runs", 0))
        if no_change_runs < 0:
            raise ValidationError("no_change_runs must be non-negative")
        canonical_revision = int(raw.get("canonical_revision", 0))
        if canonical_revision < 0:
            raise ValidationError("canonical_revision must be non-negative")
        fair_start_index = int(raw.get("fair_start_index", 0))
        if fair_start_index < 0:
            raise ValidationError("fair_start_index must be non-negative")
        last_coordination_refresh = copy.deepcopy(
            raw.get("last_coordination_refresh")
        )
        if last_coordination_refresh is not None:
            last_coordination_refresh = sanitize_coordination_snapshot(
                last_coordination_refresh
            )
        last_completed_slice = copy.deepcopy(raw.get("last_completed_slice"))
        if last_completed_slice is not None:
            last_completed_slice = sanitize_slice_dict(last_completed_slice)
        slice_claim = validate_slice_claim(
            copy.deepcopy(raw.get("slice_claim")),
            run_key=run_key,
            current_target_sha=current_target_sha,
            current_unit=current_unit,
            audit_status=status,
            audit_queue=queue,
        )
        open_findings_raw = raw.get("open_findings", [])
        if open_findings_raw is None:
            open_findings_raw = []
        if not isinstance(open_findings_raw, list):
            raise ValidationError("open_findings must be a list")
        findings = [
            AuditFinding.from_dict(item)
            for item in open_findings_raw
        ]
        completed_raw = raw.get("completed_units") or {}
        if not isinstance(completed_raw, dict):
            raise ValidationError("completed_units must be an object")
        # Canonicalize completed-unit keys to lowercase SHA form.
        canonical_completed: dict[str, Any] = {}
        for key, entry in completed_raw.items():
            run_k, unit_k, sha_k = parse_unit_key(str(key))
            canon_key = f"{run_k}:{unit_k}:{sha_k}"
            canonical_completed[canon_key] = entry
        completed_units = validate_completed_units_map(
            canonical_completed,
            expected_run_key=run_key,
            expected_target_sha=current_target_sha,
        )
        assert_audit_verdict_invariants(
            status=status,
            queue=queue,
            run_key=run_key,
            current_target_sha=current_target_sha,
            last_audited_sha=last_audited_sha,
            current_unit=current_unit,
            slice_claim=slice_claim,
            open_findings=findings,
            completed_units=completed_units,
        )
        return cls(
            target_repository=target_repository,
            target_branch=target_branch,
            current_target_sha=current_target_sha,
            last_audited_sha=last_audited_sha,
            audit_status=status,
            audit_queue=queue,
            current_unit=current_unit,
            current_unit_index=current_unit_index,
            open_findings=findings,
            next_action=str(raw.get("next_action", "")),
            last_completed_slice=last_completed_slice,
            idempotency_run_key=run_key,
            mode=mode,
            include_release_readiness=bool(
                raw.get("include_release_readiness", False)
            ),
            session=SessionState.from_dict(raw.get("session")),
            schema_version=version,
            completed_units=completed_units,
            no_change_runs=no_change_runs,
            slice_claim=slice_claim,
            canonical_revision=canonical_revision,
            fair_start_index=fair_start_index % len(queue),
            last_coordination_refresh=last_coordination_refresh,
        )


def _claim_is_genuinely_live(claim: dict[str, Any] | None) -> bool:
    """True only for an executing claim still inside its lease window."""
    if not isinstance(claim, dict):
        return False
    if str(claim.get("state") or "") != "executing":
        return False
    claimed_raw = claim.get("claimed_at")
    if isinstance(claimed_raw, bool) or not isinstance(claimed_raw, (int, float)):
        return False
    lease_raw = claim.get("lease_seconds", SLICE_CLAIM_LEASE_SECONDS)
    if isinstance(lease_raw, bool) or not isinstance(lease_raw, (int, float)):
        return False
    if float(lease_raw) <= 0:
        return False
    age = time.time() - float(claimed_raw)
    return 0 <= age < float(lease_raw)


def make_run_key(repository: str, branch: str, target_sha: str) -> str:
    material = f"{repository}|{branch}|{target_sha.lower()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


_ACTIVE_AUDIT_STATUSES = frozenset(
    {
        "IDLE",
        "IN_SLICE",
        "AWAITING_EVIDENCE",
        "SLICE_COMPLETE",
        "FAILED_CLOSED",
    }
)


def _completed_entries_for_queue(
    *,
    queue: list[str],
    run_key: str,
    current_target_sha: str,
    completed_units: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    entries: dict[str, dict[str, Any]] = {}
    for unit in queue:
        key = f"{run_key}:{unit}:{current_target_sha}"
        entry = completed_units.get(key)
        if not isinstance(entry, dict):
            return None
        entries[unit] = entry
    return entries


def assert_audit_verdict_invariants(
    *,
    status: str,
    queue: list[str],
    run_key: str,
    current_target_sha: str,
    last_audited_sha: str | None,
    current_unit: str | None,
    slice_claim: dict[str, Any] | None,
    open_findings: list[AuditFinding],
    completed_units: dict[str, dict[str, Any]],
) -> None:
    """Reject restored verdicts that are not evidence-derived."""
    audited_this_head = (
        last_audited_sha is not None and last_audited_sha == current_target_sha
    )
    entries = _completed_entries_for_queue(
        queue=queue,
        run_key=run_key,
        current_target_sha=current_target_sha,
        completed_units=completed_units,
    )
    if status in _ACTIVE_AUDIT_STATUSES and audited_this_head:
        raise ValidationError(
            f"{status} cannot claim last_audited_sha == current_target_sha"
        )
    if status == "AWAITING_EVIDENCE" and current_unit is None:
        raise ValidationError("AWAITING_EVIDENCE requires current_unit")
    if status == "SLICE_COMPLETE" and (
        current_unit is not None or slice_claim is not None
    ):
        raise ValidationError(
            "SLICE_COMPLETE cannot retain current_unit or slice_claim"
        )
    if status not in {"PASSED", "FINDINGS"}:
        return
    if current_unit is not None or slice_claim is not None:
        raise ValidationError(f"{status} cannot retain an active slice")
    if entries is None:
        raise ValidationError(
            f"{status} requires validated evidence for every audit unit "
            "at the current run/HEAD"
        )
    finding_units = [
        unit
        for unit, entry in entries.items()
        if str(entry.get("outcome")) == "FINDING"
    ]
    if status == "PASSED":
        if not audited_this_head:
            raise ValidationError(
                "PASSED requires last_audited_sha == current_target_sha"
            )
        if open_findings:
            raise ValidationError("PASSED cannot retain unresolved findings")
        if finding_units:
            raise ValidationError("PASSED requires every unit outcome PASS")
        return
    # FINDINGS: queue verdict needs finding evidence; same-HEAD coordination
    # attention may follow a real PASSED baseline (all units PASS).
    if audited_this_head:
        return
    if not finding_units:
        raise ValidationError(
            "FINDINGS requires finding evidence unless last_audited_sha "
            "equals current_target_sha"
        )
    if not open_findings:
        raise ValidationError(
            "FINDINGS requires unresolved open_findings derived from evidence"
        )


_CLAIM_REQUIRED_FIELDS = ("claim_id", "unit", "run_key", "target_sha", "state")
_CLAIM_FUTURE_SKEW_SECONDS = 60.0


def validate_slice_claim(
    slice_claim: Any,
    *,
    run_key: str,
    current_target_sha: str,
    current_unit: str | None,
    audit_status: str,
    audit_queue: list[str],
) -> dict[str, Any] | None:
    """Fail closed on incomplete/inconsistent restored slice claims."""
    if slice_claim is None:
        if audit_status == "IN_SLICE":
            raise ValidationError("IN_SLICE requires a complete slice_claim")
        return None
    if not isinstance(slice_claim, dict):
        raise ValidationError("slice_claim must be an object")
    claim_state = str(slice_claim.get("state", "")).strip()
    if claim_state not in CLAIM_STATES:
        raise ValidationError(f"unsupported slice_claim.state: {claim_state!r}")
    missing = [
        name
        for name in _CLAIM_REQUIRED_FIELDS
        if not str(slice_claim.get(name, "")).strip()
    ]
    if missing:
        raise ValidationError(
            "slice_claim missing required fields: " + ", ".join(missing)
        )
    claim_id = str(slice_claim.get("claim_id", "")).strip()
    if len(claim_id) > 128:
        raise ValidationError("slice_claim.claim_id exceeds max length")
    claim_unit = str(slice_claim.get("unit", "")).strip()
    if claim_unit not in audit_queue:
        raise ValidationError(
            f"slice_claim.unit {claim_unit!r} is not in audit_queue"
        )
    if current_unit is not None and claim_unit != current_unit:
        raise ValidationError("slice_claim.unit does not match current_unit")
    claim_run = str(slice_claim.get("run_key", "")).strip()
    if claim_run != run_key:
        raise ValidationError("slice_claim.run_key mismatch")
    claim_sha = require_exact_commit_sha(
        str(slice_claim.get("target_sha", "")),
        label="slice_claim.target_sha",
    )
    if claim_sha != current_target_sha:
        raise ValidationError("slice_claim.target_sha mismatch")

    out = copy.deepcopy(slice_claim)
    out["claim_id"] = claim_id
    out["unit"] = claim_unit
    out["run_key"] = claim_run
    out["target_sha"] = claim_sha
    out["state"] = claim_state

    if "lease_seconds" in out and out.get("lease_seconds") is not None:
        lease_raw = out.get("lease_seconds")
        if isinstance(lease_raw, bool) or not isinstance(lease_raw, (int, float)):
            raise ValidationError("slice_claim.lease_seconds must be numeric")
        lease = float(lease_raw)
        if lease <= 0 or lease != lease or lease == float("inf"):
            raise ValidationError("slice_claim.lease_seconds must be positive finite")
        out["lease_seconds"] = lease

    if claim_state == "executing" or "claimed_at" in out:
        if "claimed_at" not in out or out.get("claimed_at") is None:
            raise ValidationError(
                "slice_claim.executing requires numeric claimed_at"
            )
        claimed_raw = out.get("claimed_at")
        if isinstance(claimed_raw, bool) or not isinstance(
            claimed_raw, (int, float)
        ):
            raise ValidationError("slice_claim.claimed_at must be numeric")
        claimed_at = float(claimed_raw)
        if claimed_at != claimed_at or claimed_at == float("inf") or claimed_at < 0:
            raise ValidationError(
                "slice_claim.claimed_at must be a finite non-negative timestamp"
            )
        now = time.time()
        if claimed_at > now + _CLAIM_FUTURE_SKEW_SECONDS:
            raise ValidationError("slice_claim.claimed_at is unreasonably in the future")
        out["claimed_at"] = claimed_at
        if claim_state == "executing" and "lease_seconds" not in out:
            out["lease_seconds"] = float(SLICE_CLAIM_LEASE_SECONDS)

    if audit_status == "IN_SLICE" and claim_state not in {
        "executing",
        "timed_out",
        "failed",
        "awaiting_evidence",
    }:
        raise ValidationError(
            f"IN_SLICE slice_claim.state {claim_state!r} is not resumable"
        )
    return out


def sanitize_finding(finding: AuditFinding) -> AuditFinding:
    return AuditFinding(
        finding_id=finding.finding_id,
        unit=finding.unit,
        summary=sanitize_durable_text(finding.summary),
        severity=finding.severity,
    )


def sanitize_evidence(evidence: AuditEvidence) -> AuditEvidence:
    return AuditEvidence(
        status=evidence.status,
        unit=evidence.unit,
        target_sha=evidence.target_sha,
        notes=sanitize_durable_text(evidence.notes),
        truncated=evidence.truncated,
    )


def sanitize_slice_dict(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a persisted slice dict to an allowlisted, sanitized shape."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError("slice payload must be an object")
    allowed = {
        "unit",
        "target_sha",
        "outcome",
        "findings",
        "evidence",
        "audit_request",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(
            f"slice payload contains unsupported fields: {', '.join(unknown)}"
        )
    out: dict[str, Any] = {}
    if "unit" in raw:
        out["unit"] = str(raw.get("unit") or "")
    if "target_sha" in raw and raw.get("target_sha") is not None:
        out["target_sha"] = str(raw.get("target_sha") or "")
    if "outcome" in raw:
        out["outcome"] = str(raw.get("outcome") or "")
    if "audit_request" in raw:
        out["audit_request"] = sanitize_durable_text(
            str(raw.get("audit_request") or "")
        )
    evidence = raw.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise ValidationError("slice evidence must be an object")
        evidence_allowed = {"status", "unit", "target_sha", "notes", "truncated"}
        e_unknown = sorted(set(evidence) - evidence_allowed)
        if e_unknown:
            raise ValidationError(
                "slice evidence contains unsupported fields: "
                + ", ".join(e_unknown)
            )
        out["evidence"] = {
            "status": str(evidence.get("status") or ""),
            "unit": str(evidence.get("unit") or ""),
            "target_sha": str(evidence.get("target_sha") or ""),
            "notes": sanitize_durable_text(str(evidence.get("notes") or "")),
            "truncated": bool(evidence.get("truncated", False)),
        }
    findings = raw.get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            raise ValidationError("slice findings must be a list")
        cleaned_findings = []
        for item in findings:
            if not isinstance(item, dict):
                raise ValidationError("slice finding must be an object")
            f_allowed = {"finding_id", "unit", "summary", "severity"}
            f_unknown = sorted(set(item) - f_allowed)
            if f_unknown:
                raise ValidationError(
                    "slice finding contains unsupported fields: "
                    + ", ".join(f_unknown)
                )
            cleaned_findings.append(
                {
                    "finding_id": str(item.get("finding_id") or ""),
                    "unit": str(item.get("unit") or ""),
                    "summary": sanitize_durable_text(str(item.get("summary") or "")),
                    "severity": str(item.get("severity") or ""),
                }
            )
        out["findings"] = cleaned_findings
    return out


_COORDINATION_TOP_KEYS = frozenset(
    {
        "collector",
        "target_sha",
        "status",
        "outcome",
        "reasons",
        "work_packet",
        "pr",
        "ci",
        "reviews",
    }
)
_COORDINATION_NESTED_KEYS: dict[str, frozenset[str]] = {
    "work_packet": frozenset({"status", "state", "number", "updated_at"}),
    "pr": frozenset({"status", "number", "state", "headRefOid", "url", "candidates"}),
    "ci": frozenset({"status", "exit_code"}),
    "reviews": frozenset({"status", "actionable", "count"}),
}
_PR_CANDIDATE_KEYS = frozenset({"number", "title", "state", "headRefOid", "url"})


def _sanitize_durable_strings(value: Any, *, label: str) -> Any:
    """Recursively sanitize string leaves; preserve bool/int/null structure."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            raise ValidationError(f"{label} must not be NaN")
        return value
    if isinstance(value, str):
        return sanitize_durable_text(value)
    if isinstance(value, list):
        return [
            _sanitize_durable_strings(item, label=f"{label}[]") for item in value
        ]
    if isinstance(value, dict):
        return {
            str(key): _sanitize_durable_strings(item, label=f"{label}.{key}")
            for key, item in value.items()
        }
    raise ValidationError(f"{label} has unsupported durable type {type(value).__name__}")


def _sanitize_coordination_section(name: str, value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"coordination.{name} must be an object")
    allowed = _COORDINATION_NESTED_KEYS[name]
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(
            f"coordination.{name} contains unsupported fields: "
            + ", ".join(unknown)
        )
    out: dict[str, Any] = {}
    for key in allowed:
        if key not in value:
            continue
        item = value[key]
        if key == "candidates":
            if not isinstance(item, list):
                raise ValidationError("coordination.pr.candidates must be a list")
            cleaned_candidates = []
            for cand in item:
                if not isinstance(cand, dict):
                    raise ValidationError(
                        "coordination.pr.candidates entries must be objects"
                    )
                c_unknown = sorted(set(cand) - _PR_CANDIDATE_KEYS)
                if c_unknown:
                    raise ValidationError(
                        "coordination.pr.candidates entry has unsupported fields: "
                        + ", ".join(c_unknown)
                    )
                cleaned_candidates.append(
                    _sanitize_durable_strings(cand, label="coordination.pr.candidates")
                )
            out[key] = cleaned_candidates
            continue
        if key == "actionable":
            out[key] = bool(item)
            continue
        if key in {"number", "count", "exit_code"} and item is not None:
            try:
                out[key] = int(item)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"coordination.{name}.{key} must be an integer"
                ) from exc
            continue
        out[key] = _sanitize_durable_strings(
            item, label=f"coordination.{name}.{key}"
        )
    return out


def sanitize_coordination_snapshot(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Allowlist + recursively sanitize coordination refresh evidence."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError("last_coordination_refresh must be an object")
    unknown = sorted(set(raw) - _COORDINATION_TOP_KEYS)
    if unknown:
        raise ValidationError(
            "last_coordination_refresh contains unsupported fields: "
            + ", ".join(unknown)
        )
    out: dict[str, Any] = {}
    for key in _COORDINATION_TOP_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "reasons":
            if not isinstance(value, list):
                raise ValidationError("coordination reasons must be a list")
            out["reasons"] = [
                sanitize_durable_text(str(item)) for item in value if item is not None
            ]
            continue
        if key in {"collector", "target_sha", "status", "outcome"}:
            out[key] = sanitize_durable_text(str(value or ""))
            continue
        out[key] = _sanitize_coordination_section(key, value)
    return out


def sanitize_packet_for_persistence(
    packet: AuditControlPacket,
) -> AuditControlPacket:
    """Return a deep-copied packet safe for durable JSON persistence."""
    sanitized = AuditControlPacket.from_dict(packet.to_dict())
    sanitized.open_findings = [
        sanitize_finding(f) for f in sanitized.open_findings
    ]
    sanitized.last_completed_slice = sanitize_slice_dict(
        sanitized.last_completed_slice
    )
    cleaned_units: dict[str, dict[str, Any]] = {}
    for key, entry in sanitized.completed_units.items():
        cleaned = sanitize_slice_dict(entry)
        assert cleaned is not None
        cleaned_units[key] = cleaned
    sanitized.completed_units = cleaned_units
    sanitized.next_action = sanitize_durable_text(sanitized.next_action)
    sanitized.session.notes = sanitize_durable_text(sanitized.session.notes)
    sanitized.last_coordination_refresh = sanitize_coordination_snapshot(
        sanitized.last_coordination_refresh
    )
    return sanitized


def sanitize_slice_result(result: SliceResult) -> SliceResult:
    return SliceResult(
        unit=result.unit,
        target_sha=result.target_sha,
        outcome=result.outcome,
        findings=[sanitize_finding(f) for f in result.findings],
        evidence=(
            sanitize_evidence(result.evidence) if result.evidence else None
        ),
        audit_request=sanitize_durable_text(result.audit_request),
    )


def require_exact_commit_sha(value: str, *, label: str) -> str:
    """Normalize and require a full 40-char commit SHA (no prefix matching)."""
    normalized = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise ValidationError(f"{label} must be an exact 40-char commit SHA")
    return normalized


def parse_unit_key(unit_key: str) -> tuple[str, str, str]:
    parts = unit_key.split(":")
    if len(parts) != 3:
        raise ValidationError(f"invalid completed unit key: {unit_key!r}")
    run_key, unit, target_sha = parts
    if not run_key or not unit:
        raise ValidationError(f"invalid completed unit key: {unit_key!r}")
    target_sha = require_exact_commit_sha(
        target_sha, label=f"completed_units key SHA in {unit_key!r}"
    )
    return run_key, unit, target_sha


def validate_completed_unit_entry(
    unit_key: str,
    entry: Any,
    *,
    expected_run_key: str | None = None,
    expected_target_sha: str | None = None,
) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValidationError(
            f"completed_units[{unit_key!r}] must be an object"
        )
    run_key, unit, target_sha = parse_unit_key(unit_key)
    if expected_run_key is not None and run_key != expected_run_key:
        raise ValidationError(
            f"completed_units key run_key mismatch for {unit_key!r}"
        )
    if expected_target_sha is not None:
        expected = require_exact_commit_sha(
            expected_target_sha,
            label=f"expected target SHA for {unit_key!r}",
        )
        if target_sha != expected:
            raise ValidationError(
                f"completed_units key target SHA mismatch for {unit_key!r}"
            )
    outcome = str(entry.get("outcome", ""))
    if outcome not in {"PASS", "FINDING"}:
        raise ValidationError(
            f"completed_units[{unit_key!r}] has unsupported outcome {outcome!r}"
        )
    if str(entry.get("unit", "")) != unit:
        raise ValidationError(
            f"completed_units[{unit_key!r}] unit does not match key"
        )
    entry_sha = require_exact_commit_sha(
        str(entry.get("target_sha", "")),
        label=f"completed_units[{unit_key!r}] target_sha",
    )
    if entry_sha != target_sha:
        raise ValidationError(
            f"completed_units[{unit_key!r}] target_sha does not match key"
        )
    raw_evidence = entry.get("evidence")
    if not isinstance(raw_evidence, dict):
        raise ValidationError(
            f"completed_units[{unit_key!r}] requires evidence object"
        )
    evidence = AuditEvidence.from_dict(raw_evidence)
    if not evidence.is_passable():
        raise ValidationError(
            f"completed_units[{unit_key!r}] evidence is not COMPLETE"
        )
    evidence_sha = require_exact_commit_sha(
        evidence.target_sha,
        label=f"completed_units[{unit_key!r}] evidence.target_sha",
    )
    if evidence.unit != unit or evidence_sha != target_sha:
        raise ValidationError(
            f"completed_units[{unit_key!r}] evidence identity mismatch"
        )
    findings_raw = entry.get("findings", [])
    if not isinstance(findings_raw, list):
        raise ValidationError(
            f"completed_units[{unit_key!r}] findings must be a list"
        )
    findings = [AuditFinding.from_dict(item) for item in findings_raw]
    if outcome == "FINDING" and not findings:
        raise ValidationError(
            f"completed_units[{unit_key!r}] FINDING requires findings"
        )
    if outcome == "PASS" and findings:
        raise ValidationError(
            f"completed_units[{unit_key!r}] PASS cannot include findings"
        )
    return entry


def validate_completed_units_map(
    completed_units: dict[str, Any],
    *,
    expected_run_key: str | None = None,
    expected_target_sha: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(completed_units, dict):
        raise ValidationError("completed_units must be an object")
    validated: dict[str, dict[str, Any]] = {}
    for unit_key, entry in completed_units.items():
        validated[str(unit_key)] = validate_completed_unit_entry(
            str(unit_key),
            entry,
            expected_run_key=expected_run_key,
            expected_target_sha=expected_target_sha,
        )
    return validated


def build_audit_request(packet: AuditControlPacket, unit: str) -> str:
    base = packet.last_audited_sha or "NULL"
    return (
        f"AUDIT_UNIT={unit}\n"
        f"REPO={packet.target_repository}\n"
        f"BRANCH={packet.target_branch}\n"
        f"DELTA={base}..{packet.current_target_sha}\n"
        f"MODE={packet.mode}\n"
        f"RUN_KEY={packet.idempotency_run_key}\n"
        f"RESUME={RESUME_COMMAND}\n"
        "RULES=fail_closed_on_truncated_evidence;no_product_code_changes;"
        "persist_checkpoint_before_exit\n"
    )


class CheckpointStore(Protocol):
    def load(self) -> AuditControlPacket | None: ...

    def save(self, packet: AuditControlPacket) -> None: ...

    @contextmanager
    def lock(self) -> Iterator[None]: ...


class FileCheckpointStore:
    """Local durable checkpoint under the ADR-0005 data root."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.path = self.data_root / STORE_FILENAME
        self.lock_path = self.data_root / "chat-audit.lock"
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_handle: Any = None

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._thread_lock:
            if self._lock_depth == 0:
                self.data_root.mkdir(parents=True, exist_ok=True)
                self._lock_handle = open(self.lock_path, "a+", encoding="utf-8")
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0 and self._lock_handle is not None:
                    fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                    self._lock_handle.close()
                    self._lock_handle = None

    def load(self) -> AuditControlPacket | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return AuditControlPacket.from_dict(raw)

    def save(self, packet: AuditControlPacket) -> None:
        safe = sanitize_packet_for_persistence(packet)
        self.data_root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(safe.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


class MemoryCheckpointStore:
    """In-memory / adapter stand-in for GitHub-backed packet I/O in tests."""

    def __init__(self, packet: AuditControlPacket | None = None):
        self._packet = copy.deepcopy(packet) if packet else None
        self._lock = threading.RLock()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def load(self) -> AuditControlPacket | None:
        return copy.deepcopy(self._packet)

    def save(self, packet: AuditControlPacket) -> None:
        safe = sanitize_packet_for_persistence(packet)
        self._packet = AuditControlPacket.from_dict(safe.to_dict())


class UnitExecutor(Protocol):
    def execute(
        self, packet: AuditControlPacket, unit: str, audit_request: str
    ) -> SliceResult: ...


class FixedUnitExecutor:
    """Deterministic unit executor for tests / explicit offline mode only."""

    def __init__(
        self,
        outcomes: dict[str, str] | None = None,
        *,
        default_outcome: str = "PASS",
        truncate_units: set[str] | None = None,
        timeout_units: set[str] | None = None,
        finding_summaries: dict[str, str] | None = None,
    ):
        self.outcomes = outcomes or {}
        self.default_outcome = default_outcome
        self.truncate_units = truncate_units or set()
        self.timeout_units = timeout_units or set()
        self.finding_summaries = finding_summaries or {}
        self.calls: list[tuple[str, str]] = []

    def execute(
        self, packet: AuditControlPacket, unit: str, audit_request: str
    ) -> SliceResult:
        self.calls.append((unit, packet.current_target_sha))
        if unit in self.timeout_units:
            return SliceResult(
                unit=unit,
                target_sha=packet.current_target_sha,
                outcome="TIMEOUT",
                audit_request=audit_request,
                evidence=AuditEvidence(
                    status="MISSING",
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    notes="slice interrupted before evidence capture",
                    truncated=True,
                ),
            )
        if unit in self.truncate_units:
            evidence = AuditEvidence(
                status="TRUNCATED",
                unit=unit,
                target_sha=packet.current_target_sha,
                notes="",
                truncated=True,
            )
            return SliceResult(
                unit=unit,
                target_sha=packet.current_target_sha,
                outcome="REJECTED",
                audit_request=audit_request,
                evidence=evidence,
            )
        outcome = self.outcomes.get(unit, self.default_outcome)
        if outcome == "FINDING":
            summary = self.finding_summaries.get(
                unit, f"finding in {unit}"
            )
            finding = AuditFinding(
                finding_id=f"{packet.idempotency_run_key}:{unit}",
                unit=unit,
                summary=summary,
                severity="P1",
            )
            evidence = AuditEvidence(
                status="COMPLETE",
                unit=unit,
                target_sha=packet.current_target_sha,
                notes=summary,
            )
            return SliceResult(
                unit=unit,
                target_sha=packet.current_target_sha,
                outcome="FINDING",
                findings=[finding],
                audit_request=audit_request,
                evidence=evidence,
            )
        evidence = AuditEvidence(
            status="COMPLETE",
            unit=unit,
            target_sha=packet.current_target_sha,
            notes=f"{unit} pass on {packet.current_target_sha[:12]}",
        )
        return SliceResult(
            unit=unit,
            target_sha=packet.current_target_sha,
            outcome="PASS",
            audit_request=audit_request,
            evidence=evidence,
        )


class ExternalEvidenceUnitExecutor:
    """CLI/production executor: fail closed unless COMPLETE evidence is supplied."""

    def __init__(self, evidence_payload: dict[str, Any] | None = None):
        self.evidence_payload = evidence_payload
        self.calls: list[tuple[str, str]] = []

    def execute(
        self, packet: AuditControlPacket, unit: str, audit_request: str
    ) -> SliceResult:
        self.calls.append((unit, packet.current_target_sha))
        if not self.evidence_payload:
            return SliceResult(
                unit=unit,
                target_sha=packet.current_target_sha,
                outcome="AWAITING_EVIDENCE",
                audit_request=audit_request,
                evidence=AuditEvidence(
                    status="MISSING",
                    unit=unit,
                    target_sha=packet.current_target_sha,
                    notes="external COMPLETE evidence required",
                    truncated=True,
                ),
            )
        raw = self.evidence_payload
        if "unit" not in raw or not str(raw.get("unit", "")).strip():
            raise ValidationError(
                "evidence payload must explicitly identify unit"
            )
        if "target_sha" not in raw or not str(raw.get("target_sha", "")).strip():
            raise ValidationError(
                "evidence payload must explicitly identify target_sha"
            )
        evidence_sha = str(raw["target_sha"]).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", evidence_sha):
            raise ValidationError(
                "evidence target_sha must be an exact 40-char commit SHA"
            )
        expected_sha = packet.current_target_sha.strip().lower()
        if evidence_sha != expected_sha:
            raise ValidationError(
                "evidence target_sha must exactly equal current_target_sha"
            )
        if "outcome" not in raw or raw.get("outcome") is None:
            raise ValidationError(
                "evidence payload must explicitly set outcome"
            )
        outcome = str(raw.get("outcome")).strip()
        if outcome not in {"PASS", "FINDING", "TIMEOUT", "REJECTED"}:
            raise ValidationError(
                f"unsupported evidence outcome: {outcome!r}"
            )
        if "truncated" not in raw or not isinstance(raw.get("truncated"), bool):
            raise ValidationError(
                "evidence payload must explicitly set boolean truncated"
            )
        if "status" not in raw or not str(raw.get("status") or "").strip():
            raise ValidationError(
                "evidence payload must explicitly set status"
            )
        status = str(raw["status"]).strip()
        if status not in EVIDENCE_STATUSES:
            raise ValidationError(f"unsupported evidence status: {status!r}")
        truncated = raw["truncated"]
        if outcome == "PASS" and (
            truncated is not False or status != "COMPLETE"
        ):
            raise ValidationError(
                "PASS evidence requires status=COMPLETE and truncated=false"
            )
        evidence = sanitize_evidence(
            AuditEvidence.from_dict(
                {
                    "status": status,
                    "unit": str(raw["unit"]),
                    "target_sha": evidence_sha,
                    "notes": raw.get("notes", ""),
                    "truncated": truncated,
                }
            )
        )
        findings = [
            sanitize_finding(AuditFinding.from_dict(item))
            for item in raw.get("findings", [])
        ]
        return SliceResult(
            unit=unit,
            target_sha=packet.current_target_sha,
            outcome=outcome,
            findings=findings,
            audit_request=sanitize_durable_text(audit_request),
            evidence=evidence,
        )


class WorkPacketHandoff(Protocol):
    def upsert_implementation_packet(
        self, packet: AuditControlPacket, finding: AuditFinding
    ) -> dict[str, Any]: ...


class RecordingWorkPacketHandoff:
    def __init__(self) -> None:
        self.handoffs: list[dict[str, Any]] = []

    def upsert_implementation_packet(
        self, packet: AuditControlPacket, finding: AuditFinding
    ) -> dict[str, Any]:
        safe_finding = sanitize_finding(finding)
        record = {
            "title": f"[AI Work] Audit finding: {safe_finding.finding_id}",
            "repository": packet.target_repository,
            "branch": packet.target_branch,
            "head": packet.current_target_sha,
            "finding": safe_finding.to_dict(),
            "next_action": (
                "Implement the bounded audit finding in Cursor; "
                "do not modify product code from Chat."
            ),
        }
        self.handoffs.append(record)
        return record


class CoordinationRefresher(Protocol):
    """Same-HEAD refresh of Work Packet / PR / CI / review coordination state."""

    def refresh(self, packet: AuditControlPacket) -> dict[str, Any]: ...


class FixedCoordinationRefresher:
    """Deterministic coordination adapter for tests / offline fixtures."""

    def __init__(self, snapshot: dict[str, Any] | None = None):
        self.snapshot = dict(
            snapshot
            or {
                "status": "OK",
                "outcome": "PASSED",
                "reasons": [],
                "work_packet": {"status": "ACTIVE"},
                "pr": {"state": "OPEN"},
                "ci": {"status": "OK"},
                "reviews": {"actionable": False},
            }
        )
        self.calls = 0

    def refresh(self, packet: AuditControlPacket) -> dict[str, Any]:
        self.calls += 1
        out = copy.deepcopy(self.snapshot)
        out["target_sha"] = packet.current_target_sha
        out["collector"] = "atlas.chat_audit.FixedCoordinationRefresher"
        return out


def handoff_filename_for_finding_id(finding_id: str) -> str:
    """Collision-resistant filename: readable stem + digest of original ID."""
    digest = hashlib.sha256(finding_id.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", finding_id).strip("._-")
    if not stem:
        stem = "finding"
    stem = stem[:64]
    return f"{stem}__{digest}.json"


class FileWorkPacketHandoff:
    """Offline/test-only local finding handoff cache.

    Production Chat path must use GitHubAIWorkHandoff; local JSON is not a
    canonical success signal.
    """

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.directory = self.data_root / "chat-audit-handoffs"
        self.handoffs: list[dict[str, Any]] = []

    def upsert_implementation_packet(
        self, packet: AuditControlPacket, finding: AuditFinding
    ) -> dict[str, Any]:
        safe_finding = sanitize_finding(finding)
        # Re-validate id so filenames never embed secret-like material.
        AuditFinding.from_dict(safe_finding.to_dict())
        record = {
            "title": f"[AI Work] Audit finding: {safe_finding.finding_id}",
            "repository": packet.target_repository,
            "branch": packet.target_branch,
            "head": packet.current_target_sha,
            "finding": safe_finding.to_dict(),
            "next_action": (
                "Implement the bounded audit finding in Cursor; "
                "do not modify product code from Chat."
            ),
            "status": "OPEN",
            "finding_id": safe_finding.finding_id,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / handoff_filename_for_finding_id(
            safe_finding.finding_id
        )
        path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        record["handoff_path"] = str(path)
        self.handoffs.append(record)
        return record


class BrowserRolloverProvider(Protocol):
    def rollover(
        self, packet: AuditControlPacket, resume_command: str
    ) -> dict[str, Any]: ...


class FakeBrowserRolloverProvider:
    """Provider-independent fake that never mutates audit truth fields."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def rollover(
        self, packet: AuditControlPacket, resume_command: str
    ) -> dict[str, Any]:
        result = {
            "opened_fresh_chat": True,
            "submitted_resume": resume_command,
            "verified_from_packet": True,
            "provider": "fake",
            "run_key": packet.idempotency_run_key,
            "target_sha": packet.current_target_sha,
        }
        self.calls.append(result)
        return result


class StagehandRolloverProvider:
    """Optional Stagehand adapter stub — gated on Issue #19 go/no-go."""

    PROVIDER_DECISION_ISSUE = 19

    def __init__(self, *, provider_approved: bool = False):
        self.provider_approved = provider_approved

    def rollover(
        self, packet: AuditControlPacket, resume_command: str
    ) -> dict[str, Any]:
        if not self.provider_approved:
            raise ValidationError(
                "Stagehand rollover adapter is gated on Issue "
                f"#{self.PROVIDER_DECISION_ISSUE} provider go/no-go; "
                "not approved for this PoC path"
            )
        raise ValidationError(
            "Stagehand rollover adapter is optional and not implemented "
            "in this PoC; use FakeBrowserRolloverProvider"
        )


def stagehand_hard_dependency_present() -> bool:
    """Return True only if Stagehand is imported into this module graph."""
    import sys

    return any(name == "stagehand" or name.startswith("stagehand.") for name in sys.modules)


@dataclass
class Identity:
    repository: str
    branch: str
    head: str


IdentityResolver = Callable[[], Identity]


class ChatAuditController:
    """Plans and persists one bounded audit slice per invocation."""

    def __init__(
        self,
        store: CheckpointStore,
        *,
        executor: UnitExecutor | None = None,
        handoff: WorkPacketHandoff | None = None,
        rollover: BrowserRolloverProvider | None = None,
        coordination: CoordinationRefresher | None = None,
        identity_resolver: IdentityResolver | None = None,
        git_runner: GitRunner = default_git_runner,
        worktree_path: str | None = None,
        enforce_worktree_identity: bool = False,
        allow_trusted_identity: bool = False,
    ):
        self.store = store
        self.executor = executor or FixedUnitExecutor()
        self.handoff = handoff or RecordingWorkPacketHandoff()
        self.rollover = rollover or FakeBrowserRolloverProvider()
        if coordination is None:
            raise ValidationError(
                "ChatAuditController requires an explicit CoordinationRefresher; "
                "FixedCoordinationRefresher is offline/test-only and must not be "
                "the implicit production default"
            )
        self.coordination = coordination
        self.identity_resolver = identity_resolver
        self.git_runner = git_runner
        self.worktree_path = worktree_path
        self.enforce_worktree_identity = enforce_worktree_identity
        # Offline/test-only: allow caller-supplied repo/branch/head without worktree.
        # Production CLI must derive identity from worktree (or injected resolver).
        self.allow_trusted_identity = bool(
            allow_trusted_identity or identity_resolver is not None
        )

    def _resolve_identity(
        self,
        *,
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> Identity:
        if self.identity_resolver is not None:
            identity = self.identity_resolver()
        elif self.worktree_path:
            wt = self._read_worktree()
            identity = Identity(
                repository=wt.repository,
                branch=wt.branch,
                head=wt.head,
            )
        elif self.allow_trusted_identity and repository and branch and head:
            identity = Identity(
                repository=normalize_github_repository(repository),
                branch=branch,
                head=head,
            )
        else:
            raise ValidationError(
                "chat-audit requires authoritative worktree identity "
                "(or offline allow_trusted_identity); refusing caller-only "
                "repository/branch/head"
            )
        resolved = Identity(
            repository=normalize_github_repository(identity.repository),
            branch=str(identity.branch).strip(),
            head=require_exact_commit_sha(identity.head, label="HEAD"),
        )
        if not resolved.branch:
            raise ValidationError("branch identity is required")
        # Caller-supplied values are assertions against authoritative identity.
        if repository is not None:
            if normalize_github_repository(repository) != resolved.repository:
                raise ValidationError("repository identity mismatch")
        if branch is not None and str(branch).strip() != resolved.branch:
            raise ValidationError("branch identity mismatch")
        if head is not None:
            asserted = require_exact_commit_sha(head, label="asserted HEAD")
            if asserted != resolved.head:
                raise ValidationError("HEAD identity mismatch")
        return resolved

    def _read_worktree(self) -> WorktreeIdentity:
        assert self.worktree_path is not None
        cwd = self.worktree_path
        toplevel = self.git_runner(
            ["git", "rev-parse", "--show-toplevel"], cwd
        ).strip()
        origin = self.git_runner(
            ["git", "remote", "get-url", "origin"], cwd
        ).strip()
        branch = self.git_runner(
            ["git", "branch", "--show-current"], cwd
        ).strip()
        head = self.git_runner(["git", "rev-parse", "HEAD"], cwd).strip()
        if not branch:
            raise ValidationError("detached HEAD fails closed for chat-audit")
        return WorktreeIdentity(
            worktree_path=str(Path(cwd).resolve()),
            repository=normalize_github_repository(origin),
            branch=branch,
            head=head,
            toplevel=toplevel,
        )

    def initialize(
        self,
        *,
        repository: str,
        branch: str,
        head: str | None = None,
        include_release_readiness: bool = False,
        mode: str = "delta",
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            repository=repository, branch=branch, head=head
        )
        existing = self.store.load()
        if existing is not None:
            raise ValidationError(
                "chat-audit checkpoint already exists; use run-slice to resume"
            )
        run_key = make_run_key(
            identity.repository, identity.branch, identity.head
        )
        packet = AuditControlPacket(
            target_repository=identity.repository,
            target_branch=identity.branch,
            current_target_sha=identity.head,
            last_audited_sha=None,
            audit_status="IDLE",
            audit_queue=build_audit_queue(
                include_release_readiness=include_release_readiness
            ),
            current_unit=None,
            current_unit_index=0,
            next_action="run_next_audit_slice",
            idempotency_run_key=run_key,
            mode=mode,
            include_release_readiness=include_release_readiness,
            session=SessionState(state="ACTIVE"),
        )
        self.store.save(packet)
        return {
            "action": "initialized",
            "packet": packet.to_dict(),
            "cheap_no_change": False,
        }

    def _assert_packet_matches_identity(
        self,
        packet: AuditControlPacket,
        identity: Identity,
        *,
        require_head: bool,
    ) -> None:
        if packet.target_repository != identity.repository:
            raise ValidationError("stale repository: checkpoint/repo mismatch")
        if packet.target_branch != identity.branch:
            raise ValidationError("stale branch: checkpoint/branch mismatch")
        if require_head and packet.current_target_sha != identity.head:
            raise ValidationError("stale HEAD: checkpoint/HEAD mismatch")

    def show(
        self,
        *,
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            repository=repository, branch=branch, head=head
        )
        packet = self.store.load()
        if packet is None:
            raise ValidationError("no chat-audit checkpoint present")
        # Bind repo/branch. HEAD drift is resumable: run-slice rotates delta.
        self._assert_packet_matches_identity(
            packet, identity, require_head=False
        )
        payload = packet.to_dict()
        payload["worktree_head"] = identity.head
        payload["head_drift"] = not heads_match(
            packet.current_target_sha, identity.head
        )
        payload["resumable"] = True
        return payload

    def run_slice(
        self,
        *,
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
        include_release_readiness: bool = False,
    ) -> dict[str, Any]:
        try:
            return self._run_slice_locked(
                repository=repository,
                branch=branch,
                head=head,
                include_release_readiness=include_release_readiness,
            )
        except CheckpointCasConflict as exc:
            # Independent writer advanced canonical state; do not retry side
            # effects from this stale invocation. Operator/scheduler may retry.
            try:
                detail = sanitize_durable_text(str(exc)[:400])
            except ValidationError:
                detail = "<redacted-cas-conflict>"
            return {
                "action": "failed_closed",
                "outcome": "HUMAN_REQUIRED",
                "reason": "checkpoint_cas_conflict",
                "detail": detail,
                "cheap_no_change": False,
                "idempotent_replay": False,
            }

    def _run_slice_locked(
        self,
        *,
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
        include_release_readiness: bool = False,
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            repository=repository, branch=branch, head=head
        )
        with self.store.lock():
            packet = self.store.load()
            if packet is None:
                init = self.initialize(
                    repository=identity.repository,
                    branch=identity.branch,
                    head=identity.head,
                    include_release_readiness=include_release_readiness,
                )
                packet = AuditControlPacket.from_dict(init["packet"])

            if packet.target_repository != identity.repository:
                raise ValidationError("stale repository: checkpoint/repo mismatch")
            if packet.target_branch != identity.branch:
                raise ValidationError("stale branch: checkpoint/branch mismatch")

            # Refresh target SHA / queue when HEAD advanced.
            if not heads_match(packet.current_target_sha, identity.head):
                if packet.audit_status == "IN_SLICE" and _claim_is_genuinely_live(
                    packet.slice_claim
                ):
                    raise ValidationError(
                        "stale HEAD during live IN_SLICE claim: refuse to "
                        "advance another run's checkpoint"
                    )
                # Non-live IN_SLICE (timed_out, failed, expired executing) is
                # abandoned inside the delta rotation: claim cleared and run
                # key rebound to the new HEAD so a stale executor result cannot
                # apply. Do not persist a half-cleared IN_SLICE packet.
                packet = self._start_new_delta_run(packet, identity.head)
            elif packet.last_audited_sha and heads_match(
                packet.last_audited_sha, identity.head
            ):
                return self._cheap_no_change(packet)

            # Resume interrupted slice.
            if packet.audit_status == "IN_SLICE" and packet.current_unit:
                # Re-validate claim shape before any executor side effects —
                # in-memory/mutated packets must not bypass restore invariants.
                claim = validate_slice_claim(
                    packet.slice_claim,
                    run_key=packet.idempotency_run_key,
                    current_target_sha=packet.current_target_sha,
                    current_unit=packet.current_unit,
                    audit_status=packet.audit_status,
                    audit_queue=packet.audit_queue,
                )
                packet.slice_claim = claim
                claim = packet.slice_claim or {}
                claim_state = str(claim.get("state", ""))
                if claim_state == "executing":
                    claimed_at = float(claim["claimed_at"])
                    lease = float(
                        claim.get("lease_seconds") or SLICE_CLAIM_LEASE_SECONDS
                    )
                    age = time.time() - claimed_at
                    if age < lease:
                        raise ValidationError(
                            "active slice claim held; duplicate invocation refused"
                        )
                    # Expired executing claim is reclaimable only when fully valid.
                    packet.slice_claim = {
                        **claim,
                        "state": "timed_out",
                        "reclaimed_from_expired_lease": True,
                    }
                    self.store.save(packet)
                elif claim_state == "failed":
                    # Controller-boundary failure: reclaim immediately.
                    packet.slice_claim = {
                        **claim,
                        "state": "timed_out",
                        "reclaimed_from_failed_claim": True,
                    }
                    self.store.save(packet)
                unit = packet.current_unit
            else:
                unit = self._select_next_unit(packet)
                if unit is None:
                    return self._finalize_queue(packet)

            run_key = packet.idempotency_run_key
            unit_key = f"{run_key}:{unit}:{packet.current_target_sha}"
            if self._has_valid_completed_unit(packet, unit_key):
                prior = packet.completed_units[unit_key]
                return {
                    "action": "idempotent_replay",
                    "unit": unit,
                    "outcome": prior.get("outcome"),
                    "packet": packet.to_dict(),
                    "cheap_no_change": False,
                    "idempotent_replay": True,
                }

            audit_request = build_audit_request(packet, unit)
            claim_id = str(uuid.uuid4())
            packet.audit_status = "IN_SLICE"
            packet.current_unit = unit
            packet.current_unit_index = packet.audit_queue.index(unit)
            packet.next_action = f"complete_or_resume_unit:{unit}"
            packet.slice_claim = {
                "claim_id": claim_id,
                "unit": unit,
                "run_key": run_key,
                "target_sha": packet.current_target_sha,
                "state": "executing",
                "claimed_at": time.time(),
                "lease_seconds": SLICE_CLAIM_LEASE_SECONDS,
            }
            self.store.save(packet)

        try:
            result = self.executor.execute(packet, unit, audit_request)
        except Exception as exc:
            return self._reconcile_slice_exception(
                claim_id=claim_id,
                unit=unit,
                exc=exc,
                phase="executor",
            )

        with self.store.lock():
            latest = self.store.load()
            if latest is None:
                raise ValidationError("checkpoint disappeared during slice")
            claim = latest.slice_claim or {}
            if claim.get("claim_id") != claim_id:
                raise ValidationError(
                    "slice claim lost or stolen; refuse to apply stale result"
                )
            if self._has_valid_completed_unit(latest, unit_key):
                prior = latest.completed_units[unit_key]
                return {
                    "action": "idempotent_replay",
                    "unit": unit,
                    "outcome": prior.get("outcome"),
                    "packet": latest.to_dict(),
                    "cheap_no_change": False,
                    "idempotent_replay": True,
                }
            try:
                return self._apply_slice_result(
                    latest, result, claim_id=claim_id
                )
            except CheckpointCasConflict:
                raise
            except Exception as exc:
                return self._reconcile_slice_exception(
                    claim_id=claim_id,
                    unit=unit,
                    exc=exc,
                    phase="handoff",
                    locked_packet=latest,
                )

    def mark_session(
        self,
        state: str,
        *,
        notes: str = "",
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> dict[str, Any]:
        if state not in SESSION_STATES:
            raise ValidationError(f"unsupported session state: {state}")
        identity = self._resolve_identity(
            repository=repository, branch=branch, head=head
        )
        with self.store.lock():
            packet = self.store.load()
            if packet is None:
                raise ValidationError("no chat-audit checkpoint present")
            self._assert_packet_matches_identity(
                packet, identity, require_head=True
            )
            packet.session.state = state
            if notes:
                packet.session.notes = notes
            if state == "TIMEOUT":
                packet.next_action = "resume_from_checkpoint_after_timeout"
                if packet.slice_claim:
                    packet.slice_claim = {
                        **packet.slice_claim,
                        "state": "timed_out",
                    }
            elif state == "ROLLOVER_REQUIRED":
                packet.next_action = "run_rollover_then_resume"
            self.store.save(packet)
            return packet.to_dict()

    def perform_rollover(
        self,
        *,
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            repository=repository, branch=branch, head=head
        )
        with self.store.lock():
            packet = self.store.load()
            if packet is None:
                raise ValidationError("no chat-audit checkpoint present")
            self._assert_packet_matches_identity(
                packet, identity, require_head=True
            )
            if packet.session.state not in {
                "ROLLOVER_REQUIRED",
                "TIMEOUT",
                "STALLED",
            }:
                raise ValidationError(
                    "rollover requires ROLLOVER_REQUIRED/TIMEOUT/STALLED session"
                )
            # Durability: re-save canonical checkpoint before browser action.
            audit_snapshot = {
                "last_audited_sha": packet.last_audited_sha,
                "current_target_sha": packet.current_target_sha,
                "audit_status": packet.audit_status,
                "audit_queue": list(packet.audit_queue),
                "current_unit": packet.current_unit,
                "current_unit_index": packet.current_unit_index,
                "open_findings": [f.to_dict() for f in packet.open_findings],
                "idempotency_run_key": packet.idempotency_run_key,
                "completed_units": copy.deepcopy(packet.completed_units),
                "last_completed_slice": copy.deepcopy(packet.last_completed_slice),
                "next_action": packet.next_action,
                "slice_claim": copy.deepcopy(packet.slice_claim),
            }
            self.store.save(packet)

        provider_result = self.rollover.rollover(packet, RESUME_COMMAND)

        with self.store.lock():
            reloaded = self.store.load()
            assert reloaded is not None
            self._assert_packet_matches_identity(
                reloaded, identity, require_head=True
            )
            for key, value in audit_snapshot.items():
                current = getattr(reloaded, key)
                if key == "open_findings":
                    current = [f.to_dict() for f in current]
                if current != value:
                    raise ValidationError(
                        "rollover provider mutated canonical audit state"
                    )
            reloaded.session.state = "RESUMED"
            reloaded.session.last_resume_command = RESUME_COMMAND
            reloaded.session.rollover_count += 1
            reloaded.session.notes = (
                "fresh chat resumed from durable checkpoint"
            )
            # Preserve next_action / audit truth; only session fields change.
            self.store.save(reloaded)
            return {
                "action": "rollover_resumed",
                "provider_result": provider_result,
                "packet": reloaded.to_dict(),
                "audit_fields_unchanged": True,
            }

    def resume_instruction_payload(
        self,
        *,
        repository: str | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> dict[str, Any]:
        """Payload a fresh Chat needs — no conversation history required."""
        identity = self._resolve_identity(
            repository=repository, branch=branch, head=head
        )
        packet = self.store.load()
        if packet is None:
            raise ValidationError("no chat-audit checkpoint present")
        self._assert_packet_matches_identity(
            packet, identity, require_head=False
        )
        return {
            "resume_command": RESUME_COMMAND,
            "target_repository": packet.target_repository,
            "target_branch": packet.target_branch,
            "current_target_sha": packet.current_target_sha,
            "worktree_head": identity.head,
            "head_drift": not heads_match(
                packet.current_target_sha, identity.head
            ),
            "resumable": True,
            "last_audited_sha": packet.last_audited_sha,
            "audit_status": packet.audit_status,
            "current_unit": packet.current_unit,
            "next_action": packet.next_action,
            "idempotency_run_key": packet.idempotency_run_key,
            "open_findings_count": len(packet.open_findings),
            "conversation_history_required": False,
        }

    def _reconcile_slice_exception(
        self,
        *,
        claim_id: str,
        unit: str,
        exc: BaseException,
        phase: str,
        locked_packet: AuditControlPacket | None = None,
    ) -> dict[str, Any]:
        """Fail closed after executor/handoff exception; allow immediate retry."""

        def _apply(packet: AuditControlPacket) -> dict[str, Any]:
            claim = packet.slice_claim or {}
            if claim.get("claim_id") != claim_id:
                raise ValidationError(
                    "slice claim lost or stolen during exception reconcile"
                )
            try:
                detail = sanitize_durable_text(str(exc)[:400])
            except ValidationError:
                detail = "<redacted-exception>"
            # Remain IN_SLICE with failed claim so the same unit is immediately
            # reclaimable; next_action carries HUMAN_REQUIRED for operators.
            packet.audit_status = "IN_SLICE"
            packet.session.state = "STALLED"
            packet.session.notes = detail
            packet.next_action = f"HUMAN_REQUIRED:{phase}_exception:{unit}"
            packet.slice_claim = {
                **claim,
                "state": "failed",
                "error_phase": phase,
                "error": detail,
            }
            # Keep current_unit so timeout/resume can reclaim the same slice.
            packet.current_unit = unit
            if unit in packet.audit_queue:
                packet.current_unit_index = packet.audit_queue.index(unit)
            self.store.save(packet)
            return {
                "action": "failed_closed",
                "unit": unit,
                "outcome": "HUMAN_REQUIRED",
                "reason": f"{phase}_exception",
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }

        if locked_packet is not None:
            return _apply(locked_packet)
        with self.store.lock():
            latest = self.store.load()
            if latest is None:
                raise ValidationError(
                    "checkpoint disappeared during exception reconcile"
                )
            return _apply(latest)

    def _start_new_delta_run(
        self, packet: AuditControlPacket, new_head: str
    ) -> AuditControlPacket:
        new_head = require_exact_commit_sha(new_head, label="new HEAD")
        # Never promote an incomplete/failed target to last_audited_sha.
        # Baseline advances only on successful queue finalization (PASSED).
        packet.current_target_sha = new_head
        packet.idempotency_run_key = make_run_key(
            packet.target_repository, packet.target_branch, new_head
        )
        packet.audit_queue = build_audit_queue(
            include_release_readiness=packet.include_release_readiness
        )
        # Rotate start unit so continuous HEAD advances cannot starve later
        # risk units forever while still requiring exact-head evidence per unit.
        if packet.audit_queue:
            packet.fair_start_index = (
                int(packet.fair_start_index) + 1
            ) % len(packet.audit_queue)
        packet.current_unit = None
        packet.current_unit_index = packet.fair_start_index
        packet.completed_units = {}
        packet.slice_claim = None
        # Prior findings belonged to the previous target SHA / handoff cycle.
        # Fresh HEAD requires a fresh audit verdict; clear stale open findings.
        packet.open_findings = []
        packet.audit_status = "IDLE"
        packet.next_action = "run_next_audit_slice"
        # Preserve explicit mode (delta|full); do not silently narrow full audits.
        self.store.save(packet)
        return packet

    def _select_next_unit(self, packet: AuditControlPacket) -> str | None:
        queue = packet.audit_queue
        if not queue:
            return None
        start = int(packet.fair_start_index) % len(queue)
        for offset in range(len(queue)):
            unit = queue[(start + offset) % len(queue)]
            unit_key = (
                f"{packet.idempotency_run_key}:{unit}:"
                f"{packet.current_target_sha}"
            )
            if not self._has_valid_completed_unit(packet, unit_key):
                return unit
        return None

    def _has_valid_completed_unit(
        self, packet: AuditControlPacket, unit_key: str
    ) -> bool:
        if unit_key not in packet.completed_units:
            return False
        validate_completed_unit_entry(
            unit_key,
            packet.completed_units[unit_key],
            expected_run_key=packet.idempotency_run_key,
            expected_target_sha=packet.current_target_sha,
        )
        return True

    def _cheap_no_change(self, packet: AuditControlPacket) -> dict[str, Any]:
        packet.no_change_runs += 1
        snapshot = self.coordination.refresh(packet)
        if not isinstance(snapshot, dict):
            raise ValidationError("coordination refresh must return an object")
        # Normalize/sanitize before mutation so durable leaves never retain
        # raw external text (including nested review_body / extras).
        safe_snapshot = sanitize_coordination_snapshot(snapshot)
        assert safe_snapshot is not None
        packet.last_coordination_refresh = copy.deepcopy(safe_snapshot)
        status = str(safe_snapshot.get("status") or "").upper()
        outcome = str(safe_snapshot.get("outcome") or "").upper()
        reasons = [
            str(item) for item in (safe_snapshot.get("reasons") or []) if item
        ]
        # Fail closed: only explicit OK/PASSED with empty reasons and complete
        # collector surfaces may PASS. Missing/unknown/denylisted outcomes never
        # fall through to synthetic PASSED.
        COORDINATION_PASS_STATUSES = frozenset({"OK", "PASSED"})
        required_surfaces = ("work_packet", "pr", "ci", "reviews")
        surfaces_complete = all(
            isinstance(safe_snapshot.get(name), dict) for name in required_surfaces
        )
        collector = str(safe_snapshot.get("collector") or "").strip()
        snapshot_sha = str(safe_snapshot.get("target_sha") or "").strip().lower()
        explicit_pass = (
            status in COORDINATION_PASS_STATUSES
            and outcome in COORDINATION_PASS_STATUSES
            and not reasons
            and bool(collector)
            and snapshot_sha == packet.current_target_sha.lower()
            and surfaces_complete
        )
        if not explicit_pass:
            if reasons:
                reason = reasons[0]
            elif not outcome:
                reason = "coordination_outcome_missing"
            elif outcome not in COORDINATION_PASS_STATUSES:
                reason = f"coordination_outcome_unsupported:{outcome.lower()}"
            elif not status:
                reason = "coordination_status_missing"
            elif status not in COORDINATION_PASS_STATUSES:
                reason = f"coordination_status_unsupported:{status.lower()}"
            elif not collector:
                reason = "coordination_collector_missing"
            elif snapshot_sha != packet.current_target_sha.lower():
                reason = "coordination_target_sha_mismatch"
            elif not surfaces_complete:
                reason = "coordination_surfaces_incomplete"
            else:
                reason = "coordination_evidence_incomplete"
            packet.audit_status = "FINDINGS"
            packet.current_unit = None
            packet.slice_claim = None
            packet.next_action = f"HUMAN_REQUIRED:coordination:{reason}"
            packet.session.state = "STALLED"
            packet.session.notes = sanitize_durable_text(
                "; ".join(reasons)[:400] or reason
            )
            self.store.save(packet)
            return {
                "action": "cheap_no_change_rework",
                "outcome": "HUMAN_REQUIRED",
                "reason": reason,
                "coordination": safe_snapshot,
                "packet": packet.to_dict(),
                "cheap_no_change": True,
                "units_executed": 0,
                "idempotent_replay": False,
            }
        packet.audit_status = (
            "FINDINGS" if packet.open_findings else "PASSED"
        )
        packet.current_unit = None
        packet.slice_claim = None
        packet.next_action = "inspect_coordination_pr_ci_only"
        packet.session.state = "ACTIVE"
        self.store.save(packet)
        return {
            "action": "cheap_no_change",
            "outcome": "FINDINGS" if packet.open_findings else "PASSED",
            "coordination": safe_snapshot,
            "packet": packet.to_dict(),
            "cheap_no_change": True,
            "units_executed": 0,
            "idempotent_replay": False,
        }

    def _finalize_queue(self, packet: AuditControlPacket) -> dict[str, Any]:
        if not packet.audit_queue:
            raise ValidationError(
                "cannot finalize empty audit_queue without evidence"
            )
        # Every queued unit must have validated completion evidence.
        derived_findings: list[AuditFinding] = []
        for unit in packet.audit_queue:
            unit_key = (
                f"{packet.idempotency_run_key}:{unit}:"
                f"{packet.current_target_sha}"
            )
            if not self._has_valid_completed_unit(packet, unit_key):
                raise ValidationError(
                    f"cannot finalize without validated evidence for {unit}"
                )
            entry = packet.completed_units[unit_key]
            if str(entry.get("outcome")) == "FINDING":
                derived_findings.extend(
                    AuditFinding.from_dict(item)
                    for item in entry.get("findings", [])
                )
        # Authoritative verdict comes from persisted completed-unit outcomes.
        packet.open_findings = derived_findings
        if derived_findings:
            packet.audit_status = "FINDINGS"
            packet.next_action = "await_cursor_implementation_handoff"
        else:
            packet.audit_status = "PASSED"
            packet.last_audited_sha = packet.current_target_sha
            packet.next_action = "idle_until_next_scheduled_audit"
        packet.current_unit = None
        packet.slice_claim = None
        self.store.save(packet)
        return {
            "action": "queue_complete",
            "packet": packet.to_dict(),
            "cheap_no_change": False,
            "idempotent_replay": False,
        }

    def _apply_slice_result(
        self,
        packet: AuditControlPacket,
        result: SliceResult,
        *,
        claim_id: str | None = None,
    ) -> dict[str, Any]:
        if claim_id is not None:
            claim = packet.slice_claim or {}
            if claim.get("claim_id") != claim_id:
                raise ValidationError("slice claim mismatch while applying result")
        result = sanitize_slice_result(result)

        if result.outcome == "TIMEOUT":
            packet.audit_status = "IN_SLICE"
            packet.session.state = "TIMEOUT"
            packet.next_action = f"resume_unit:{result.unit}"
            if packet.slice_claim:
                packet.slice_claim = {
                    **packet.slice_claim,
                    "state": "timed_out",
                }
            self.store.save(packet)
            return {
                "action": "timeout_checkpointed",
                "unit": result.unit,
                "outcome": "TIMEOUT",
                "audit_request": result.audit_request,
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }

        if result.outcome == "AWAITING_EVIDENCE":
            packet.audit_status = "AWAITING_EVIDENCE"
            packet.next_action = f"supply_evidence_for_unit:{result.unit}"
            if packet.slice_claim:
                packet.slice_claim = {
                    **packet.slice_claim,
                    "state": "awaiting_evidence",
                }
            self.store.save(packet)
            return {
                "action": "awaiting_evidence",
                "unit": result.unit,
                "outcome": "AWAITING_EVIDENCE",
                "audit_request": result.audit_request,
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }

        evidence = result.evidence
        if evidence is None or not evidence.is_passable():
            packet.audit_status = "FAILED_CLOSED"
            packet.next_action = (
                f"reject_incomplete_evidence:{result.unit}"
            )
            packet.slice_claim = None
            self.store.save(packet)
            return {
                "action": "failed_closed",
                "unit": result.unit,
                "outcome": "REJECTED",
                "reason": "truncated_or_incomplete_evidence",
                "audit_request": result.audit_request,
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }

        if packet.current_unit and result.unit != packet.current_unit:
            raise ValidationError("executor unit does not match claimed unit")
        if evidence.unit != result.unit:
            raise ValidationError("evidence unit does not match result unit")
        for finding in result.findings:
            if finding.unit != result.unit:
                raise ValidationError(
                    "finding.unit must match claimed/result/evidence unit"
                )
        result_sha = require_exact_commit_sha(
            result.target_sha, label="result.target_sha"
        )
        evidence_sha = require_exact_commit_sha(
            evidence.target_sha, label="evidence.target_sha"
        )
        if result_sha != packet.current_target_sha:
            raise ValidationError(
                "stale evidence target SHA does not match checkpoint"
            )
        if evidence_sha != packet.current_target_sha:
            raise ValidationError(
                "stale evidence target SHA does not match checkpoint"
            )

        if result.outcome not in {"PASS", "FINDING"}:
            packet.audit_status = "FAILED_CLOSED"
            packet.next_action = f"reject_unsupported_outcome:{result.outcome}"
            packet.slice_claim = None
            self.store.save(packet)
            return {
                "action": "failed_closed",
                "unit": result.unit,
                "outcome": "REJECTED",
                "reason": "unsupported_executor_outcome",
                "audit_request": result.audit_request,
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }
        if result.outcome == "FINDING" and not result.findings:
            packet.audit_status = "FAILED_CLOSED"
            packet.next_action = "reject_finding_without_payload"
            packet.slice_claim = None
            self.store.save(packet)
            return {
                "action": "failed_closed",
                "unit": result.unit,
                "outcome": "REJECTED",
                "reason": "finding_without_payload",
                "audit_request": result.audit_request,
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }
        if result.outcome == "PASS" and result.findings:
            packet.audit_status = "FAILED_CLOSED"
            packet.next_action = "reject_pass_with_findings"
            packet.slice_claim = None
            self.store.save(packet)
            return {
                "action": "failed_closed",
                "unit": result.unit,
                "outcome": "REJECTED",
                "reason": "pass_with_findings",
                "audit_request": result.audit_request,
                "packet": packet.to_dict(),
                "cheap_no_change": False,
                "idempotent_replay": False,
            }

        unit_key = (
            f"{packet.idempotency_run_key}:{result.unit}:"
            f"{packet.current_target_sha}"
        )
        # Handoff before mutating durable open_findings / completed_units so a
        # handoff failure leaves no partial finding commit for duplicate replay.
        handoff_records: list[dict[str, Any]] = []
        if result.outcome == "FINDING":
            for finding in result.findings:
                handoff_records.append(
                    self.handoff.upsert_implementation_packet(packet, finding)
                )
            for finding in result.findings:
                packet.open_findings.append(finding)

        packet.last_completed_slice = result.to_dict()
        packet.completed_units[unit_key] = result.to_dict()
        packet.audit_status = "SLICE_COMPLETE"
        packet.current_unit = None
        packet.slice_claim = None
        packet.next_action = "run_next_audit_slice"
        if packet.session.state == "TIMEOUT":
            packet.session.state = "ACTIVE"
        self.store.save(packet)
        return {
            "action": "slice_complete",
            "unit": result.unit,
            "outcome": result.outcome,
            "audit_request": result.audit_request,
            "handoffs": handoff_records,
            "packet": packet.to_dict(),
            "cheap_no_change": False,
            "idempotent_replay": False,
        }
