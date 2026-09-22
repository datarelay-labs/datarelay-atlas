"""Continuous Chat Audit & Session Supervisor PoC v0 (ADR-0007).

Durable Audit Control Packet + bounded delta-first slices + optional rollover.
Chat conversations are execution instances; checkpoints are canonical.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from atlas.provenance import ValidationError
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

DEFAULT_AUDIT_UNITS = (
    "changed_code",
    "affected_contracts",
    "affected_tests_ci",
    "security_impact",
    "docs_spec_drift",
)

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
        return cls(
            finding_id=str(raw["finding_id"]),
            unit=str(raw["unit"]),
            summary=str(raw["summary"]),
            severity=str(raw.get("severity", "P2")),
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
        if not raw:
            return cls()
        state = str(raw.get("state", "ACTIVE"))
        if state not in SESSION_STATES:
            raise ValidationError(f"unsupported session state: {state}")
        return cls(
            state=state,
            last_resume_command=str(raw.get("last_resume_command", RESUME_COMMAND)),
            rollover_count=int(raw.get("rollover_count", 0)),
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
        findings = [
            AuditFinding.from_dict(item)
            for item in raw.get("open_findings", [])
        ]
        return cls(
            target_repository=normalize_github_repository(
                str(raw["target_repository"])
            ),
            target_branch=str(raw["target_branch"]),
            current_target_sha=str(raw["current_target_sha"]),
            last_audited_sha=(
                str(raw["last_audited_sha"])
                if raw.get("last_audited_sha")
                else None
            ),
            audit_status=status,
            audit_queue=[str(u) for u in raw.get("audit_queue", [])],
            current_unit=(
                str(raw["current_unit"]) if raw.get("current_unit") else None
            ),
            current_unit_index=int(raw.get("current_unit_index", 0)),
            open_findings=findings,
            next_action=str(raw.get("next_action", "")),
            last_completed_slice=copy.deepcopy(raw.get("last_completed_slice")),
            idempotency_run_key=str(raw.get("idempotency_run_key", "")),
            mode=str(raw.get("mode", "delta")),
            include_release_readiness=bool(
                raw.get("include_release_readiness", False)
            ),
            session=SessionState.from_dict(raw.get("session")),
            schema_version=version,
            completed_units=copy.deepcopy(raw.get("completed_units") or {}),
            no_change_runs=int(raw.get("no_change_runs", 0)),
        )


def build_audit_queue(*, include_release_readiness: bool = False) -> list[str]:
    queue = list(DEFAULT_AUDIT_UNITS)
    if include_release_readiness:
        queue.append("release_readiness")
    return queue


def make_run_key(repository: str, branch: str, target_sha: str) -> str:
    material = f"{repository}|{branch}|{target_sha.lower()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


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


class FileCheckpointStore:
    """Local durable checkpoint under the ADR-0005 data root."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.path = self.data_root / STORE_FILENAME

    def load(self) -> AuditControlPacket | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return AuditControlPacket.from_dict(raw)

    def save(self, packet: AuditControlPacket) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(packet.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


class MemoryCheckpointStore:
    """In-memory / adapter stand-in for GitHub-backed packet I/O in tests."""

    def __init__(self, packet: AuditControlPacket | None = None):
        self._packet = copy.deepcopy(packet) if packet else None

    def load(self) -> AuditControlPacket | None:
        return copy.deepcopy(self._packet)

    def save(self, packet: AuditControlPacket) -> None:
        self._packet = AuditControlPacket.from_dict(packet.to_dict())


class UnitExecutor(Protocol):
    def execute(
        self, packet: AuditControlPacket, unit: str, audit_request: str
    ) -> SliceResult: ...


class FixedUnitExecutor:
    """Deterministic unit executor for tests / offline PoC."""

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
        record = {
            "title": f"[AI Work] Audit finding: {finding.finding_id}",
            "repository": packet.target_repository,
            "branch": packet.target_branch,
            "head": packet.current_target_sha,
            "finding": finding.to_dict(),
            "next_action": (
                "Implement the bounded audit finding in Cursor; "
                "do not modify product code from Chat."
            ),
        }
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
        identity_resolver: IdentityResolver | None = None,
        git_runner: GitRunner = default_git_runner,
        worktree_path: str | None = None,
        enforce_worktree_identity: bool = False,
    ):
        self.store = store
        self.executor = executor or FixedUnitExecutor()
        self.handoff = handoff or RecordingWorkPacketHandoff()
        self.rollover = rollover or FakeBrowserRolloverProvider()
        self.identity_resolver = identity_resolver
        self.git_runner = git_runner
        self.worktree_path = worktree_path
        self.enforce_worktree_identity = enforce_worktree_identity

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
        elif repository and branch and head:
            identity = Identity(
                repository=normalize_github_repository(repository),
                branch=branch,
                head=head,
            )
        else:
            raise ValidationError(
                "chat-audit identity requires resolver, worktree, or "
                "explicit repository/branch/head"
            )
        if not HEAD_RE.match(identity.head.strip().lower()):
            raise ValidationError(f"invalid HEAD: {identity.head!r}")
        return Identity(
            repository=normalize_github_repository(identity.repository),
            branch=identity.branch,
            head=identity.head.strip().lower(),
        )

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
        if normalize_github_repository(repository) != identity.repository:
            raise ValidationError("repository identity mismatch")
        if branch != identity.branch:
            raise ValidationError("branch identity mismatch")
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

    def show(self) -> dict[str, Any]:
        packet = self.store.load()
        if packet is None:
            raise ValidationError("no chat-audit checkpoint present")
        return packet.to_dict()

    def run_slice(
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
            if packet.audit_status == "IN_SLICE":
                raise ValidationError(
                    "stale HEAD during IN_SLICE: refuse to advance another "
                    "run's checkpoint"
                )
            packet = self._start_new_delta_run(packet, identity.head)
        elif packet.last_audited_sha and heads_match(
            packet.last_audited_sha, identity.head
        ):
            return self._cheap_no_change(packet)

        # Resume interrupted slice.
        if packet.audit_status == "IN_SLICE" and packet.current_unit:
            unit = packet.current_unit
        else:
            unit = self._select_next_unit(packet)
            if unit is None:
                return self._finalize_queue(packet)

        run_key = packet.idempotency_run_key
        unit_key = f"{run_key}:{unit}:{packet.current_target_sha}"
        if unit_key in packet.completed_units:
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
        packet.audit_status = "IN_SLICE"
        packet.current_unit = unit
        packet.current_unit_index = packet.audit_queue.index(unit)
        packet.next_action = f"complete_or_resume_unit:{unit}"
        self.store.save(packet)

        result = self.executor.execute(packet, unit, audit_request)
        return self._apply_slice_result(packet, result)

    def mark_session(
        self, state: str, *, notes: str = ""
    ) -> dict[str, Any]:
        if state not in SESSION_STATES:
            raise ValidationError(f"unsupported session state: {state}")
        packet = self.store.load()
        if packet is None:
            raise ValidationError("no chat-audit checkpoint present")
        packet.session.state = state
        if notes:
            packet.session.notes = notes
        if state == "TIMEOUT":
            packet.next_action = "resume_from_checkpoint_after_timeout"
        elif state == "ROLLOVER_REQUIRED":
            packet.next_action = "run_rollover_then_resume"
        self.store.save(packet)
        return packet.to_dict()

    def perform_rollover(self) -> dict[str, Any]:
        packet = self.store.load()
        if packet is None:
            raise ValidationError("no chat-audit checkpoint present")
        if packet.session.state not in {"ROLLOVER_REQUIRED", "TIMEOUT", "STALLED"}:
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
        }
        self.store.save(packet)
        provider_result = self.rollover.rollover(packet, RESUME_COMMAND)
        # Reload and change only session fields.
        reloaded = self.store.load()
        assert reloaded is not None
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
        reloaded.session.notes = "fresh chat resumed from durable checkpoint"
        reloaded.next_action = "run_next_audit_slice"
        self.store.save(reloaded)
        return {
            "action": "rollover_resumed",
            "provider_result": provider_result,
            "packet": reloaded.to_dict(),
            "audit_fields_unchanged": True,
        }

    def resume_instruction_payload(self) -> dict[str, Any]:
        """Payload a fresh Chat needs — no conversation history required."""
        packet = self.store.load()
        if packet is None:
            raise ValidationError("no chat-audit checkpoint present")
        return {
            "resume_command": RESUME_COMMAND,
            "target_repository": packet.target_repository,
            "target_branch": packet.target_branch,
            "current_target_sha": packet.current_target_sha,
            "last_audited_sha": packet.last_audited_sha,
            "audit_status": packet.audit_status,
            "current_unit": packet.current_unit,
            "next_action": packet.next_action,
            "idempotency_run_key": packet.idempotency_run_key,
            "open_findings_count": len(packet.open_findings),
            "conversation_history_required": False,
        }

    def _start_new_delta_run(
        self, packet: AuditControlPacket, new_head: str
    ) -> AuditControlPacket:
        packet.last_audited_sha = packet.current_target_sha
        packet.current_target_sha = new_head
        packet.idempotency_run_key = make_run_key(
            packet.target_repository, packet.target_branch, new_head
        )
        packet.audit_queue = build_audit_queue(
            include_release_readiness=packet.include_release_readiness
        )
        packet.current_unit = None
        packet.current_unit_index = 0
        packet.completed_units = {}
        packet.audit_status = "IDLE"
        packet.next_action = "run_next_audit_slice"
        packet.mode = "delta"
        self.store.save(packet)
        return packet

    def _select_next_unit(self, packet: AuditControlPacket) -> str | None:
        for unit in packet.audit_queue:
            unit_key = (
                f"{packet.idempotency_run_key}:{unit}:"
                f"{packet.current_target_sha}"
            )
            if unit_key not in packet.completed_units:
                return unit
        return None

    def _cheap_no_change(self, packet: AuditControlPacket) -> dict[str, Any]:
        packet.no_change_runs += 1
        packet.audit_status = (
            "FINDINGS" if packet.open_findings else "PASSED"
        )
        packet.current_unit = None
        packet.next_action = "inspect_coordination_pr_ci_only"
        packet.session.state = "ACTIVE"
        self.store.save(packet)
        return {
            "action": "cheap_no_change",
            "packet": packet.to_dict(),
            "cheap_no_change": True,
            "units_executed": 0,
            "idempotent_replay": False,
        }

    def _finalize_queue(self, packet: AuditControlPacket) -> dict[str, Any]:
        if packet.open_findings:
            packet.audit_status = "FINDINGS"
            packet.next_action = "await_cursor_implementation_handoff"
        else:
            packet.audit_status = "PASSED"
            packet.last_audited_sha = packet.current_target_sha
            packet.next_action = "idle_until_next_scheduled_audit"
        packet.current_unit = None
        self.store.save(packet)
        return {
            "action": "queue_complete",
            "packet": packet.to_dict(),
            "cheap_no_change": False,
            "idempotent_replay": False,
        }

    def _apply_slice_result(
        self, packet: AuditControlPacket, result: SliceResult
    ) -> dict[str, Any]:
        if result.outcome == "TIMEOUT":
            packet.audit_status = "IN_SLICE"
            packet.session.state = "TIMEOUT"
            packet.next_action = f"resume_unit:{result.unit}"
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

        evidence = result.evidence
        if evidence is None or not evidence.is_passable():
            packet.audit_status = "FAILED_CLOSED"
            packet.next_action = (
                f"reject_incomplete_evidence:{result.unit}"
            )
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

        if not heads_match(result.target_sha, packet.current_target_sha):
            raise ValidationError(
                "stale evidence target SHA does not match checkpoint"
            )

        unit_key = (
            f"{packet.idempotency_run_key}:{result.unit}:"
            f"{packet.current_target_sha}"
        )
        handoff_records: list[dict[str, Any]] = []
        if result.outcome == "FINDING":
            for finding in result.findings:
                packet.open_findings.append(finding)
                handoff_records.append(
                    self.handoff.upsert_implementation_packet(packet, finding)
                )

        packet.last_completed_slice = result.to_dict()
        packet.completed_units[unit_key] = result.to_dict()
        packet.audit_status = "SLICE_COMPLETE"
        packet.current_unit = None
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
