"""Descriptor-driven host-local supervise-once pass (Issue #47 slice E1).

Cron may invoke this once per host. Project selection comes from the
host-local descriptor. Issue, branch, workstream, and exact HEAD come from
the unique trusted ACTIVE GitHub Work Packet. Chat IDs, worktrees, state
roots, and credentials stay on the host.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from atlas.audit_claim import (
    AuditBudget,
    WorkPacketSnapshot,
    make_audit_claim_key,
    run_exact_head_audit,
)
from atlas.audit_disposition import run_completed_audit_disposition
from atlas.chat_audit import require_exact_commit_sha
from atlas.codex_audit import collect_audit_evidence_bundle
from atlas.host_worker import (
    HostProbe,
    HostWorkerConfig,
    ProcessList,
    SessionList,
    Spawn,
    actual_host_id,
    host_worker_run_lock,
    normalize_host_id,
    persistent_cursor_active,
    require_external_state_root,
    spawn_agent_argv,
)
from atlas.provenance import ValidationError
from atlas.secrets import redact_sensitive_audit_text
from atlas.work_controller import (
    CompletionEvent,
    GitHubWorkPacketAdapter,
    GitRunner,
    WorkstreamRecord,
    default_git_runner,
    normalize_github_repository,
    redact_absolute_paths,
    validate_clean_worktree_identity,
)


def _project_result(
    repository: str,
    action: str,
    *,
    issue_number: int | None = None,
    auditor_calls: int = 0,
    cursor_calls: int = 0,
    verdict: str | None = None,
    findings: str | None = None,
    chat_id: str = "",
    worktree: str = "",
) -> dict[str, Any]:
    text = findings
    if text:
        text = redact_absolute_paths(
            redact_sensitive_audit_text(text, max_chars=500)
        )
        if chat_id:
            text = text.replace(chat_id, "[redacted-chat]")
        if worktree:
            text = text.replace(worktree, "[redacted-worktree]")
    return {
        "repository": repository,
        "issue_number": issue_number,
        "action": action,
        "auditor_calls": int(auditor_calls),
        "cursor_calls": int(cursor_calls),
        "verdict": verdict,
        "findings": text,
    }


def _default_evidence(
    packet: dict[str, Any],
    worktree: str,
    *,
    git_runner: GitRunner,
) -> dict[str, Any]:
    identity = validate_clean_worktree_identity(
        worktree,
        repository=str(packet["repository"]),
        branch=str(packet["branch"]),
        expected_head=str(packet["head"]),
        git_runner=git_runner,
    )
    event = CompletionEvent(
        event_id=(
            f"supervise-{int(packet['issue_number'])}-"
            f"{str(packet['head'])[:12]}"
        ),
        workstream=str(packet["workstream"]),
        issue_number=int(packet["issue_number"]),
        branch=str(packet["branch"]),
        head=str(packet["head"]),
        attempt=1,
    )
    record = WorkstreamRecord(
        workstream=str(packet["workstream"]),
        repository=str(packet["repository"]),
        issue_number=int(packet["issue_number"]),
        branch=str(packet["branch"]),
        worktree_path=worktree,
        expected_head=str(packet["head"]),
        state="AUDIT",
        attempt=1,
    )
    audit_base = str(packet.get("audit_base") or "").strip() or None
    bundle = collect_audit_evidence_bundle(
        event,
        record,
        identity=identity,
        git_runner=git_runner,
        base_ref=audit_base,
    )
    if not isinstance(bundle, dict):
        raise ValidationError("audit evidence bundle must be an object")
    return bundle


def _audit_bases_match(listed: dict[str, Any], fresh: dict[str, Any]) -> bool:
    left = str(listed.get("audit_base") or "").strip()
    right = str(fresh.get("audit_base") or "").strip()
    if not left and not right:
        return True
    try:
        return require_exact_commit_sha(left, label="audit_base") == require_exact_commit_sha(
            right, label="audit_base"
        )
    except ValidationError:
        return False


def _same_packet(listed: dict[str, Any], fresh: dict[str, Any]) -> bool:
    try:
        listed_head = require_exact_commit_sha(
            str(listed.get("head") or ""), label="listed.head"
        )
    except ValidationError:
        return False
    return (
        int(listed["issue_number"]) == int(fresh["issue_number"])
        and str(listed.get("branch") or "") == str(fresh["branch"])
        and str(listed.get("workstream") or "") == str(fresh["workstream"])
        and listed_head == str(fresh["head"])
        and str(listed.get("status") or "") == str(fresh["status"])
        and _audit_bases_match(listed, fresh)
    )


def _audit_base_is_ancestor(
    git_runner: GitRunner, worktree: str, base: str, head: str
) -> bool:
    try:
        git_runner(
            ["git", "merge-base", "--is-ancestor", base, head],
            str(Path(worktree).resolve()),
        )
    except ValidationError:
        return False
    return True


def _canonical_packet_unchanged(
    packet_adapter: GitHubWorkPacketAdapter,
    repository: str,
    expected: dict[str, Any],
) -> bool:
    """Re-read trusted ACTIVE state after a slow pre-effect step.

    Discovery and the issue view must still name the same issue, branch,
    workstream, HEAD, and ACTIVE status. Any miss fails closed.
    """
    try:
        discovered = packet_adapter.discover_trusted_active_packets(repository)
        if len(discovered) != 1 or not _same_packet(discovered[0], expected):
            return False
        confirmed = packet_adapter.reread_trusted_active_packet(
            repository, int(expected["issue_number"])
        )
    except ValidationError:
        return False
    return _same_packet(expected, confirmed)


def _snapshot(packet: dict[str, Any]) -> WorkPacketSnapshot:
    return WorkPacketSnapshot(
        repository=str(packet["repository"]),
        issue_number=int(packet["issue_number"]),
        branch=str(packet["branch"]),
        head=str(packet["head"]),
        status="ACTIVE",
    )


def _supervise_project(
    *,
    config: HostWorkerConfig,
    descriptor_repository: str,
    worktree: str,
    chat_id: str,
    packet_adapter: GitHubWorkPacketAdapter,
    claim_store_for: Callable[[str], Any],
    auditor: Any,
    evidence_for: Callable[[dict[str, Any], str], dict],
    budget: AuditBudget,
    git_runner: GitRunner,
    spawn: Spawn,
    host_probe: HostProbe,
    list_sessions: SessionList | None,
    list_processes: ProcessList | None,
    api_key_env: str,
    now: Callable[[], float] | None,
) -> dict[str, Any]:
    repository = normalize_github_repository(descriptor_repository)
    discovered = packet_adapter.discover_trusted_active_packets(repository)
    if not discovered:
        return _project_result(repository, "idle", chat_id=chat_id, worktree=worktree)
    if len(discovered) != 1:
        return _project_result(
            repository,
            "ambiguous_packet",
            chat_id=chat_id,
            worktree=worktree,
        )
    listed = discovered[0]
    fresh = packet_adapter.reread_trusted_active_packet(
        repository, int(listed["issue_number"])
    )
    if not _same_packet(listed, fresh):
        return _project_result(
            repository,
            "canonical_drift",
            issue_number=int(fresh["issue_number"]),
            chat_id=chat_id,
            worktree=worktree,
        )
    if persistent_cursor_active(
        worktree,
        list_sessions=list_sessions,
        list_processes=list_processes,
    ):
        return _project_result(
            repository,
            "cursor_active_noop",
            issue_number=int(fresh["issue_number"]),
            chat_id=chat_id,
            worktree=worktree,
        )
    try:
        validate_clean_worktree_identity(
            worktree,
            repository=repository,
            branch=str(fresh["branch"]),
            expected_head=str(fresh["head"]),
            git_runner=git_runner,
        )
    except ValidationError as exc:
        return _project_result(
            repository,
            "identity_refused",
            issue_number=int(fresh["issue_number"]),
            findings=str(exc),
            chat_id=chat_id,
            worktree=worktree,
        )
    audit_base = str(fresh.get("audit_base") or "").strip()
    if audit_base and not _audit_base_is_ancestor(
        git_runner, worktree, audit_base, str(fresh["head"])
    ):
        return _project_result(
            repository, "audit_base_refused", issue_number=int(fresh["issue_number"])
        )

    store = claim_store_for(repository)
    issue_number = int(fresh["issue_number"])
    head = str(fresh["head"])
    ledger, _ledger_sha = store.load(issue_number)
    key = make_audit_claim_key(repository, issue_number, head)
    claim = ledger.claims.get(key) if ledger is not None else None
    if claim is not None and claim.state == "completed":
        outcome = run_completed_audit_disposition(
            host_config=config,
            claim_store=store,
            packet_adapter=packet_adapter,
            issue_number=issue_number,
            branch=str(fresh["branch"]),
            workstream=str(fresh["workstream"]),
            head=head,
            packets=[_snapshot(fresh)],
            git_runner=git_runner,
            spawn=spawn,
            list_sessions=list_sessions,
            list_processes=list_processes,
            host_probe=host_probe,
            gates=None,
        )
        return _project_result(
            repository,
            str(outcome.get("action") or "disposition"),
            issue_number=issue_number,
            cursor_calls=int(outcome.get("cursor_calls") or 0),
            verdict=outcome.get("verdict"),
            findings=outcome.get("findings"),
            chat_id=chat_id,
            worktree=worktree,
        )

    bundle = evidence_for(fresh, worktree)
    if not _canonical_packet_unchanged(
        packet_adapter, repository, fresh
    ):
        return _project_result(
            repository,
            "canonical_drift",
            issue_number=issue_number,
            chat_id=chat_id,
            worktree=worktree,
        )
    before_calls = getattr(auditor, "calls", None)
    audited = run_exact_head_audit(
        audit_requested=True,
        repository=repository,
        issue_number=issue_number,
        branch=str(fresh["branch"]),
        head=head,
        packets=[_snapshot(fresh)],
        worktree_path=worktree,
        store=store,
        auditor=auditor,
        evidence_bundle=bundle,
        budget=budget,
        git_runner=git_runner,
        list_sessions=list_sessions,
        list_processes=list_processes,
        now=now,
        api_key_env=api_key_env,
    )
    if before_calls is None:
        auditor_calls = int(audited.get("auditor_calls") or 0)
    else:
        auditor_calls = int(getattr(auditor, "calls")) - int(before_calls)
    return _project_result(
        repository,
        str(audited.get("action") or "audit"),
        issue_number=issue_number,
        auditor_calls=auditor_calls,
        verdict=audited.get("verdict"),
        findings=audited.get("findings"),
        chat_id=chat_id,
        worktree=worktree,
    )


def supervise_once(
    *,
    config: HostWorkerConfig,
    packet_adapter: GitHubWorkPacketAdapter,
    claim_store_for: Callable[[str], Any],
    auditor: Any,
    evidence_for: Callable[[dict[str, Any], str], dict] | None = None,
    budget: AuditBudget | None = None,
    git_runner: GitRunner | None = None,
    spawn: Spawn | None = None,
    host_probe: HostProbe | None = None,
    list_sessions: SessionList | None = None,
    list_processes: ProcessList | None = None,
    api_key_env: str = "OPENAI_API_KEY",
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """One flocked pass over the descriptor. At most one effect per project."""
    probe = host_probe or actual_host_id
    if normalize_host_id(probe()) != config.host_id:
        return {
            "action": "host_refused",
            "auditor_calls": 0,
            "cursor_calls": 0,
            "model_calls": 0,
            "projects": [],
        }
    root = require_external_state_root(
        config.state_root, [item.worktree for item in config.projects]
    )
    runner = git_runner or default_git_runner
    limits = budget or AuditBudget(per_run_hard_usd=1.0, monthly_hard_usd=25.0)
    collect = evidence_for or (
        lambda packet, worktree: _default_evidence(
            packet, worktree, git_runner=runner
        )
    )

    def default_spawn(argv: list[str], cwd: str) -> int:
        return spawn_agent_argv(
            argv, cwd, timeout_sec=config.cursor_resume_timeout_sec
        )

    effect_spawn = spawn or default_spawn
    projects: list[dict[str, Any]] = []
    with host_worker_run_lock(Path(root) / "supervise-once.lock"):
        for descriptor in config.projects:
            try:
                row = _supervise_project(
                    config=config,
                    descriptor_repository=descriptor.repository,
                    worktree=descriptor.worktree,
                    chat_id=descriptor.cursor_chat_id,
                    packet_adapter=packet_adapter,
                    claim_store_for=claim_store_for,
                    auditor=auditor,
                    evidence_for=collect,
                    budget=limits,
                    git_runner=runner,
                    spawn=effect_spawn,
                    host_probe=probe,
                    list_sessions=list_sessions,
                    list_processes=list_processes,
                    api_key_env=api_key_env,
                    now=now,
                )
            except ValidationError as exc:
                row = _project_result(
                    descriptor.repository,
                    "refused",
                    findings=str(exc),
                    chat_id=descriptor.cursor_chat_id,
                    worktree=descriptor.worktree,
                )
            projects.append(row)
    auditor_calls = sum(int(item["auditor_calls"]) for item in projects)
    cursor_calls = sum(int(item["cursor_calls"]) for item in projects)
    return {
        "action": "supervised",
        "auditor_calls": auditor_calls,
        "cursor_calls": cursor_calls,
        "model_calls": auditor_calls,
        "projects": projects,
    }
