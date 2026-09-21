"""Autonomous Work Controller PoC v0 (ADR-0006).

Persists one local workstream, accepts idempotent Cursor completion events,
runs an independent audit, and either stops or dispatches a fresh /resume.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from atlas.provenance import ValidationError

CONTROLLER_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3
RESUME_PROMPT = "/resume"
HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")
WORKSTREAM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

AuditVerdict = str  # PASS | REWORK | HUMAN_REQUIRED
ControllerState = str


ALLOWED_STATES = frozenset(
    {
        "IDLE",
        "AWAITING_AUDIT",
        "AUDITING",
        "REWORK_DISPATCHED",
        "PASSED",
        "HUMAN_REQUIRED",
    }
)
TERMINAL_STATES = frozenset({"PASSED", "HUMAN_REQUIRED"})
AUDIT_VERDICTS = frozenset({"PASS", "REWORK", "HUMAN_REQUIRED"})


@dataclass(frozen=True)
class CompletionEvent:
    event_id: str
    workstream: str
    issue_number: int
    branch: str
    head: str
    attempt: int
    session_id: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "CompletionEvent":
        missing = [
            key
            for key in (
                "event_id",
                "workstream",
                "issue_number",
                "branch",
                "head",
                "attempt",
            )
            if key not in raw or raw[key] in (None, "")
        ]
        if missing:
            raise ValidationError(
                f"completion event missing required fields: {', '.join(missing)}"
            )
        try:
            issue_number = int(raw["issue_number"])
            attempt = int(raw["attempt"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("issue_number and attempt must be integers") from exc
        return cls(
            event_id=str(raw["event_id"]).strip(),
            workstream=str(raw["workstream"]).strip(),
            issue_number=issue_number,
            branch=str(raw["branch"]).strip(),
            head=str(raw["head"]).strip().lower(),
            attempt=attempt,
            session_id=(
                str(raw["session_id"]).strip()
                if raw.get("session_id") not in (None, "")
                else None
            ),
        )


@dataclass(frozen=True)
class AuditResult:
    verdict: str
    findings: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in AUDIT_VERDICTS:
            raise ValidationError(f"invalid audit verdict: {self.verdict}")


@dataclass(frozen=True)
class DispatchRequest:
    workstream: str
    worktree_path: str
    branch: str
    issue_number: int
    attempt: int
    resume_prompt: str = RESUME_PROMPT


@dataclass(frozen=True)
class DispatchResult:
    session_id: str
    command: list[str]


@dataclass
class WorkstreamRecord:
    workstream: str
    repository: str
    issue_number: int
    branch: str
    worktree_path: str
    expected_head: str
    state: str = "IDLE"
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    last_event_id: str | None = None
    last_session_id: str | None = None
    last_audit_verdict: str | None = None
    last_findings: str = ""
    last_outcome: dict = field(default_factory=dict)
    processed_event_ids: list[str] = field(default_factory=list)
    pending_event: dict | None = None


class AuditPort(Protocol):
    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        ...


class WorkPacketPort(Protocol):
    def apply_rework_findings(
        self,
        *,
        issue_number: int,
        findings: str,
        attempt: int,
        head: str,
    ) -> None:
        ...


class CursorDispatchPort(Protocol):
    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        ...


class ObserverPort(Protocol):
    def observe(self, message: str, payload: dict) -> None:
        ...


class NullObserver:
    def observe(self, message: str, payload: dict) -> None:
        return None


class RecordingObserver:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    def observe(self, message: str, payload: dict) -> None:
        self.messages.append((message, payload))


class FixedAuditAdapter:
    """Deterministic audit adapter for tests and offline dogfood."""

    def __init__(self, result: AuditResult) -> None:
        self._result = result
        self.calls: list[CompletionEvent] = []

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        self.calls.append(event)
        return self._result


class RecordingWorkPacketAdapter:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def apply_rework_findings(
        self,
        *,
        issue_number: int,
        findings: str,
        attempt: int,
        head: str,
    ) -> None:
        self.updates.append(
            {
                "issue_number": issue_number,
                "findings": findings,
                "attempt": attempt,
                "head": head,
            }
        )


class RecordingCursorDispatcher:
    def __init__(self, session_prefix: str = "session") -> None:
        self.requests: list[DispatchRequest] = []
        self._n = 0
        self.session_prefix = session_prefix

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self._n += 1
        self.requests.append(request)
        command = build_persist_resume_command(request)
        return DispatchResult(
            session_id=f"{self.session_prefix}-{self._n}",
            command=command,
        )


def build_persist_resume_command(request: DispatchRequest) -> list[str]:
    """Fixed argv surface for a fresh persistent resume in a validated worktree.

    Long-term native mechanism (Cursor CLI docs/changelog): interactive
    `agent persist` in the worktree, then manage via list/attach/stop.
    Prompt-create is intentionally not assumed reliable on CLI
    2026.09.18-9a7762b; runners may attach and submit RESUME_PROMPT.
    """
    return [
        "agent",
        "--workspace",
        request.worktree_path,
        "--trust",
        "persist",
    ]


class SubprocessCursorDispatcher:
    """Spawn the fixed persist argv via an injected runner (no shell)."""

    def __init__(
        self,
        runner: Callable[[list[str]], str] | None = None,
    ) -> None:
        self._runner = runner or _default_spawn_runner
        self.requests: list[DispatchRequest] = []

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self.requests.append(request)
        command = build_persist_resume_command(request)
        session_id = self._runner(command)
        if not session_id or not str(session_id).strip():
            raise ValidationError("cursor dispatcher returned empty session id")
        return DispatchResult(session_id=str(session_id).strip(), command=command)


def _default_spawn_runner(command: list[str]) -> str:
    """Fail closed in library default: operators must inject a real runner."""
    raise ValidationError(
        "no cursor spawn runner configured; inject a runner or use the dogfood "
        f"runbook for native `agent persist` (planned argv: {command!r})"
    )


class WorkControllerStore:
    """Filesystem JSON store for controller state (ADR-0006)."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.path = self.data_root / "work-controller.json"

    def _empty(self) -> dict:
        return {"schema_version": CONTROLLER_SCHEMA_VERSION, "workstreams": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        version = data.get("schema_version")
        if version != CONTROLLER_SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported work-controller schema_version: {version}; "
                f"expected {CONTROLLER_SCHEMA_VERSION}"
            )
        if "workstreams" not in data or not isinstance(data["workstreams"], dict):
            raise ValidationError("work-controller.json missing workstreams map")
        return data

    def _save(self, data: dict) -> None:
        payload = {
            "schema_version": CONTROLLER_SCHEMA_VERSION,
            "workstreams": data.get("workstreams", {}),
        }
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def list_workstreams(self) -> list[WorkstreamRecord]:
        data = self._load()
        return [self._from_dict(raw) for raw in data["workstreams"].values()]

    def get(self, workstream: str) -> WorkstreamRecord:
        data = self._load()
        raw = data["workstreams"].get(workstream)
        if raw is None:
            raise ValidationError(f"unknown workstream: {workstream}")
        return self._from_dict(raw)

    def put(self, record: WorkstreamRecord) -> WorkstreamRecord:
        data = self._load()
        data["workstreams"][record.workstream] = self._to_dict(record)
        self._save(data)
        return record

    def _from_dict(self, raw: dict) -> WorkstreamRecord:
        state = raw.get("state", "IDLE")
        if state not in ALLOWED_STATES:
            raise ValidationError(f"invalid controller state: {state}")
        return WorkstreamRecord(
            workstream=raw["workstream"],
            repository=raw["repository"],
            issue_number=int(raw["issue_number"]),
            branch=raw["branch"],
            worktree_path=raw["worktree_path"],
            expected_head=str(raw["expected_head"]).lower(),
            state=state,
            attempt=int(raw.get("attempt", 0)),
            max_attempts=int(raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
            last_event_id=raw.get("last_event_id"),
            last_session_id=raw.get("last_session_id"),
            last_audit_verdict=raw.get("last_audit_verdict"),
            last_findings=raw.get("last_findings", ""),
            last_outcome=dict(raw.get("last_outcome") or {}),
            processed_event_ids=list(raw.get("processed_event_ids") or []),
            pending_event=raw.get("pending_event"),
        )

    def _to_dict(self, record: WorkstreamRecord) -> dict:
        return {
            "workstream": record.workstream,
            "repository": record.repository,
            "issue_number": record.issue_number,
            "branch": record.branch,
            "worktree_path": record.worktree_path,
            "expected_head": record.expected_head,
            "state": record.state,
            "attempt": record.attempt,
            "max_attempts": record.max_attempts,
            "last_event_id": record.last_event_id,
            "last_session_id": record.last_session_id,
            "last_audit_verdict": record.last_audit_verdict,
            "last_findings": record.last_findings,
            "last_outcome": record.last_outcome,
            "processed_event_ids": record.processed_event_ids,
            "pending_event": record.pending_event,
        }


class WorkController:
    """Bounded local dogfood control loop."""

    def __init__(
        self,
        data_root: Path,
        *,
        audit: AuditPort,
        work_packet: WorkPacketPort,
        dispatcher: CursorDispatchPort,
        observer: ObserverPort | None = None,
    ) -> None:
        self.store = WorkControllerStore(data_root)
        self.audit = audit
        self.work_packet = work_packet
        self.dispatcher = dispatcher
        self.observer = observer or NullObserver()

    def register_workstream(
        self,
        *,
        workstream: str,
        repository: str,
        issue_number: int,
        branch: str,
        worktree_path: str,
        expected_head: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> WorkstreamRecord:
        self._validate_workstream_id(workstream)
        self._validate_head(expected_head)
        if max_attempts < 1:
            raise ValidationError("max_attempts must be >= 1")
        worktree = Path(worktree_path).resolve()
        if not worktree.is_dir():
            raise ValidationError(f"worktree_path is not a directory: {worktree}")
        existing = {
            item.workstream for item in self.store.list_workstreams()
        }
        if workstream in existing:
            raise ValidationError(f"workstream already registered: {workstream}")
        record = WorkstreamRecord(
            workstream=workstream,
            repository=repository.strip(),
            issue_number=int(issue_number),
            branch=branch.strip(),
            worktree_path=str(worktree),
            expected_head=expected_head.strip().lower(),
            state="IDLE",
            attempt=0,
            max_attempts=int(max_attempts),
        )
        return self.store.put(record)

    def show(self, workstream: str) -> dict:
        return asdict(self.store.get(workstream))

    def list_workstreams(self) -> list[dict]:
        return [asdict(item) for item in self.store.list_workstreams()]

    def handle_completion(self, raw_event: dict) -> dict:
        event = CompletionEvent.from_dict(raw_event)
        record = self.store.get(event.workstream)

        if event.event_id in record.processed_event_ids:
            outcome = dict(record.last_outcome or {})
            outcome["idempotent_replay"] = True
            self.observer.observe("completion_idempotent", outcome)
            return outcome

        self._assert_event_identity(record, event)

        record.state = "AWAITING_AUDIT"
        record.pending_event = asdict(event)
        record.last_event_id = event.event_id
        if event.session_id:
            record.last_session_id = event.session_id
        self.store.put(record)

        return self._run_audit_and_advance(record.workstream)

    def reconcile(self, workstream: str | None = None) -> list[dict]:
        """Recover after controller restart.

        Unfinished audit states re-run the pending event once. Terminal and
        REWORK_DISPATCHED states are left unchanged.
        """
        targets = (
            [self.store.get(workstream)]
            if workstream
            else self.store.list_workstreams()
        )
        outcomes: list[dict] = []
        for record in targets:
            if record.state in {"AWAITING_AUDIT", "AUDITING"} and record.pending_event:
                outcomes.append(self._run_audit_and_advance(record.workstream))
            else:
                outcomes.append(
                    {
                        "workstream": record.workstream,
                        "state": record.state,
                        "reconciled": False,
                        "reason": "no_pending_audit",
                    }
                )
        return outcomes

    def _run_audit_and_advance(self, workstream: str) -> dict:
        record = self.store.get(workstream)
        if not record.pending_event:
            raise ValidationError(f"no pending event for workstream: {workstream}")
        event = CompletionEvent.from_dict(record.pending_event)

        record.state = "AUDITING"
        self.store.put(record)

        audit_result = self.audit.audit(event, record)
        record = self.store.get(workstream)
        record.last_audit_verdict = audit_result.verdict
        record.last_findings = audit_result.findings

        if audit_result.verdict == "PASS":
            outcome = self._finalize(
                record,
                event,
                state="PASSED",
                action="stop",
                verdict="PASS",
            )
        elif audit_result.verdict == "HUMAN_REQUIRED":
            outcome = self._finalize(
                record,
                event,
                state="HUMAN_REQUIRED",
                action="stop",
                verdict="HUMAN_REQUIRED",
            )
        else:
            next_attempt = event.attempt + 1
            if next_attempt > record.max_attempts:
                record.last_findings = (
                    f"{audit_result.findings}\n"
                    f"retry exhausted at attempt {event.attempt}/{record.max_attempts}"
                ).strip()
                outcome = self._finalize(
                    record,
                    event,
                    state="HUMAN_REQUIRED",
                    action="stop",
                    verdict="HUMAN_REQUIRED",
                    extra={"reason": "retry_exhausted"},
                )
            else:
                self.work_packet.apply_rework_findings(
                    issue_number=record.issue_number,
                    findings=audit_result.findings,
                    attempt=next_attempt,
                    head=event.head,
                )
                dispatch = self.dispatcher.start_resume(
                    DispatchRequest(
                        workstream=record.workstream,
                        worktree_path=record.worktree_path,
                        branch=record.branch,
                        issue_number=record.issue_number,
                        attempt=next_attempt,
                        resume_prompt=RESUME_PROMPT,
                    )
                )
                record.attempt = next_attempt
                record.last_session_id = dispatch.session_id
                record.expected_head = event.head
                outcome = self._finalize(
                    record,
                    event,
                    state="REWORK_DISPATCHED",
                    action="rework_dispatched",
                    verdict="REWORK",
                    extra={
                        "dispatch_session_id": dispatch.session_id,
                        "dispatch_command": dispatch.command,
                        "next_attempt": next_attempt,
                        "resume_prompt": RESUME_PROMPT,
                    },
                )
        self.observer.observe("completion_handled", outcome)
        return outcome

    def _finalize(
        self,
        record: WorkstreamRecord,
        event: CompletionEvent,
        *,
        state: str,
        action: str,
        verdict: str,
        extra: dict | None = None,
    ) -> dict:
        if event.event_id not in record.processed_event_ids:
            record.processed_event_ids.append(event.event_id)
        record.state = state
        record.pending_event = None
        outcome = {
            "workstream": record.workstream,
            "event_id": event.event_id,
            "state": state,
            "action": action,
            "verdict": verdict,
            "attempt": event.attempt,
            "head": event.head,
            "findings": record.last_findings,
            "idempotent_replay": False,
        }
        if extra:
            outcome.update(extra)
        record.last_outcome = outcome
        self.store.put(record)
        return outcome

    def _assert_event_identity(
        self, record: WorkstreamRecord, event: CompletionEvent
    ) -> None:
        self._validate_head(event.head)
        if record.state in TERMINAL_STATES:
            raise ValidationError(
                f"workstream {record.workstream} is terminal ({record.state}); "
                "register a new cycle before accepting completions"
            )
        if event.issue_number != record.issue_number:
            raise ValidationError(
                f"issue_number mismatch: event={event.issue_number} "
                f"registered={record.issue_number}"
            )
        if event.branch != record.branch:
            raise ValidationError(
                f"branch mismatch: event={event.branch} registered={record.branch}"
            )
        if record.state == "REWORK_DISPATCHED":
            # Rework may produce a new HEAD; accept any well-formed sha and
            # lock it as expected_head once this event is accepted.
            if event.attempt != record.attempt:
                raise ValidationError(
                    f"attempt mismatch after rework: event={event.attempt} "
                    f"expected={record.attempt}"
                )
            record.expected_head = event.head
            self.store.put(record)
            return
        if event.head != record.expected_head:
            raise ValidationError(
                f"stale or invalid head: event={event.head} "
                f"expected={record.expected_head}"
            )
        if record.state == "IDLE" and record.attempt == 0:
            if event.attempt != 1:
                raise ValidationError(
                    f"first completion attempt must be 1, got {event.attempt}"
                )
            return
        raise ValidationError(
            f"refusing completion in state={record.state} attempt={record.attempt}"
        )

    @staticmethod
    def _validate_workstream_id(workstream: str) -> None:
        if not WORKSTREAM_RE.match(workstream):
            raise ValidationError(f"invalid workstream id: {workstream}")

    @staticmethod
    def _validate_head(head: str) -> None:
        if not HEAD_RE.match(head.strip().lower()):
            raise ValidationError(f"invalid head sha: {head}")


def default_data_root() -> Path:
    env = os.environ.get("ATLAS_DATA_ROOT", "").strip()
    if env:
        return Path(env)
    return Path.cwd() / ".atlas-data"


def load_completion_event(path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValidationError("completion event must be a JSON object")
    return raw


class OpenAIAuditAdapter:
    """Replaceable production audit port.

    Credentials stay in runtime env (`OPENAI_API_KEY`). This PoC adapter does
    not embed network I/O in unit tests; inject FixedAuditAdapter there.
    When enabled, callers must supply an `execute` callable that performs the
    HTTP call and returns an AuditResult.
    """

    def __init__(
        self,
        *,
        execute: Callable[[CompletionEvent, WorkstreamRecord, str], AuditResult],
        api_key_env: str = "OPENAI_API_KEY",
    ) -> None:
        self._execute = execute
        self._api_key_env = api_key_env

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        api_key = os.environ.get(self._api_key_env, "").strip()
        if not api_key:
            raise ValidationError(
                f"{self._api_key_env} is required for OpenAI audit adapter"
            )
        # Never persist or return the key.
        return self._execute(event, record, api_key)
