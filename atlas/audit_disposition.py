"""Exact-HEAD audit disposition (Issue #47 slice D).

Connects one completed audit claim to the canonical Work Packet and the
host-local Cursor resume transport. REWORK mutates that packet once, then
resumes the same project Chat ID. PASS writes a governance checkpoint only.
HUMAN_REQUIRED retains a bounded reason and does not dispatch. This module
does not merge, release, deploy, or activate another Work Packet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atlas.audit_claim import (
    AuditClaim,
    IssueAuditLedger,
    WorkPacketSnapshot,
    make_audit_claim_key,
    validate_stored_disposition,
)
from atlas.chat_audit import CheckpointCasConflict, require_exact_commit_sha
from atlas.host_worker import (
    HostWorkerConfig,
    ProcessList,
    ProjectDescriptor,
    SessionList,
    _select_descriptor,
    actual_host_id,
    normalize_host_id,
    persistent_cursor_active,
    require_external_state_root,
    run_once,
)
from atlas.provenance import ValidationError
from atlas.secrets import redact_sensitive_audit_text
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    GitRunner,
    _packet_metadata_value,
    normalize_github_repository,
    redact_absolute_paths,
    render_dispatch_blocked_work_packet_body,
    render_pass_governance_work_packet_body,
    render_rework_work_packet_body,
    sanitize_rework_findings,
    validate_clean_worktree_identity,
)

Spawn = Callable[[list[str], str], int]
_TERMINAL_ACTIONS = frozenset(
    {"redispatched", "dispatch_blocked", "pass_checkpoint", "human_required"}
)


@dataclass(frozen=True)
class DeterministicGateSnapshot:
    tests_status: str
    ci_status: str
    ci_head: str
    review_status: str
    governance_status: str
    governance_head: str


class MemoryPacketStore:
    """CAS stand-in for one canonical Work Packet body. Tests never call GitHub."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.token = "sha0"
        self.mutations = 0

    def load(self) -> tuple[str, str]:
        return self.body, self.token

    def cas_save(self, body: str, *, expected_body: str, expected_token: str) -> None:
        if self.body != expected_body or self.token != expected_token:
            raise CheckpointCasConflict("work packet compare-and-set failed")
        self.body = body
        self.mutations += 1
        self.token = f"sha{self.mutations}"


def gates_are_current(gates: DeterministicGateSnapshot | None, *, head: str) -> bool:
    if gates is None:
        return False
    return (
        gates.tests_status == "PASS"
        and gates.ci_status == "OK"
        and gates.ci_head == head
        and gates.review_status in {"OK", "ABSENT"}
        and gates.governance_status == "CURRENT"
        and gates.governance_head == head
    )


def _result(
    action: str,
    *,
    cursor_calls: int = 0,
    packet_mutations: int = 0,
    verdict: str | None = None,
    findings: str | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "cursor_calls": cursor_calls,
        "packet_mutations": packet_mutations,
        "verdict": verdict,
        "findings": findings,
    }


def _bounded_findings(findings: str, *, chat_id: str, worktree: str) -> str:
    text = redact_sensitive_audit_text(findings or "", max_chars=4000)
    if chat_id:
        text = text.replace(chat_id, "[redacted-chat]")
    if worktree:
        text = text.replace(worktree, "[redacted-worktree]")
    text = redact_absolute_paths(text)
    safe = sanitize_rework_findings(text)
    if chat_id and chat_id in safe:
        raise ValidationError("refusing to persist a Cursor chat id")
    return safe or "(no findings text provided)"


def _advisory_text(advisory: str | None, *, chat_id: str, worktree: str) -> str:
    raw = str(advisory or "").strip()
    if not raw or raw in {"ABSENT", "BILLING_DISABLED"}:
        return ""
    try:
        return _bounded_findings(raw, chat_id=chat_id, worktree=worktree)
    except ValidationError:
        return ""


def _packet_marker(body: str, head: str, marker: str) -> bool:
    return f"HEAD={head.strip().lower()}" in body and marker in body


def _remember(
    ledger: IssueAuditLedger,
    *,
    claim_key: str,
    action: str,
    repository: str,
    issue_number: int,
    verdict: str,
    target_sha: str,
    findings: str,
    attempt: int,
) -> None:
    ledger.dispositions[claim_key] = validate_stored_disposition(
        claim_key,
        {
            "action": action,
            "verdict": verdict,
            "target_sha": target_sha,
            "repository": repository,
            "issue_number": int(issue_number),
            "attempt": int(attempt),
            "findings": findings,
        },
    )


def apply_exact_head_disposition(
    *,
    claim: AuditClaim,
    ledger: IssueAuditLedger,
    ledger_sha: str | None,
    claim_store: Any,
    packet_store: MemoryPacketStore,
    repository: str,
    issue_number: int,
    branch: str,
    workstream: str,
    head: str,
    packets: list[WorkPacketSnapshot],
    descriptor: ProjectDescriptor,
    observed_host: str,
    expected_host: str,
    worktree_path: str,
    git_runner: GitRunner,
    spawn: Spawn,
    list_sessions: SessionList | None = None,
    list_processes: ProcessList | None = None,
    attempt: int = 1,
    gates: DeterministicGateSnapshot | None = None,
    bugbot_advisory: str | None = None,
    state_root: str | None = None,
    host_probe: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Apply one completed exact-HEAD claim. Idle callers make no mutation."""
    repo = normalize_github_repository(repository)
    target = require_exact_commit_sha(head, label="head")
    branch_name = str(branch or "").strip()
    workstream_name = str(workstream or "").strip()
    if not branch_name or not workstream_name:
        return _result("stale_packet")
    if claim.state != "completed" or claim.verdict not in {
        "PASS",
        "REWORK",
        "HUMAN_REQUIRED",
    }:
        return _result("stale_checkpoint")
    if (
        normalize_github_repository(claim.repository) != repo
        or int(claim.issue_number) != int(issue_number)
        or claim.branch != branch_name
        or claim.target_sha != target
    ):
        return _result("stale_head")
    expected_key = make_audit_claim_key(repo, issue_number, target)
    if claim.claim_key != expected_key:
        return _result("stale_checkpoint")
    if len(packets) != 1:
        return _result("ambiguous_packet")
    packet = packets[0]
    if (
        normalize_github_repository(packet.repository) != repo
        or int(packet.issue_number) != int(issue_number)
        or packet.branch != branch_name
        or packet.head != target
        or packet.status != "ACTIVE"
    ):
        return _result("stale_head")
    if normalize_github_repository(descriptor.repository) != repo:
        return _result("descriptor_refused")
    if descriptor.worktree != worktree_path:
        return _result("descriptor_refused")
    if str(observed_host or "").strip().lower() != str(expected_host or "").strip().lower():
        return _result("host_refused")
    try:
        external_root = require_external_state_root(
            state_root, [worktree_path, descriptor.worktree]
        )
    except ValidationError:
        return _result("state_root_refused")

    prior = ledger.dispositions.get(expected_key)
    if isinstance(prior, dict):
        try:
            prior = validate_stored_disposition(expected_key, prior)
        except ValidationError:
            return _result("disposition_refused", verdict=claim.verdict)
        ledger.dispositions[expected_key] = prior
        if str(prior.get("action") or "") in _TERMINAL_ACTIONS:
            return _result(
                "duplicate",
                verdict=str(prior.get("verdict") or ""),
                findings=str(prior.get("findings") or ""),
                packet_mutations=packet_store.mutations,
            )

    body, token = packet_store.load()
    meta_head = str(_packet_metadata_value(body, "LAST_VERIFIED_HEAD") or "").lower()
    meta_branch = _packet_metadata_value(body, "BRANCH")
    meta_repo = _packet_metadata_value(body, "TARGET_REPO")
    meta_stream = _packet_metadata_value(body, "WORKSTREAM")
    meta_status = _packet_metadata_value(body, "STATUS")
    try:
        canonical_repo = normalize_github_repository(meta_repo or "")
    except ValidationError:
        return _result("stale_packet")
    if (
        canonical_repo != repo
        or meta_branch != branch_name
        or meta_stream != workstream_name
        or meta_status != "ACTIVE"
        or meta_head != target
    ):
        return _result("stale_packet")

    if persistent_cursor_active(
        worktree_path,
        list_sessions=list_sessions,
        list_processes=list_processes,
    ):
        return _result("cursor_active_noop", verdict=claim.verdict)
    try:
        validate_clean_worktree_identity(
            worktree_path,
            repository=repo,
            branch=branch_name,
            expected_head=target,
            git_runner=git_runner,
        )
    except ValidationError as exc:
        return _result(
            "identity_refused",
            findings=redact_sensitive_audit_text(str(exc)),
        )

    chat_id = descriptor.cursor_chat_id
    try:
        findings = _bounded_findings(
            claim.findings, chat_id=chat_id, worktree=worktree_path
        )
    except ValidationError as exc:
        return _result("findings_refused", findings=redact_sensitive_audit_text(str(exc)))
    advisory = _advisory_text(bugbot_advisory, chat_id=chat_id, worktree=worktree_path)
    if advisory and "Bugbot advisory:" not in findings:
        findings = _bounded_findings(
            f"{findings}\nBugbot advisory: {advisory}",
            chat_id=chat_id,
            worktree=worktree_path,
        )

    if claim.verdict == "HUMAN_REQUIRED":
        _remember(
            ledger,
            claim_key=expected_key,
            repository=repo,
            issue_number=issue_number,
            action="human_required",
            verdict="HUMAN_REQUIRED",
            target_sha=target,
            findings=findings,
            attempt=attempt,
        )
        claim_store.save(ledger, expected_sha=ledger_sha)
        if chat_id in (ledger.dispositions[expected_key].get("findings") or ""):
            raise ValidationError("refusing to persist a Cursor chat id")
        return _result(
            "human_required",
            verdict="HUMAN_REQUIRED",
            findings=findings,
            packet_mutations=packet_store.mutations,
        )

    if claim.verdict == "PASS":
        if _packet_marker(body, target, "WORK_PACKET_MUTATION=GOVERNANCE_CHECKPOINT"):
            return _result("duplicate", verdict="PASS", findings=findings)
        if not gates_are_current(gates, head=target):
            return _result("no_advancement", verdict="PASS", findings=findings)
        rendered = render_pass_governance_work_packet_body(
            body,
            repository=repo,
            branch=branch_name,
            workstream=workstream_name,
            head=target,
            gate_summary="tests PASS; ci OK; review current; governance current",
            advisory=advisory,
        )
        if chat_id in rendered:
            raise ValidationError("refusing to project Cursor chat id to GitHub")
        again, token2 = packet_store.load()
        if again != body or token2 != token:
            return _result("packet_conflict", verdict="PASS")
        packet_store.cas_save(rendered, expected_body=body, expected_token=token)
        _remember(
            ledger,
            claim_key=expected_key,
            repository=repo,
            issue_number=issue_number,
            action="pass_checkpoint",
            verdict="PASS",
            target_sha=target,
            findings=findings,
            attempt=attempt,
        )
        claim_store.save(ledger, expected_sha=ledger_sha)
        return _result(
            "pass_checkpoint",
            verdict="PASS",
            findings=findings,
            packet_mutations=packet_store.mutations,
        )

    if _packet_marker(body, target, "WORK_PACKET_MUTATION=DISPATCH_BLOCKED"):
        return _result("duplicate", verdict="REWORK", findings=findings)

    collapsed = " ".join(findings.split())
    prompt = (
        "Address the exact-HEAD REWORK findings on this worktree. "
        "Re-run the affected deterministic checks. "
        "Do not merge, release, or deploy. "
        f"Findings: {collapsed}"
    )

    def _compensate(reason: str) -> dict[str, Any]:
        safe_reason = redact_absolute_paths(redact_sensitive_audit_text(reason or "dispatch blocked"))
        if chat_id:
            safe_reason = safe_reason.replace(chat_id, "[redacted-chat]")
        current, current_token = packet_store.load()
        try:
            blocked = render_dispatch_blocked_work_packet_body(
                current,
                repository=repo,
                branch=branch_name,
                workstream=workstream_name,
                findings=findings,
                attempt=int(attempt),
                head=target,
                reason=safe_reason or "dispatch blocked",
            )
        except ValidationError:
            _remember(
                ledger,
                claim_key=expected_key,
            repository=repo,
            issue_number=issue_number,
                action="dispatch_blocked",
                verdict="HUMAN_REQUIRED",
                target_sha=target,
                findings=findings,
                attempt=attempt,
            )
            return _result(
                "compensation_failed",
                verdict="HUMAN_REQUIRED",
                findings=findings,
                packet_mutations=packet_store.mutations,
            )
        if chat_id in blocked:
            raise ValidationError("refusing to project Cursor chat id to GitHub")
        try:
            packet_store.cas_save(
                blocked, expected_body=current, expected_token=current_token
            )
        except (CheckpointCasConflict, ValidationError):
            _remember(
                ledger,
                claim_key=expected_key,
            repository=repo,
            issue_number=issue_number,
                action="dispatch_blocked",
                verdict="HUMAN_REQUIRED",
                target_sha=target,
                findings=findings,
                attempt=attempt,
            )
            try:
                claim_store.save(ledger, expected_sha=ledger_sha)
            except CheckpointCasConflict:
                pass
            return _result(
                "compensation_failed",
                verdict="HUMAN_REQUIRED",
                findings=findings,
                packet_mutations=packet_store.mutations,
            )
        _remember(
            ledger,
            claim_key=expected_key,
            repository=repo,
            issue_number=issue_number,
            action="dispatch_blocked",
            verdict="HUMAN_REQUIRED",
            target_sha=target,
            findings=findings,
            attempt=attempt,
        )
        claim_store.save(ledger, expected_sha=ledger_sha)
        return _result(
            "dispatch_blocked",
            verdict="HUMAN_REQUIRED",
            findings=findings,
            packet_mutations=packet_store.mutations,
        )

    def _dispatch_and_record() -> dict[str, Any]:
        nonlocal ledger_sha
        if isinstance(prior, dict) and str(prior.get("action") or "") == "dispatch_started":
            return _compensate("prior cursor resume effect is unconfirmed")
        _remember(
            ledger,
            claim_key=expected_key,
            repository=repo,
            issue_number=issue_number,
            action="dispatch_started",
            verdict="REWORK",
            target_sha=target,
            findings=findings,
            attempt=attempt,
        )
        try:
            ledger_sha = claim_store.save(ledger, expected_sha=ledger_sha)
        except CheckpointCasConflict:
            return _result(
                "disposition_persist_failed",
                verdict="REWORK",
                findings=findings,
                packet_mutations=packet_store.mutations,
            )
        config = HostWorkerConfig(
            state_root=external_root,
            host_id=normalize_host_id(expected_host),
            projects=(descriptor,),
        )
        probe = host_probe or actual_host_id
        try:
            outcome = run_once(
                config=config,
                resume_requested=True,
                repository=repo,
                canonical_branch=branch_name,
                expected_head=target,
                prompt=prompt,
                git_runner=git_runner,
                spawn=spawn,
                host_probe=probe,
                list_sessions=list_sessions,
                list_processes=list_processes,
            )
        except ValidationError as exc:
            return _compensate(str(exc))
        calls = int(outcome.get("cursor_calls") or 0)
        if outcome.get("action") == "resumed" and calls > 0:
            _remember(
                ledger,
                claim_key=expected_key,
                repository=repo,
                issue_number=issue_number,
                action="redispatched",
                verdict="REWORK",
                target_sha=target,
                findings=findings,
                attempt=attempt,
            )
            try:
                claim_store.save(ledger, expected_sha=ledger_sha)
            except CheckpointCasConflict:
                return _result(
                    "dispatch_unconfirmed",
                    cursor_calls=calls,
                    packet_mutations=packet_store.mutations,
                    verdict="REWORK",
                    findings=findings,
                )
            return _result(
                "redispatched",
                cursor_calls=calls,
                packet_mutations=packet_store.mutations,
                verdict="REWORK",
                findings=findings,
            )
        return _compensate(str(outcome.get("action") or "dispatch blocked"))

    if _packet_marker(body, target, "WORK_PACKET_MUTATION=PENDING_DISPATCH"):
        return _dispatch_and_record()

    rendered = render_rework_work_packet_body(
        body,
        repository=repo,
        branch=branch_name,
        workstream=workstream_name,
        findings=findings,
        attempt=int(attempt),
        head=target,
    )
    if chat_id in rendered:
        raise ValidationError("refusing to project Cursor chat id to GitHub")
    again, token2 = packet_store.load()
    if again != body or token2 != token:
        return _result("packet_conflict", verdict="REWORK", findings=findings)
    packet_store.cas_save(rendered, expected_body=body, expected_token=token)
    return _dispatch_and_record()


class GitHubIssuePacketStore:
    """Canonical Work Packet CAS through ``GitHubWorkPacketAdapter``.

    Body rendering stays in the shared render functions. This store only
    loads and commits through the adapter's view, author trust, uniqueness,
    and recheck.
    """

    def __init__(
        self,
        adapter: GitHubWorkPacketAdapter,
        *,
        repository: str,
        issue_number: int,
        branch: str,
    ) -> None:
        self._adapter = adapter
        self._repository = normalize_github_repository(repository)
        self._issue_number = int(issue_number)
        self._branch = branch.strip()
        self.mutations = 0

    def load(self) -> tuple[str, str]:
        payload = self._adapter._view_issue(self._repository, self._issue_number)
        self._adapter._assert_ai_work_issue(
            payload, issue_number=self._issue_number
        )
        self._adapter._require_trusted_issue_author(self._repository, payload)
        body = str(payload.get("body") or "")
        token = str(payload.get("updatedAt") or payload.get("updated_at") or "")
        return body, token

    def cas_save(self, body: str, *, expected_body: str, expected_token: str) -> None:
        try:
            self._adapter.commit_unchanged_body(
                repository=self._repository,
                issue_number=self._issue_number,
                branch=self._branch,
                new_body=body,
                expected_body=expected_body,
                expected_updated_at=expected_token,
            )
        except ValidationError as exc:
            if "changed during mutation" in str(exc):
                raise CheckpointCasConflict(str(exc)) from exc
            raise
        self.mutations += 1


def run_completed_audit_disposition(
    *,
    host_config: HostWorkerConfig,
    claim_store: Any,
    packet_adapter: GitHubWorkPacketAdapter,
    issue_number: int,
    branch: str,
    workstream: str,
    head: str,
    packets: list[WorkPacketSnapshot],
    git_runner: GitRunner,
    spawn: Spawn,
    list_sessions: SessionList | None = None,
    list_processes: ProcessList | None = None,
    host_probe: Callable[[], str] | None = None,
    attempt: int = 1,
    gates: DeterministicGateSnapshot | None = None,
    bugbot_advisory: str | None = None,
) -> dict[str, Any]:
    """Product entry: completed claim, canonical packet, locked host resume."""
    probe = host_probe or actual_host_id
    observed = normalize_host_id(probe())
    if len(host_config.projects) != 1 and not packets:
        return _result("ambiguous_packet")
    repository = packets[0].repository if packets else host_config.projects[0].repository
    descriptor = _select_descriptor(host_config.projects, repository=repository)
    ledger, ledger_sha = claim_store.load(int(issue_number))
    if ledger is None:
        return _result("stale_checkpoint")
    key = make_audit_claim_key(repository, int(issue_number), head)
    claim = ledger.claims.get(key)
    if claim is None:
        return _result("stale_checkpoint")
    packet_store = GitHubIssuePacketStore(
        packet_adapter,
        repository=repository,
        issue_number=int(issue_number),
        branch=branch,
    )
    return apply_exact_head_disposition(
        claim=claim,
        ledger=ledger,
        ledger_sha=ledger_sha,
        claim_store=claim_store,
        packet_store=packet_store,
        repository=repository,
        issue_number=int(issue_number),
        branch=branch,
        workstream=workstream,
        head=head,
        packets=packets,
        descriptor=descriptor,
        observed_host=observed,
        expected_host=host_config.host_id,
        worktree_path=descriptor.worktree,
        git_runner=git_runner,
        spawn=spawn,
        list_sessions=list_sessions,
        list_processes=list_processes,
        attempt=attempt,
        gates=gates,
        bugbot_advisory=bugbot_advisory,
        state_root=host_config.state_root,
        host_probe=probe,
    )
