"""Minimal Atlas Phase 1 operator CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from atlas.chat_audit import (
    ChatAuditController,
    ExternalEvidenceUnitExecutor,
    FakeBrowserRolloverProvider,
    FileWorkPacketHandoff,
    FixedCoordinationRefresher,
    FixedUnitExecutor,
    StagehandRolloverProvider,
)
from atlas.chat_audit_github import (
    GitHubAIWorkHandoff,
    GitHubCoordinationRefresher,
    publish_active_checkpoint_pointer,
    resolve_checkpoint_store,
)
from atlas.codex_audit import CodexAuditProvider
from atlas.audit_claim import GitHubContentsClaimStore
from atlas.audit_disposition import run_completed_audit_disposition
from atlas.final_audit import AuditBudget, BoundedResponsesAuditProvider
from atlas.cursor_usage import (
    assert_content_free,
    build_github_reconciliation_snapshot,
    build_report,
    collect_github_packet_observations,
    collect_process_facts,
    cursor_capacity_input,
    identities_for_workspaces,
    live_sessions,
    load_context_advice,
    load_github_reconciliation_snapshot,
    load_packet_facts,
    load_worker_snapshot,
    parse_usage_csv,
    summary_report,
    summarize_usage,
)
from atlas.context_optimization import load_context_canary_report
from atlas.context_shadow import load_shadow_quality_binding
from atlas.context_learned import load_learned_canary_binding
from atlas.provider_broker import STRATEGIES
from atlas.provider_capability import CAPABILITY_NAMES
from atlas.provider_transition import (
    FAILURE_REASONS,
    load_provider_transition_candidates,
    plan_provider_transition,
)
from atlas.host_worker import load_host_worker_config, run_once
from atlas.instruction_governance import TRIGGERS
from atlas.supervisor import supervise_once
from atlas.data_protection import backup_data_root, restore_test
from atlas.schema_compat import rollback_data_root, upgrade_data_root
from atlas.ops import (
    assess_service_environment,
    prod_launch_contract,
    stage_unit,
    validate_prod_deployment_env,
)
from atlas.provenance import ValidationError
from atlas.readiness_authorization import authorize_github_single_effect_file
from atlas.readiness_graph import (
    MAX_NODES,
    parse_packet_selector,
    plan_readiness_file,
    plan_selected_packet_projections,
)
from atlas.lifecycle_intelligence import lifecycle_view_payload
from atlas.readiness_github import plan_github_reconciled_readiness_file
from atlas.semantic_retrieval import embedding_config_from_cli
from atlas.service import AtlasService
from atlas.work_controller import (
    AuditOnlyCursorDispatcher,
    AuditResult,
    FixedAuditAdapter,
    GitHubWorkPacketAdapter,
    OpenAIResponsesAuditAdapter,
    PtyPersistCursorDispatcher,
    RecordingCursorDispatcher,
    default_git_runner,
    RecordingWorkPacketAdapter,
    WorkController,
    drain_completion_inbox,
    enqueue_completion_event,
    load_completion_event,
)


def _default_data_root() -> Path:
    env = os.environ.get("ATLAS_DATA_ROOT", "").strip()
    if env:
        return Path(env)
    return Path.cwd() / ".atlas-data"


def _service(args: argparse.Namespace) -> AtlasService:
    return AtlasService(Path(args.data_root))


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _read_json_file(path: str, *, label: str, max_bytes: int = 1024 * 1024) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"{label} file is unsafe")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"{label} file is unreadable") from exc
    if len(raw) > max_bytes:
        raise ValidationError(f"{label} file exceeds bounded size")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} file is invalid JSON") from exc


def cmd_project_register(args: argparse.Namespace) -> int:
    svc = _service(args)
    project = svc.register_project(
        project_id=args.project_id,
        repository=args.repository,
        display_name=args.display_name,
        default_ref=args.ref,
        engineering_metadata_path=args.engineering_metadata_path,
        enabled=not args.disabled,
    )
    _print_json(asdict(project))
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    svc = _service(args)
    _print_json(svc.show_project(args.project_id))
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    svc = _service(args)
    _print_json([asdict(p) for p in svc.list_projects()])
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    svc = _service(args)
    source = svc.add_source(
        args.project_id,
        source_id=args.source_id,
        source_path=args.source_path,
        ref=args.ref,
        title=args.title,
        enabled=not args.disabled,
    )
    _print_json(asdict(source))
    return 0


def cmd_source_import(args: argparse.Namespace) -> int:
    svc = _service(args)
    source = svc.import_personal_markdown(
        args.project_id,
        source_id=args.source_id,
        source_path=args.source_path,
        title=args.title,
        enabled=not args.disabled,
    )
    _print_json(asdict(source))
    return 0


def cmd_source_list(args: argparse.Namespace) -> int:
    svc = _service(args)
    _print_json([asdict(s) for s in svc.list_sources(args.project_id)])
    return 0


def cmd_source_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).source_detail(args.project_id, args.source_id))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    svc = _service(args)
    records = svc.sync_project(args.project_id, token=args.token)
    _print_json([asdict(r) for r in records])
    if any(r.sync_state == "error" for r in records):
        return 2
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    svc = _service(args)
    records = svc.rebuild_project(args.project_id, token=args.token)
    _print_json([asdict(r) for r in records])
    if any(r.sync_state == "error" for r in records):
        return 2
    return 0


def cmd_adoption(args: argparse.Namespace) -> int:
    svc = _service(args)
    adoption = svc.read_adoption(args.project_id, token=args.token)
    _print_json(
        {
            "project_name": adoption.project_name,
            "engineering_system_version": adoption.engineering_system_version,
            "engineering_system_baseline": adoption.engineering_system_baseline,
            "engineering_system_mode": adoption.engineering_system_mode,
            "engineering_system_ci_mode": adoption.engineering_system_ci_mode,
            "source_path": adoption.source_path,
        }
    )
    return 0


def cmd_projections(args: argparse.Namespace) -> int:
    svc = _service(args)
    _print_json(svc.projection_records(args.project_id))
    return 0


def cmd_intelligence_overview(args: argparse.Namespace) -> int:
    project_ids = list(args.project_id or [])
    _print_json(_service(args).intelligence_overview(project_ids or None))
    return 0


def cmd_intelligence_decision(args: argparse.Namespace) -> int:
    project_ids = list(args.project_id or [])
    _print_json(_service(args).decision_detail(args.decision_id, project_ids or None))
    return 0


def cmd_intelligence_show(args: argparse.Namespace) -> int:
    svc = _service(args)
    _print_json(svc.project_intelligence(args.project_id))
    return 0


def cmd_personal_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).personal_knowledge_dashboard())
    return 0


def cmd_personal_search(args: argparse.Namespace) -> int:
    hits = _service(args).personal_search(args.project_id, args.query, limit=args.limit)
    _print_json([asdict(hit) for hit in hits])
    return 0


def cmd_providers_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).provider_dashboard())
    return 0


def cmd_providers_quality_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).provider_route_quality_dashboard())
    return 0


def cmd_providers_quality_publish(args: argparse.Namespace) -> int:
    _print_json(
        _service(args).publish_provider_route_quality(
            [Path(value) for value in args.observation]
        )
    )
    return 0


def cmd_concurrency_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).concurrency_dashboard())
    return 0


def cmd_concurrency_publish(args: argparse.Namespace) -> int:
    snapshot = _read_json_file(args.snapshot, label="concurrency snapshot")
    _print_json(_service(args).publish_concurrency_snapshot(snapshot))
    return 0


def cmd_concurrency_record_run(args: argparse.Namespace) -> int:
    observation = _read_json_file(args.observation, label="concurrency run observation")
    _print_json(_service(args).record_concurrency_run(observation))
    return 0


def cmd_instruction_governance_profile_build(args: argparse.Namespace) -> int:
    payload = _service(args).build_instruction_governance_profile(
        engineering_system_revision=args.engineering_system_revision,
        agent_base_path=Path(args.agent_base),
        behavior_scenarios_path=Path(args.behavior_scenarios),
        trigger_kind=args.trigger_kind,
        trigger_revision=args.trigger_revision,
        model_provider=args.model_provider,
        model_name=args.model_name,
        model_profile=args.model_profile,
        harness_id=args.harness_id,
        harness_revision=args.harness_revision,
    )
    _print_json(payload)
    return 0


def cmd_instruction_governance_candidate_change(args: argparse.Namespace) -> int:
    payload = _service(args).build_instruction_candidate_change(
        managed_path=args.path,
        candidate_path=Path(args.candidate),
    )
    _print_json(payload)
    return 0


def cmd_instruction_governance_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).instruction_governance_dashboard())
    return 0


def cmd_instruction_governance_route(args: argparse.Namespace) -> int:
    _print_json(_service(args).instruction_governance_routing())
    return 0


def cmd_instruction_governance_disposition_build(args: argparse.Namespace) -> int:
    _print_json(_service(args).build_instruction_governance_disposition(args.audit_identity))
    return 0


def cmd_instruction_governance_disposition_publish(args: argparse.Namespace) -> int:
    _print_json(_service(args).publish_instruction_governance_disposition(args.audit_identity))
    return 0


def cmd_instruction_governance_disposition_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).instruction_governance_disposition_dashboard())
    return 0


def cmd_instruction_governance_canary_record(args: argparse.Namespace) -> int:
    observation = _read_json_file(args.observation, label="instruction governance canary observation")
    _print_json(_service(args).record_instruction_governance_canary(observation))
    return 0


def cmd_instruction_governance_canary_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).instruction_governance_canary_dashboard())
    return 0


def cmd_instruction_governance_preflight(args: argparse.Namespace) -> int:
    profile = _read_json_file(args.profile, label="instruction governance profile")
    payload = _service(args).instruction_governance_preflight(
        profile=profile,
        agent_base_path=Path(args.agent_base),
        behavior_scenarios_path=Path(args.behavior_scenarios),
    )
    _print_json(payload)
    return 0


def cmd_instruction_governance_record(args: argparse.Namespace) -> int:
    profile = _read_json_file(args.profile, label="instruction governance profile")
    result = _read_json_file(args.result, label="instruction governance audit result")
    payload = _service(args).record_instruction_governance_audit(
        profile=profile,
        agent_base_path=Path(args.agent_base),
        behavior_scenarios_path=Path(args.behavior_scenarios),
        result=result,
    )
    _print_json(payload)
    return 0


def cmd_decision_plane_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).decision_plane_dashboard())
    return 0


def cmd_decision_plane_canary_readiness(args: argparse.Namespace) -> int:
    _print_json(_service(args).decision_canary_readiness())
    return 0


def cmd_decision_plane_canary_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).decision_canary_dashboard())
    return 0


def cmd_decision_plane_canary_publish(args: argparse.Namespace) -> int:
    request = _read_json_file(args.request, label="decision plane canary request")
    _print_json(_service(args).publish_decision_canary_admission(request))
    return 0


def cmd_decision_plane_limited_active_show(args: argparse.Namespace) -> int:
    _print_json(_service(args).decision_limited_active_dashboard())
    return 0


def cmd_decision_plane_append(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.observation).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError("decision plane observation file is invalid") from exc
    _print_json(_service(args).append_decision_plane_observation(payload))
    return 0


def cmd_decision_plane_context_candidates(args: argparse.Namespace) -> int:
    _print_json(
        _service(args).decision_plane_optional_context_candidates(
            list(args.optional_path or [])
        )
    )
    return 0


def cmd_decision_plane_check_candidates(args: argparse.Namespace) -> int:
    _print_json(
        _service(args).decision_plane_focused_check_candidates(
            list(args.changed_path or [])
        )
    )
    return 0


def cmd_providers_transition_preview(args: argparse.Namespace) -> int:
    payload = _service(args).provider_transition_preview(
        current_route_id=args.current_route,
        failure_reason=args.failure_reason,
        prior_failed_route_ids=list(args.prior_failed_route or []),
        max_attempts=args.max_attempts,
    )
    _print_json(payload)
    return 0


def cmd_providers_publish(args: argparse.Namespace) -> int:
    payload = _service(args).publish_provider_dashboard(
        candidate_paths=[Path(value) for value in args.candidate],
        observed_at=args.observed_at,
        required_capability=args.required_capability,
        strategy=args.strategy,
        max_evidence_age_seconds=args.max_evidence_age_seconds,
        route_set_path=Path(args.route_set) if args.route_set else None,
    )
    _print_json(payload)
    return 0


def cmd_search_all(args: argparse.Namespace) -> int:
    svc = _service(args)
    project_ids = list(args.project_id or [])
    if not project_ids:
        project_ids = [project.project_id for project in svc.list_projects() if project.enabled]
    _print_json(
        svc.search_across_projects(
            args.query,
            project_ids=project_ids,
            source_class=args.source_class,
            limit_per_project=args.limit_per_project,
        )
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    svc = _service(args)
    embedding = embedding_config_from_cli(
        endpoint=args.embedding_endpoint,
        model=args.embedding_model,
        query_prefix=args.embedding_query_prefix,
        document_prefix=args.embedding_document_prefix,
        timeout_seconds=args.embedding_timeout,
    )
    hits = svc.search(args.project_id, args.query, limit=args.limit, embedding=embedding)
    _print_json([asdict(hit) for hit in hits])
    return 0


def cmd_lifecycle_show(args: argparse.Namespace) -> int:
    project = _service(args).registry.get(args.project_id)
    _print_json(lifecycle_view_payload(Path(args.data_root), project.repository))
    return 0


def cmd_lifecycle_validate(args: argparse.Namespace) -> int:
    project = _service(args).registry.get(args.project_id)
    payload = lifecycle_view_payload(Path(args.data_root), project.repository)
    unavailable = [name for name in ("work", "ci", "tests", "release", "surface_reconciliation", "full_user_e2e") if payload[name]["state"] == "UNAVAILABLE"]
    _print_json({"project_id": args.project_id, "repository": project.repository, "valid": not unavailable, "unavailable_channels": unavailable})
    return 0 if not unavailable else 2


def cmd_web_serve(args: argparse.Namespace) -> int:
    from atlas.web_ui import serve_ui

    serve_ui(Path(args.data_root), host=args.host, port=args.port)
    return 0


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from atlas.mcp_config import resolve_mcp_serve_config
    from atlas.mcp_http import serve_mcp

    config = resolve_mcp_serve_config(
        data_root=Path(args.data_root),
        bind_host=args.host,
        port=args.port,
        resource_url=args.resource_url,
        issuer_url=args.issuer_url,
        introspection_url=args.introspection_url,
        introspection_client_id=args.introspection_client_id,
        introspection_client_secret_file=args.introspection_client_secret_file,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
    )
    serve_mcp(config)
    return 0


def _controller_from_args(args: argparse.Namespace) -> WorkController:
    """Build a controller with operator-selected adapters.

    Production default is Codex (ChatGPT-plan, no API key). Use
    `--audit-adapter fixed` explicitly for offline/deterministic verdicts.
    `--audit-adapter openai` is metadata-only and may be used with a recording
    packet adapter for audit-only runs. It cannot mutate the canonical GitHub
    Work Packet or spawn Cursor until it has the Codex evidence bundle.

    Production Work Packet adapter mutates the same GitHub `[AI Work]` Issue
    before REWORK dispatch. `RecordingWorkPacketAdapter` is offline/test-only.

    Non-runtime commands (`register` / `show` / `list`) do not carry audit or
    dispatch flags; they always get offline-safe recording adapters.
    """
    if not hasattr(args, "audit_adapter"):
        return WorkController(
            Path(args.data_root),
            audit=FixedAuditAdapter(AuditResult(verdict="PASS", findings="")),
            work_packet=RecordingWorkPacketAdapter(),
            dispatcher=RecordingCursorDispatcher(),
            enforce_worktree_identity=True,
        )

    adapter = getattr(args, "audit_adapter", "codex")
    if adapter == "codex":
        audit = CodexAuditProvider()
    elif adapter == "openai":
        audit = OpenAIResponsesAuditAdapter()
    elif adapter == "fixed":
        verdict = getattr(args, "audit_verdict", "PASS")
        findings = getattr(args, "audit_findings", "") or ""
        audit = FixedAuditAdapter(AuditResult(verdict=verdict, findings=findings))
    else:
        raise ValidationError(f"unsupported audit adapter: {adapter}")

    packet_choice = getattr(args, "work_packet_adapter", None)
    spawn = bool(getattr(args, "spawn_dispatch", False))
    if adapter == "openai" and (spawn or packet_choice == "github"):
        raise ValidationError(
            "OpenAI audit adapter is metadata-only and cannot mutate the "
            "canonical GitHub Work Packet or spawn Cursor until it has Codex "
            "evidence parity; use --work-packet-adapter recording without "
            "--spawn-dispatch"
        )
    if packet_choice is None:
        # ChatGPT-primary default is audit-only and must not mutate GitHub or
        # spawn Cursor. An explicit --spawn-dispatch selects the GitHub packet
        # path, where WorkController enforces IMPLEMENTER=CURSOR.
        packet_choice = "github" if spawn else "recording"
    if packet_choice == "github" and not spawn:
        raise ValidationError(
            "GitHub Work Packet mutation requires --spawn-dispatch "
            "(audit-only mode must pass --work-packet-adapter recording)"
        )
    if spawn and packet_choice != "github":
        raise ValidationError(
            "real --spawn-dispatch requires --work-packet-adapter github "
            "(RecordingWorkPacketAdapter cannot pair with PtyPersistCursorDispatcher)"
        )
    if packet_choice == "github":
        work_packet = GitHubWorkPacketAdapter()
    elif packet_choice == "recording":
        work_packet = RecordingWorkPacketAdapter()
    else:
        raise ValidationError(f"unsupported work packet adapter: {packet_choice}")

    if spawn:
        dispatcher = PtyPersistCursorDispatcher()
    else:
        # Do not pair recording packet adapters with a fake successful dispatcher:
        # audit-only REWORK must stop without claiming REWORK_DISPATCHED.
        dispatcher = AuditOnlyCursorDispatcher()
    return WorkController(
        Path(args.data_root),
        audit=audit,
        work_packet=work_packet,
        dispatcher=dispatcher,
        enforce_worktree_identity=True,
    )


def cmd_wc_register(args: argparse.Namespace) -> int:
    ctl = _controller_from_args(args)
    record = ctl.register_workstream(
        workstream=args.workstream,
        repository=args.repository,
        issue_number=args.issue_number,
        branch=args.branch,
        worktree_path=args.worktree,
        expected_head=args.expected_head,
        max_attempts=args.max_attempts,
    )
    _print_json(asdict(record))
    return 0


def cmd_wc_show(args: argparse.Namespace) -> int:
    ctl = _controller_from_args(args)
    _print_json(ctl.show(args.workstream))
    return 0


def cmd_wc_list(args: argparse.Namespace) -> int:
    ctl = _controller_from_args(args)
    _print_json(ctl.list_workstreams())
    return 0


def cmd_wc_completion(args: argparse.Namespace) -> int:
    ctl = _controller_from_args(args)
    event = load_completion_event(Path(args.event_file))
    _print_json(ctl.handle_completion(event))
    return 0


def cmd_wc_enqueue(args: argparse.Namespace) -> int:
    event = load_completion_event(Path(args.event_file))
    path = enqueue_completion_event(
        Path(args.data_root),
        event,
        filename=args.filename,
    )
    _print_json({"enqueued": str(path.name), "data_root": str(Path(args.data_root))})
    return 0


def cmd_wc_drain_inbox(args: argparse.Namespace) -> int:
    ctl = _controller_from_args(args)
    _print_json(drain_completion_inbox(ctl, Path(args.data_root)))
    return 0


def cmd_wc_reconcile(args: argparse.Namespace) -> int:
    ctl = _controller_from_args(args)
    _print_json(ctl.reconcile(args.workstream))
    return 0


def _chat_audit_from_args(args: argparse.Namespace) -> ChatAuditController:
    from atlas.work_controller import default_git_runner, normalize_github_repository

    adapter = getattr(args, "unit_adapter", "evidence")
    offline = adapter == "fixed" or bool(
        getattr(args, "allow_local_checkpoint", False)
    )
    worktree = getattr(args, "worktree", None)
    allow_trusted = bool(getattr(args, "allow_trusted_identity", False)) or offline
    if not worktree and not allow_trusted:
        # Production/default Chat path: derive identity from the worktree.
        worktree = str(Path.cwd())

    repository = getattr(args, "repository", None)
    if not repository and not offline and worktree:
        # Authoritative production identity: resolve owner/repo from origin.
        origin = default_git_runner(
            ["git", "remote", "get-url", "origin"], worktree
        ).strip()
        repository = normalize_github_repository(origin)

    store = resolve_checkpoint_store(
        data_root=Path(args.data_root),
        repository=repository,
        checkpoint_issue=getattr(args, "checkpoint_issue", None),
        require_github=not offline,
    )
    if adapter == "fixed":
        # Explicit offline/test mode only — never the operator default.
        executor = FixedUnitExecutor()
    else:
        evidence_payload = None
        evidence_file = getattr(args, "evidence_file", None)
        if evidence_file:
            evidence_payload = json.loads(
                Path(evidence_file).read_text(encoding="utf-8")
            )
            if not isinstance(evidence_payload, dict):
                raise ValidationError("evidence file must contain a JSON object")
        executor = ExternalEvidenceUnitExecutor(evidence_payload)
    handoff_mode = getattr(args, "handoff", "github")
    if handoff_mode == "local" and not offline:
        raise ValidationError(
            "local finding handoff requires offline or "
            "--allow-local-checkpoint mode"
        )
    if offline or handoff_mode == "local":
        handoff = FileWorkPacketHandoff(Path(args.data_root))
    else:
        if not repository:
            raise ValidationError(
                "repository is required for GitHub [AI Work] finding handoff"
            )
        handoff = GitHubAIWorkHandoff(repository=repository)
    provider = getattr(args, "rollover_provider", "fake")
    if provider == "stagehand":
        rollover = StagehandRolloverProvider(
            provider_approved=bool(getattr(args, "stagehand_approved", False))
        )
    else:
        rollover = FakeBrowserRolloverProvider()
    if offline:
        # FixedCoordinationRefresher is explicit offline/test only.
        coordination = FixedCoordinationRefresher()
    else:
        if not repository:
            raise ValidationError(
                "repository is required for GitHub coordination refresh; "
                "pass --repository or run inside a Git worktree with origin"
            )
        issue_number = getattr(store, "issue_number", None)
        coordination = GitHubCoordinationRefresher(
            repository=repository,
            work_packet_issue=int(issue_number) if issue_number else None,
            cwd=worktree,
        )
    return ChatAuditController(
        store,
        executor=executor,
        handoff=handoff,
        rollover=rollover,
        coordination=coordination,
        worktree_path=worktree,
        enforce_worktree_identity=bool(worktree),
        allow_trusted_identity=allow_trusted,
    )


def cmd_ca_init(args: argparse.Namespace) -> int:
    ctl = _chat_audit_from_args(args)
    result = ctl.initialize(
        repository=args.repository,
        branch=args.branch,
        head=args.head,
        include_release_readiness=bool(args.include_release_readiness),
        mode=args.mode,
    )
    # Publish repo-managed pointer so fresh Chat can discover Issue N.
    if not getattr(args, "allow_local_checkpoint", False):
        issue = getattr(args, "checkpoint_issue", None)
        if issue is None:
            issue = getattr(getattr(ctl, "store", None), "issue_number", None)
        if issue is not None:
            result["active_checkpoint_pointer"] = publish_active_checkpoint_pointer(
                repository=args.repository,
                issue_number=int(issue),
            )
    _print_json(result)
    return 0


def cmd_ca_show(args: argparse.Namespace) -> int:
    ctl = _chat_audit_from_args(args)
    _print_json(
        ctl.show(
            repository=getattr(args, "repository", None),
            branch=getattr(args, "branch", None),
            head=getattr(args, "head", None),
        )
    )
    return 0


def cmd_ca_run_slice(args: argparse.Namespace) -> int:
    ctl = _chat_audit_from_args(args)
    _print_json(
        ctl.run_slice(
            repository=args.repository,
            branch=args.branch,
            head=args.head,
            include_release_readiness=bool(args.include_release_readiness),
        )
    )
    return 0


def cmd_ca_resume_payload(args: argparse.Namespace) -> int:
    ctl = _chat_audit_from_args(args)
    _print_json(
        ctl.resume_instruction_payload(
            repository=getattr(args, "repository", None),
            branch=getattr(args, "branch", None),
            head=getattr(args, "head", None),
        )
    )
    return 0


def cmd_ca_mark_session(args: argparse.Namespace) -> int:
    ctl = _chat_audit_from_args(args)
    _print_json(
        ctl.mark_session(
            args.state,
            notes=args.notes or "",
            repository=getattr(args, "repository", None),
            branch=getattr(args, "branch", None),
            head=getattr(args, "head", None),
        )
    )
    return 0


def cmd_ca_rollover(args: argparse.Namespace) -> int:
    ctl = _chat_audit_from_args(args)
    _print_json(
        ctl.perform_rollover(
            repository=getattr(args, "repository", None),
            branch=getattr(args, "branch", None),
            head=getattr(args, "head", None),
        )
    )
    return 0


def _add_work_controller_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--audit-adapter",
        choices=["codex", "fixed", "openai"],
        default="codex",
        help="codex=default production; fixed=explicit offline; openai=optional API fallback",
    )
    parser.add_argument(
        "--audit-verdict",
        choices=["PASS", "REWORK", "HUMAN_REQUIRED"],
        default="PASS",
        help="Offline/fixed audit verdict for PoC CLI (tests use injected adapters)",
    )
    parser.add_argument("--audit-findings", default="")
    parser.add_argument(
        "--work-packet-adapter",
        choices=["github", "recording"],
        default=None,
        help=(
            "github=mutate canonical GitHub Work Packet before REWORK dispatch "
            "(production default for codex); recording=offline/test only "
            "(default when --audit-adapter fixed or openai)"
        ),
    )
    parser.add_argument(
        "--spawn-dispatch",
        action="store_true",
        help="Use PTY persist dispatcher to create a fresh /work-resume session",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="DataRelay Atlas operator surface",
    )
    parser.add_argument(
        "--data-root",
        default=str(_default_data_root()),
        help="Atlas durable data root (default: ./.atlas-data or ATLAS_DATA_ROOT)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    project = sub.add_parser("project", help="Project registry commands")
    project_sub = project.add_subparsers(dest="project_command", required=True)

    register = project_sub.add_parser("register", help="Register a project")
    register.add_argument("project_id")
    register.add_argument("--repository", required=True)
    register.add_argument("--display-name")
    register.add_argument("--ref", default="main")
    register.add_argument(
        "--engineering-metadata-path",
        default=".engineering/project.yaml",
    )
    register.add_argument("--disabled", action="store_true")
    register.set_defaults(func=cmd_project_register)

    show = project_sub.add_parser("show", help="Show one project")
    show.add_argument("project_id")
    show.set_defaults(func=cmd_project_show)

    plist = project_sub.add_parser("list", help="List projects")
    plist.set_defaults(func=cmd_project_list)

    source = sub.add_parser("source", help="Canonical source commands")
    source_sub = source.add_subparsers(dest="source_command", required=True)

    sadd = source_sub.add_parser("add", help="Add a canonical source path")
    sadd.add_argument("project_id")
    sadd.add_argument("source_id")
    sadd.add_argument("--path", dest="source_path", required=True)
    sadd.add_argument("--ref")
    sadd.add_argument("--title")
    sadd.add_argument("--disabled", action="store_true")
    sadd.set_defaults(func=cmd_source_add)

    simport = source_sub.add_parser(
        "import",
        help="Import a personal Markdown file into the Atlas snapshot root",
    )
    simport.add_argument("project_id")
    simport.add_argument("source_id")
    simport.add_argument("--path", dest="source_path", required=True)
    simport.add_argument("--title")
    simport.add_argument("--disabled", action="store_true")
    simport.set_defaults(func=cmd_source_import)

    slist = source_sub.add_parser("list", help="List sources for a project")
    slist.add_argument("project_id")
    slist.set_defaults(func=cmd_source_list)

    sshow = source_sub.add_parser("show", help="Show one registered source and validated projection detail")
    sshow.add_argument("project_id")
    sshow.add_argument("source_id")
    sshow.set_defaults(func=cmd_source_show)

    sync = sub.add_parser("sync", help="Sync one project")
    sync.add_argument("project_id")
    sync.add_argument("--token", default=None)
    sync.set_defaults(func=cmd_sync)

    rebuild = sub.add_parser("rebuild", help="Rebuild projections for one project")
    rebuild.add_argument("project_id")
    rebuild.add_argument("--token", default=None)
    rebuild.set_defaults(func=cmd_rebuild)

    adoption = sub.add_parser("adoption", help="Read Engineering System adoption metadata")
    adoption.add_argument("project_id")
    adoption.add_argument("--token", default=None)
    adoption.set_defaults(func=cmd_adoption)

    projections = sub.add_parser("projections", help="Show projection/provenance records")
    projections.add_argument("project_id")
    projections.set_defaults(func=cmd_projections)

    intelligence = sub.add_parser("intelligence", help="Show deterministic derived engineering intelligence")
    intelligence_sub = intelligence.add_subparsers(dest="intelligence_command", required=True)
    intelligence_overview = intelligence_sub.add_parser("overview", help="Show cross-project derived intelligence")
    intelligence_overview.add_argument("--project-id", action="append", default=[], help="Explicit project scope; repeat to include multiple projects")
    intelligence_overview.set_defaults(func=cmd_intelligence_overview)
    intelligence_decision = intelligence_sub.add_parser("decision", help="Show one ADR decision target and backlinks")
    intelligence_decision.add_argument("decision_id")
    intelligence_decision.add_argument("--project-id", action="append", default=[], help="Optional explicit project scope; repeat to include multiple projects")
    intelligence_decision.set_defaults(func=cmd_intelligence_decision)
    intelligence_show = intelligence_sub.add_parser("show", help="Show one project's derived intelligence")
    intelligence_show.add_argument("project_id")
    intelligence_show.set_defaults(func=cmd_intelligence_show)

    personal = sub.add_parser("personal", help="Read-only Personal Knowledge Plane")
    personal_sub = personal.add_subparsers(dest="personal_command", required=True)
    personal_show = personal_sub.add_parser("show", help="Show personal/reference source inventory and import status")
    personal_show.set_defaults(func=cmd_personal_show)
    personal_search = personal_sub.add_parser("search", help="Search only personal/reference projections in one project")
    personal_search.add_argument("project_id")
    personal_search.add_argument("query")
    personal_search.add_argument("--limit", type=int, default=8)
    personal_search.set_defaults(func=cmd_personal_search)

    providers = sub.add_parser("providers", help="Read-only provider capacity and broker state")
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    providers_show = providers_sub.add_parser("show", help="Show current provider capacity snapshot and advisory broker plan")
    providers_show.set_defaults(func=cmd_providers_show)
    providers_quality_show = providers_sub.add_parser(
        "quality-show",
        help="Show verified provider route outcome measurements",
    )
    providers_quality_show.set_defaults(func=cmd_providers_quality_show)
    providers_quality_publish = providers_sub.add_parser(
        "quality-publish",
        help="Publish a derived provider route quality snapshot from observation files",
    )
    providers_quality_publish.add_argument(
        "--observation",
        action="append",
        required=True,
        help="Provider route outcome observation JSON; repeat for multiple observations",
    )
    providers_quality_publish.set_defaults(func=cmd_providers_quality_publish)
    providers_preview = providers_sub.add_parser("transition-preview", help="Plan one read-only failover from the current provider snapshot")
    providers_preview.add_argument("--current-route", required=True)
    providers_preview.add_argument("--failure-reason", required=True, choices=sorted(FAILURE_REASONS))
    providers_preview.add_argument("--prior-failed-route", action="append", default=[])
    providers_preview.add_argument("--max-attempts", type=int, default=3)
    providers_preview.set_defaults(func=cmd_providers_transition_preview)
    providers_publish = providers_sub.add_parser("publish", help="Validate candidate files and publish a derived provider dashboard snapshot")
    providers_publish.add_argument("--candidate", action="append", required=True, help="Provider route candidate JSON file; repeat for multiple routes")
    providers_publish.add_argument("--route-set", default=None, help="Optional approved provider route-set JSON file")
    providers_publish.add_argument("--observed-at", required=True, help="UTC evaluation timestamp")
    providers_publish.add_argument("--required-capability", required=True, choices=sorted(CAPABILITY_NAMES))
    providers_publish.add_argument("--strategy", default="CAPABILITY_FIRST", choices=sorted(STRATEGIES))
    providers_publish.add_argument("--max-evidence-age-seconds", type=int, required=True)
    providers_publish.set_defaults(func=cmd_providers_publish)

    concurrency = sub.add_parser("concurrency", help="Provider-neutral measured concurrency admission")
    concurrency_sub = concurrency.add_subparsers(dest="concurrency_command", required=True)
    concurrency_show = concurrency_sub.add_parser("show", help="Show current concurrency admission plan and measurements")
    concurrency_show.set_defaults(func=cmd_concurrency_show)
    concurrency_publish = concurrency_sub.add_parser("publish", help="Publish a validated derived concurrency snapshot")
    concurrency_publish.add_argument("--snapshot", required=True)
    concurrency_publish.set_defaults(func=cmd_concurrency_publish)
    concurrency_record = concurrency_sub.add_parser("record-run", help="Record one measured run bound to the current plan")
    concurrency_record.add_argument("--observation", required=True)
    concurrency_record.set_defaults(func=cmd_concurrency_record_run)

    instruction_governance = sub.add_parser("instruction-governance", help="Model-aware instruction governance audit evidence")
    instruction_governance_sub = instruction_governance.add_subparsers(dest="instruction_governance_command", required=True)
    instruction_governance_show = instruction_governance_sub.add_parser("show", help="Show managed instruction inventory and audit history")
    instruction_governance_show.set_defaults(func=cmd_instruction_governance_show)
    instruction_governance_route = instruction_governance_sub.add_parser(
        "route",
        help="Show deterministic non-mutating candidate routing decision",
    )
    instruction_governance_route.set_defaults(func=cmd_instruction_governance_route)
    instruction_governance_disposition_build = instruction_governance_sub.add_parser(
        "disposition-build",
        help="Build one exact-audit disposition and digest-bound PR handoff without persistence",
    )
    instruction_governance_disposition_build.add_argument("--audit-identity", required=True)
    instruction_governance_disposition_build.set_defaults(func=cmd_instruction_governance_disposition_build)
    instruction_governance_disposition_publish = instruction_governance_sub.add_parser(
        "disposition-publish",
        help="Publish one derived exact-audit disposition/PR handoff",
    )
    instruction_governance_disposition_publish.add_argument("--audit-identity", required=True)
    instruction_governance_disposition_publish.set_defaults(func=cmd_instruction_governance_disposition_publish)
    instruction_governance_disposition_show = instruction_governance_sub.add_parser(
        "disposition-show",
        help="Show derived instruction-governance disposition/PR-handoff ledger",
    )
    instruction_governance_disposition_show.set_defaults(func=cmd_instruction_governance_disposition_show)
    instruction_governance_canary_record = instruction_governance_sub.add_parser(
        "canary-record",
        help="Record bounded canary evidence bound to one current PR_CANDIDATE handoff",
    )
    instruction_governance_canary_record.add_argument("--observation", required=True)
    instruction_governance_canary_record.set_defaults(func=cmd_instruction_governance_canary_record)
    instruction_governance_canary_show = instruction_governance_sub.add_parser(
        "canary-show",
        help="Show instruction-governance canary/adoption-gate evidence",
    )
    instruction_governance_canary_show.set_defaults(func=cmd_instruction_governance_canary_show)
    instruction_governance_profile = instruction_governance_sub.add_parser("profile-build", help="Build exact target/Engineering System/model/harness profile JSON")
    instruction_governance_profile.add_argument("--engineering-system-revision", required=True)
    instruction_governance_profile.add_argument("--agent-base", required=True)
    instruction_governance_profile.add_argument("--behavior-scenarios", required=True)
    instruction_governance_profile.add_argument("--trigger-kind", required=True, choices=sorted(TRIGGERS))
    instruction_governance_profile.add_argument("--trigger-revision", required=True)
    instruction_governance_profile.add_argument("--model-provider", required=True)
    instruction_governance_profile.add_argument("--model-name", required=True)
    instruction_governance_profile.add_argument("--model-profile", required=True)
    instruction_governance_profile.add_argument("--harness-id", required=True)
    instruction_governance_profile.add_argument("--harness-revision", required=True)
    instruction_governance_profile.set_defaults(func=cmd_instruction_governance_profile_build)
    instruction_governance_candidate = instruction_governance_sub.add_parser("candidate-change", help="Build digest-only change metadata for one managed surface")
    instruction_governance_candidate.add_argument("--path", required=True)
    instruction_governance_candidate.add_argument("--candidate", required=True)
    instruction_governance_candidate.set_defaults(func=cmd_instruction_governance_candidate_change)
    instruction_governance_preflight = instruction_governance_sub.add_parser("preflight", help="Bind exact target/profile/Engineering System reference identity")
    instruction_governance_preflight.add_argument("--profile", required=True)
    instruction_governance_preflight.add_argument("--agent-base", required=True)
    instruction_governance_preflight.add_argument("--behavior-scenarios", required=True)
    instruction_governance_preflight.set_defaults(func=cmd_instruction_governance_preflight)
    instruction_governance_record = instruction_governance_sub.add_parser("record", help="Record one advisory behavior/candidate-diff audit result")
    instruction_governance_record.add_argument("--profile", required=True)
    instruction_governance_record.add_argument("--agent-base", required=True)
    instruction_governance_record.add_argument("--behavior-scenarios", required=True)
    instruction_governance_record.add_argument("--result", required=True)
    instruction_governance_record.set_defaults(func=cmd_instruction_governance_record)

    decision_plane = sub.add_parser("decision-plane", help="Decision Plane shadow/replay evidence")
    decision_plane_sub = decision_plane.add_subparsers(dest="decision_plane_command", required=True)
    decision_plane_show = decision_plane_sub.add_parser("show", help="Show Decision Plane shadow/replay summary")
    decision_plane_show.set_defaults(func=cmd_decision_plane_show)
    decision_plane_canary_readiness = decision_plane_sub.add_parser(
        "canary-readiness",
        help="Show deterministic readiness for a bounded canary admission request",
    )
    decision_plane_canary_readiness.set_defaults(func=cmd_decision_plane_canary_readiness)
    decision_plane_canary_show = decision_plane_sub.add_parser(
        "canary-show",
        help="Show the current bounded Decision Plane canary admission snapshot",
    )
    decision_plane_canary_show.set_defaults(func=cmd_decision_plane_canary_show)
    decision_plane_canary_publish = decision_plane_sub.add_parser(
        "canary-publish",
        help="Publish a bounded Decision Plane canary admission request",
    )
    decision_plane_canary_publish.add_argument("--request", required=True)
    decision_plane_canary_publish.set_defaults(func=cmd_decision_plane_canary_publish)
    decision_plane_limited_active_show = decision_plane_sub.add_parser(
        "limited-active-show",
        help="Show bounded optional-context LIMITED_ACTIVE effect evidence",
    )
    decision_plane_limited_active_show.set_defaults(
        func=cmd_decision_plane_limited_active_show
    )
    decision_plane_append = decision_plane_sub.add_parser("append", help="Append one validated shadow/replay observation JSON")
    decision_plane_append.add_argument("--observation", required=True)
    decision_plane_append.set_defaults(func=cmd_decision_plane_append)
    decision_plane_context = decision_plane_sub.add_parser("context-candidates", help="Prepare optional-context candidates while preserving mandatory context")
    decision_plane_context.add_argument("--optional-path", action="append", default=[])
    decision_plane_context.set_defaults(func=cmd_decision_plane_context_candidates)
    decision_plane_checks = decision_plane_sub.add_parser("focused-check-candidates", help="Prepare affected focused-check candidates from .engineering/tests.yaml")
    decision_plane_checks.add_argument("--changed-path", action="append", required=True)
    decision_plane_checks.set_defaults(func=cmd_decision_plane_check_candidates)

    search_all = sub.add_parser(
        "search-all",
        help="Search an explicit/all-enabled project scope with engineering/personal filtering",
    )
    search_all.add_argument("query")
    search_all.add_argument("--project-id", action="append", default=[])
    search_all.add_argument(
        "--source-class",
        choices=["all", "engineering", "personal"],
        default="all",
    )
    search_all.add_argument("--limit-per-project", type=int, default=5)
    search_all.set_defaults(func=cmd_search_all)

    search = sub.add_parser(
        "search",
        help="Search one project's successful projections",
    )
    search.add_argument("project_id")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument(
        "--embedding-endpoint",
        default=None,
        help="Self-hosted TEI-compatible base URL. Omit to keep keyword-only search.",
    )
    search.add_argument(
        "--embedding-model",
        default=None,
        help="Model name sent to the embeddings endpoint.",
    )
    search.add_argument(
        "--embedding-query-prefix",
        default="",
        help="Optional prefix applied to the query before embedding. Default empty.",
    )
    search.add_argument(
        "--embedding-document-prefix",
        default="",
        help="Optional prefix applied to each projection before embedding. Default empty.",
    )
    search.add_argument(
        "--embedding-timeout",
        type=float,
        default=30.0,
        help="Embeddings HTTP timeout in seconds.",
    )
    search.set_defaults(func=cmd_search)

    lifecycle = sub.add_parser("lifecycle", help="Read-only normalized lifecycle evidence")
    lifecycle_sub = lifecycle.add_subparsers(dest="lifecycle_command", required=True)
    lifecycle_show = lifecycle_sub.add_parser("show", help="Show normalized lifecycle state")
    lifecycle_show.add_argument("project_id")
    lifecycle_show.set_defaults(func=cmd_lifecycle_show)
    lifecycle_validate = lifecycle_sub.add_parser("validate", help="Validate local lifecycle evidence for a project")
    lifecycle_validate.add_argument("project_id")
    lifecycle_validate.set_defaults(func=cmd_lifecycle_validate)

    web = sub.add_parser("web", help="Read-only Human UI")
    web_sub = web.add_subparsers(dest="web_command", required=True)
    web_serve = web_sub.add_parser("serve", help="Serve the loopback-only Web UI")
    web_serve.add_argument("--host", default="127.0.0.1")
    web_serve.add_argument("--port", type=int, default=8788)
    web_serve.set_defaults(func=cmd_web_serve)

    mcp = sub.add_parser("mcp", help="Authenticated MCP resource server")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser(
        "serve",
        help="Serve Streamable HTTP MCP over HTTPS",
        allow_abbrev=False,
    )
    mcp_serve.add_argument(
        "--host",
        default=None,
        help="Bind host. Default: ATLAS_MCP_BIND_HOST or 127.0.0.1",
    )
    mcp_serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port. Default: ATLAS_MCP_BIND_PORT or 8443",
    )
    mcp_serve.add_argument("--resource-url", default=None)
    mcp_serve.add_argument("--issuer-url", default=None)
    mcp_serve.add_argument("--introspection-url", default=None)
    mcp_serve.add_argument("--introspection-client-id", default=None)
    mcp_serve.add_argument(
        "--introspection-client-secret-file",
        default=None,
        help=(
            "Operator file containing the introspection client secret. "
            "When omitted, ATLAS_MCP_INTROSPECTION_CLIENT_SECRET is used."
        ),
    )
    mcp_serve.add_argument("--tls-cert", default=None)
    mcp_serve.add_argument("--tls-key", default=None)
    mcp_serve.set_defaults(func=cmd_mcp_serve)

    wc = sub.add_parser(
        "work-controller",
        help="Autonomous Work Controller PoC (ADR-0006)",
    )
    wc_sub = wc.add_subparsers(dest="wc_command", required=True)

    wc_reg = wc_sub.add_parser("register", help="Register one local workstream")
    wc_reg.add_argument("workstream")
    wc_reg.add_argument("--repository", required=True)
    wc_reg.add_argument("--issue-number", type=int, required=True)
    wc_reg.add_argument("--branch", required=True)
    wc_reg.add_argument("--worktree", required=True)
    wc_reg.add_argument("--expected-head", required=True)
    wc_reg.add_argument("--max-attempts", type=int, default=3)
    wc_reg.set_defaults(func=cmd_wc_register)

    wc_show = wc_sub.add_parser("show", help="Show one workstream controller record")
    wc_show.add_argument("workstream")
    wc_show.set_defaults(func=cmd_wc_show)

    wc_list = wc_sub.add_parser("list", help="List registered workstreams")
    wc_list.set_defaults(func=cmd_wc_list)

    wc_comp = wc_sub.add_parser(
        "completion",
        help="Handle one Cursor completion event JSON file",
    )
    wc_comp.add_argument("event_file")
    _add_work_controller_runtime_flags(wc_comp)
    wc_comp.set_defaults(func=cmd_wc_completion)

    wc_enq = wc_sub.add_parser(
        "enqueue-completion",
        help="Write a completion event into the local inbox for hook ingestion",
    )
    wc_enq.add_argument("event_file")
    wc_enq.add_argument("--filename", default=None)
    wc_enq.set_defaults(func=cmd_wc_enqueue)

    wc_drain = wc_sub.add_parser(
        "drain-inbox",
        help="Drain local completion-inbox events into the controller",
    )
    _add_work_controller_runtime_flags(wc_drain)
    wc_drain.set_defaults(func=cmd_wc_drain_inbox)

    wc_rec = wc_sub.add_parser(
        "reconcile",
        help="Reconcile unfinished audit state after controller restart",
    )
    wc_rec.add_argument("workstream", nargs="?")
    _add_work_controller_runtime_flags(wc_rec)
    wc_rec.set_defaults(func=cmd_wc_reconcile)

    ca = sub.add_parser(
        "chat-audit",
        help="Continuous Chat Audit Supervisor PoC (ADR-0007)",
    )
    ca_sub = ca.add_subparsers(dest="ca_command", required=True)

    ca_init = ca_sub.add_parser("init", help="Initialize Audit Control Packet")
    ca_init.add_argument("--repository", required=True)
    ca_init.add_argument("--branch", required=True)
    ca_init.add_argument("--head", default=None)
    ca_init.add_argument(
        "--mode",
        choices=["delta", "full"],
        default="delta",
    )
    ca_init.add_argument(
        "--include-release-readiness",
        action="store_true",
    )
    ca_init.add_argument("--worktree", default=None)
    ca_init.add_argument(
        "--checkpoint-issue",
        type=int,
        default=None,
        help=(
            "Workstream issue id keying Contents API checkpoint path "
            "(.atlas/chat-audit/checkpoints/issue-N.json) with blob-SHA CAS"
        ),
    )
    ca_init.add_argument(
        "--allow-local-checkpoint",
        action="store_true",
        help="Offline/test only: allow local chat-audit.json without GitHub",
    )
    ca_init.add_argument(
        "--allow-trusted-identity",
        action="store_true",
        help="Offline/test only: trust caller-supplied repo/branch/head",
    )
    ca_init.add_argument(
        "--handoff",
        choices=["github", "local"],
        default="github",
        help="github=canonical [AI Work] Issues (default); local=offline cache",
    )
    ca_init.set_defaults(func=cmd_ca_init)

    ca_show = ca_sub.add_parser("show", help="Show Audit Control Packet")
    ca_show.add_argument("--repository", default=None)
    ca_show.add_argument("--branch", default=None)
    ca_show.add_argument("--head", default=None)
    ca_show.add_argument("--worktree", default=None)
    ca_show.add_argument("--checkpoint-issue", type=int, default=None)
    ca_show.add_argument("--allow-local-checkpoint", action="store_true")
    ca_show.add_argument("--allow-trusted-identity", action="store_true")
    ca_show.add_argument(
        "--handoff", choices=["github", "local"], default="github"
    )
    ca_show.set_defaults(func=cmd_ca_show)

    ca_run = ca_sub.add_parser("run-slice", help="Run one bounded audit slice")
    ca_run.add_argument("--repository", default=None)
    ca_run.add_argument("--branch", default=None)
    ca_run.add_argument("--head", default=None)
    ca_run.add_argument("--include-release-readiness", action="store_true")
    ca_run.add_argument("--worktree", default=None)
    ca_run.add_argument("--checkpoint-issue", type=int, default=None)
    ca_run.add_argument("--allow-local-checkpoint", action="store_true")
    ca_run.add_argument("--allow-trusted-identity", action="store_true")
    ca_run.add_argument(
        "--handoff",
        choices=["github", "local"],
        default="github",
        help="github=canonical [AI Work] Issues (default); local=offline cache",
    )
    ca_run.add_argument(
        "--unit-adapter",
        choices=["evidence", "fixed"],
        default="evidence",
        help="evidence=require external COMPLETE evidence (default); "
        "fixed=explicit offline PASS synthesizer for tests only",
    )
    ca_run.add_argument(
        "--evidence-file",
        default=None,
        help="JSON evidence payload required by the default evidence adapter",
    )
    ca_run.set_defaults(func=cmd_ca_run_slice)

    ca_resume = ca_sub.add_parser(
        "resume-payload",
        help="Emit fresh-Chat resume payload from durable checkpoint only",
    )
    ca_resume.add_argument("--repository", default=None)
    ca_resume.add_argument("--branch", default=None)
    ca_resume.add_argument("--head", default=None)
    ca_resume.add_argument("--worktree", default=None)
    ca_resume.add_argument("--checkpoint-issue", type=int, default=None)
    ca_resume.add_argument("--allow-local-checkpoint", action="store_true")
    ca_resume.add_argument("--allow-trusted-identity", action="store_true")
    ca_resume.add_argument(
        "--handoff", choices=["github", "local"], default="github"
    )
    ca_resume.set_defaults(func=cmd_ca_resume_payload)

    ca_mark = ca_sub.add_parser("mark-session", help="Update session supervisor state")
    ca_mark.add_argument(
        "state",
        choices=["ACTIVE", "STALLED", "TIMEOUT", "ROLLOVER_REQUIRED", "RESUMED"],
    )
    ca_mark.add_argument("--notes", default="")
    ca_mark.add_argument("--repository", default=None)
    ca_mark.add_argument("--branch", default=None)
    ca_mark.add_argument("--head", default=None)
    ca_mark.add_argument("--worktree", default=None)
    ca_mark.add_argument("--checkpoint-issue", type=int, default=None)
    ca_mark.add_argument("--allow-local-checkpoint", action="store_true")
    ca_mark.add_argument("--allow-trusted-identity", action="store_true")
    ca_mark.add_argument(
        "--handoff", choices=["github", "local"], default="github"
    )
    ca_mark.set_defaults(func=cmd_ca_mark_session)

    ca_roll = ca_sub.add_parser(
        "rollover",
        help="Perform provider rollover without mutating audit truth",
    )
    ca_roll.add_argument(
        "--rollover-provider",
        choices=["fake", "stagehand"],
        default="fake",
    )
    ca_roll.add_argument(
        "--stagehand-approved",
        action="store_true",
        help="Required to attempt Stagehand path; still gated/unimplemented in PoC",
    )
    ca_roll.add_argument("--repository", default=None)
    ca_roll.add_argument("--branch", default=None)
    ca_roll.add_argument("--head", default=None)
    ca_roll.add_argument("--worktree", default=None)
    ca_roll.add_argument("--checkpoint-issue", type=int, default=None)
    ca_roll.add_argument("--allow-local-checkpoint", action="store_true")
    ca_roll.add_argument("--allow-trusted-identity", action="store_true")
    ca_roll.add_argument(
        "--handoff", choices=["github", "local"], default="github"
    )
    ca_roll.set_defaults(func=cmd_ca_rollover)

    hw = sub.add_parser(
        "host-worker",
        help="Host-local Cursor resume worker (Issue #47 slice A)",
    )
    hw_sub = hw.add_subparsers(dest="hw_command", required=True)
    hw_run = hw_sub.add_parser(
        "run-once",
        help="Locked idle pass with zero model and zero Cursor calls",
    )
    hw_run.add_argument(
        "--descriptors",
        required=True,
        help="Host-local descriptor file. Lock identity is derived from it.",
    )
    hw_run.set_defaults(func=cmd_host_worker_run_once)
    hw_dispose = hw_sub.add_parser(
        "dispose-once",
        help="Apply one completed exact-HEAD audit claim through the host worker",
    )
    hw_dispose.add_argument("--descriptors", required=True)
    hw_dispose.add_argument("--issue", type=int, required=True)
    hw_dispose.add_argument("--branch", required=True)
    hw_dispose.add_argument("--workstream", required=True)
    hw_dispose.add_argument("--head", required=True)
    hw_dispose.set_defaults(func=cmd_host_worker_dispose_once)
    hw_supervise = hw_sub.add_parser(
        "supervise-once",
        help="One descriptor-driven pass: discover ACTIVE packets, audit or dispose",
    )
    hw_supervise.add_argument(
        "--descriptors",
        required=True,
        help="Host-local descriptor file. Issue numbers are not accepted here.",
    )
    hw_supervise.set_defaults(func=cmd_host_worker_supervise_once)

    usage = sub.add_parser(
        "usage",
        help="Read-only Cursor worker, usage, and provider-capacity evidence",
    )
    usage_sub = usage.add_subparsers(dest="usage_command", required=True)
    usage_inventory = usage_sub.add_parser(
        "inventory",
        help="Content-free resident worker inventory",
    )
    usage_inventory.add_argument(
        "--packet-facts",
        default=None,
        help="Optional JSON packet facts. Titles and bodies are not accepted.",
    )
    usage_inventory.add_argument(
        "--github-reconcile",
        action="store_true",
        help="Read trusted Work Packet and PR facts from GitHub without mutation.",
    )
    usage_inventory.add_argument(
        "--repository",
        action="append",
        default=[],
        help="Repository to reconcile even when no local resident worker identifies it.",
    )
    usage_inventory.add_argument(
        "--host-id",
        default=None,
        help="Bounded source host label for exporting a worker snapshot.",
    )
    usage_inventory.set_defaults(func=cmd_usage_inventory)
    usage_github_snapshot = usage_sub.add_parser(
        "github-snapshot",
        help="Export bounded content-free GitHub lifecycle facts for central reporting",
    )
    usage_github_snapshot.add_argument(
        "--repository",
        action="append",
        required=True,
        help="Repository to reconcile; may be repeated.",
    )
    usage_github_snapshot.set_defaults(func=cmd_usage_github_snapshot)
    usage_summarize = usage_sub.add_parser(
        "summarize",
        help="Summarize one Cursor Usage Events CSV",
    )
    usage_summarize.add_argument("--csv", required=True)
    usage_summarize.set_defaults(func=cmd_usage_summarize)
    usage_capacity = usage_sub.add_parser(
        "capacity-input",
        help="Normalize Cursor Usage Events CSV into provider-neutral capacity evidence",
    )
    usage_capacity.add_argument("--csv", required=True)
    usage_capacity.set_defaults(func=cmd_usage_capacity_input)
    usage_transition = usage_sub.add_parser(
        "provider-transition-plan",
        help="Plan one advisory provider failover transition without executing it",
    )
    usage_transition.add_argument("--candidates", required=True)
    usage_transition.add_argument("--required-capability", required=True)
    usage_transition.add_argument("--current-route", required=True)
    usage_transition.add_argument("--failure-reason", required=True)
    usage_transition.add_argument(
        "--prior-failed-route",
        action="append",
        default=[],
    )
    usage_transition.add_argument(
        "--strategy",
        choices=["CAPABILITY_FIRST", "STEWARDSHIP"],
        default="CAPABILITY_FIRST",
    )
    usage_transition.add_argument("--max-attempts", type=int, default=3)
    usage_transition.add_argument("--evaluated-at", required=True)
    usage_transition.add_argument(
        "--max-evidence-age-seconds",
        type=int,
        required=True,
    )
    usage_transition.set_defaults(func=cmd_usage_provider_transition_plan)
    usage_context_canary = usage_sub.add_parser(
        "context-canary-input",
        help="Normalize Engineering System context-canary evidence into observation-only governor input",
    )
    usage_context_canary.add_argument("--input", required=True)
    usage_context_canary.set_defaults(func=cmd_usage_context_canary_input)
    usage_context_shadow = usage_sub.add_parser(
        "context-shadow-bind",
        help="Bind Engineering System shadow equivalence to an observation-only context input",
    )
    usage_context_shadow.add_argument("--context-input", required=True)
    usage_context_shadow.add_argument("--shadow-report", required=True)
    usage_context_shadow.set_defaults(func=cmd_usage_context_shadow_bind)
    usage_context_learned = usage_sub.add_parser(
        "context-learned-bind",
        help="Bind Engineering System learned-canary admission to an observation-only context input",
    )
    usage_context_learned.add_argument("--context-input", required=True)
    usage_context_learned.add_argument("--admission-report", required=True)
    usage_context_learned.set_defaults(func=cmd_usage_context_learned_bind)
    usage_report = usage_sub.add_parser(
        "report",
        help="Inventory plus optional CSV summary and a warning advisor",
    )
    usage_report.add_argument("--csv", default=None)
    usage_report.add_argument("--packet-facts", default=None)
    usage_report.add_argument(
        "--github-reconcile",
        action="store_true",
        help="Read trusted Work Packet and PR facts from GitHub without mutation.",
    )
    usage_report.add_argument(
        "--repository",
        action="append",
        default=[],
        help="Repository to reconcile even when no local resident worker identifies it.",
    )
    usage_report.add_argument(
        "--context-facts",
        default=None,
        help="Bounded Engineering System context-epoch facts JSON.",
    )
    usage_report.add_argument(
        "--host-id",
        default=None,
        help="Bounded source host label for any local workers in this report.",
    )
    usage_report.add_argument(
        "--worker-snapshot",
        action="append",
        default=[],
        help="Content-free host inventory JSON to aggregate; may be repeated.",
    )
    usage_report.add_argument(
        "--github-snapshot",
        default=None,
        help="Bounded content-free GitHub reconciliation JSON from an authenticated host.",
    )
    usage_report.add_argument(
        "--no-local-workers",
        action="store_true",
        help="Central mode: do not require local agent/process discovery; use worker snapshots only.",
    )
    usage_report.set_defaults(func=cmd_usage_report)

    readiness = sub.add_parser(
        "readiness",
        help="Read-only dependency/readiness graph planning",
    )
    readiness_sub = readiness.add_subparsers(
        dest="readiness_command", required=True
    )
    readiness_plan = readiness_sub.add_parser(
        "plan",
        help="Evaluate one bounded readiness graph without dispatch",
    )
    readiness_plan.add_argument("--graph", required=True)
    readiness_plan.set_defaults(func=cmd_readiness_plan)
    readiness_github_plan = readiness_sub.add_parser(
        "github-plan",
        help="Reconcile graph nodes with trusted GitHub Work Packets, then plan",
    )
    readiness_github_plan.add_argument("--graph", required=True)
    readiness_github_plan.set_defaults(func=cmd_readiness_github_plan)
    readiness_github_packets = readiness_sub.add_parser(
        "github-packets",
        help="Plan an explicit bounded set of canonical GitHub AI Work Packets",
    )
    readiness_github_packets.add_argument(
        "--packet",
        action="append",
        required=True,
        help="Canonical owner/repo#issue selector; may be repeated.",
    )
    readiness_github_packets.add_argument("--max-wip", type=int, default=1)
    readiness_github_packets.set_defaults(func=cmd_readiness_github_packets)
    readiness_authorize = readiness_sub.add_parser(
        "github-authorize",
        help="Double-reconcile one graph and emit a read-only single-effect authorization",
    )
    readiness_authorize.add_argument("--graph", required=True)
    readiness_authorize.set_defaults(func=cmd_readiness_github_authorize)

    ops = sub.add_parser("ops", help="Service configuration and health")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)
    ops_readiness = ops_sub.add_parser(
        "readiness",
        help="Show read-only Phase 5 operations and release readiness",
    )
    ops_readiness.set_defaults(func=cmd_ops_readiness)
    ops_check = ops_sub.add_parser(
        "check",
        help="Fail closed unless the service environment is ready",
    )
    ops_check.add_argument(
        "--env-file",
        default=None,
        help="Service env file. When set, the process environment is ignored.",
    )
    ops_check.add_argument(
        "--prod",
        action="store_true",
        help="Require the prod-atlas public resource URL. Omit for generic checks.",
    )
    ops_check.set_defaults(func=cmd_ops_check)
    ops_stage = ops_sub.add_parser(
        "stage",
        help="Copy the systemd unit into a directory without installing it",
    )
    ops_stage.add_argument("--dest", required=True)
    ops_stage.set_defaults(func=cmd_ops_stage)
    ops_prod = ops_sub.add_parser(
        "prod-contract",
        help="Print the secret-free prod-atlas launch contract; do not contact a host",
    )
    ops_prod.set_defaults(func=cmd_ops_prod_contract)
    ops_backup = ops_sub.add_parser(
        "backup",
        help="Quiesce Atlas writers and snapshot registry plus projections",
    )
    ops_backup.add_argument("--data-root", required=True)
    ops_backup.add_argument("--dest", required=True)
    ops_backup.set_defaults(func=cmd_ops_backup)
    ops_restore = ops_sub.add_parser(
        "restore-test",
        help="Validate a backup and restore it into an empty directory",
    )
    ops_restore.add_argument("--backup", required=True)
    ops_restore.add_argument("--dest", required=True)
    ops_restore.set_defaults(func=cmd_ops_restore_test)
    ops_upgrade = ops_sub.add_parser(
        "upgrade",
        help="Prove this code can read the data root; do not rewrite it",
    )
    ops_upgrade.add_argument("--data-root", required=True)
    ops_upgrade.set_defaults(func=cmd_ops_upgrade)
    ops_rollback = ops_sub.add_parser(
        "rollback",
        help="Prove the staged target code can read the data root; do not rewrite it",
    )
    ops_rollback.add_argument("--data-root", required=True)
    ops_rollback.add_argument("--target-code", required=True)
    ops_rollback.set_defaults(func=cmd_ops_rollback)

    return parser


def _usage_inputs(args: argparse.Namespace, *, kind: str) -> dict:
    no_local_workers = bool(getattr(args, "no_local_workers", False))
    local_host_id = getattr(args, "host_id", None)
    worker_snapshot_paths = list(getattr(args, "worker_snapshot", []) or [])
    if no_local_workers:
        if local_host_id is not None:
            raise ValidationError("--host-id is invalid with --no-local-workers")
        if not worker_snapshot_paths:
            raise ValidationError(
                "--no-local-workers requires at least one --worker-snapshot"
            )
        sessions = []
        processes = []
        identities = {}
    else:
        sessions = live_sessions()
        processes = collect_process_facts(
            known_session_ids={session.session_id for session in sessions}
        )
        identities = identities_for_workspaces(
            workspace
            for workspace in (session.workspace for session in sessions)
            if workspace
        )

    imported_workers: list[dict] = []
    snapshot_summaries: list[dict] = []
    seen_snapshot_hosts: set[str] = set()
    for snapshot_path in worker_snapshot_paths:
        snapshot_workers, snapshot_summary = load_worker_snapshot(
            Path(snapshot_path)
        )
        snapshot_host_id = str(snapshot_summary["host_id"])
        if snapshot_host_id in seen_snapshot_hosts:
            raise ValidationError(
                f"duplicate worker snapshot host_id: {snapshot_host_id}"
            )
        seen_snapshot_hosts.add(snapshot_host_id)
        imported_workers.extend(snapshot_workers)
        snapshot_summaries.append(snapshot_summary)

    packet_path = getattr(args, "packet_facts", None)
    github_reconcile = bool(getattr(args, "github_reconcile", False))
    github_snapshot_path = getattr(args, "github_snapshot", None)
    requested_repositories = list(getattr(args, "repository", []) or [])
    authority_inputs = sum(
        bool(value)
        for value in (packet_path, github_reconcile, github_snapshot_path)
    )
    if authority_inputs > 1:
        raise ValidationError(
            "--packet-facts, --github-reconcile, and --github-snapshot "
            "are mutually exclusive"
        )
    if requested_repositories and not github_reconcile:
        raise ValidationError("--repository requires --github-reconcile")

    packet_observations = None
    github_snapshot_summary = None
    reconciled_repositories = None
    if github_reconcile:
        repositories = {
            identity.repository
            for identity in identities.values()
            if identity is not None
        }
        repositories.update(
            worker["repository"]
            for worker in imported_workers
            if isinstance(worker.get("repository"), str)
        )
        repositories.update(requested_repositories)
        reconciled_repositories = sorted(repositories)
        packets, packet_observations = collect_github_packet_observations(
            reconciled_repositories
        )
    elif github_snapshot_path:
        (
            packets,
            packet_observations,
            github_snapshot_summary,
        ) = load_github_reconciliation_snapshot(Path(github_snapshot_path))
        reconciled_repositories = list(github_snapshot_summary["repositories"])
    else:
        packets = load_packet_facts(Path(packet_path)) if packet_path else None

    summary = None
    if getattr(args, "csv", None):
        summary = summarize_usage(parse_usage_csv(Path(args.csv)))
    context_advice = None
    if getattr(args, "context_facts", None):
        context_advice = load_context_advice(Path(args.context_facts))
    report = build_report(
        sessions,
        processes,
        identities,
        packets,
        summary,
        packet_observations=packet_observations,
        context_advice=context_advice,
        local_host_id=local_host_id,
        imported_workers=imported_workers,
        snapshot_summaries=snapshot_summaries,
        github_snapshot_summary=github_snapshot_summary,
        reconciled_repositories=reconciled_repositories,
    )
    report["kind"] = kind
    report["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if local_host_id is not None:
        report["host_id"] = local_host_id
    if kind == "cursor_worker_inventory":
        report.pop("usage", None)
        if report.get("context_epoch") is None:
            report.pop("context_epoch", None)
    if report.get("reconciliation") is None:
        report.pop("reconciliation", None)
    if report.get("reconciliation_summary") is None:
        report.pop("reconciliation_summary", None)
    if not report.get("snapshots"):
        report.pop("snapshots", None)
    if report.get("github_snapshot") is None:
        report.pop("github_snapshot", None)
    assert_content_free(report)
    return report


def cmd_usage_inventory(args: argparse.Namespace) -> int:
    _print_json(_usage_inputs(args, kind="cursor_worker_inventory"))
    return 0


def cmd_usage_github_snapshot(args: argparse.Namespace) -> int:
    _print_json(build_github_reconciliation_snapshot(args.repository))
    return 0


def cmd_usage_summarize(args: argparse.Namespace) -> int:
    _print_json(summary_report(parse_usage_csv(Path(args.csv))))
    return 0


def cmd_usage_capacity_input(args: argparse.Namespace) -> int:
    _print_json(cursor_capacity_input(parse_usage_csv(Path(args.csv))))
    return 0


def cmd_usage_provider_transition_plan(args: argparse.Namespace) -> int:
    _print_json(
        plan_provider_transition(
            load_provider_transition_candidates(Path(args.candidates)),
            required_capability=args.required_capability,
            current_route_id=args.current_route,
            failure_reason=args.failure_reason,
            prior_failed_route_ids=args.prior_failed_route,
            strategy=args.strategy,
            max_attempts=args.max_attempts,
            evaluated_at=args.evaluated_at,
            max_evidence_age_seconds=args.max_evidence_age_seconds,
        )
    )
    return 0


def cmd_usage_context_canary_input(args: argparse.Namespace) -> int:
    _print_json(load_context_canary_report(Path(args.input)))
    return 0


def cmd_usage_context_shadow_bind(args: argparse.Namespace) -> int:
    _print_json(
        load_shadow_quality_binding(
            Path(args.context_input),
            Path(args.shadow_report),
        )
    )
    return 0


def cmd_usage_context_learned_bind(args: argparse.Namespace) -> int:
    _print_json(
        load_learned_canary_binding(
            Path(args.context_input),
            Path(args.admission_report),
        )
    )
    return 0


def cmd_usage_report(args: argparse.Namespace) -> int:
    _print_json(_usage_inputs(args, kind="cursor_usage_report"))
    return 0


def cmd_readiness_plan(args: argparse.Namespace) -> int:
    _print_json(plan_readiness_file(Path(args.graph)))
    return 0


def cmd_readiness_github_plan(args: argparse.Namespace) -> int:
    adapter = GitHubWorkPacketAdapter()
    _print_json(
        plan_github_reconciled_readiness_file(
            Path(args.graph), adapter.read_readiness_packet_fact
        )
    )
    return 0


def cmd_readiness_github_authorize(args: argparse.Namespace) -> int:
    adapter = GitHubWorkPacketAdapter()
    _print_json(
        authorize_github_single_effect_file(
            Path(args.graph), adapter.read_readiness_packet_fact
        )
    )
    return 0


def cmd_readiness_github_packets(args: argparse.Namespace) -> int:
    if len(args.packet) > MAX_NODES:
        raise ValidationError("selected packet list exceeds bounded node count")
    selectors = [parse_packet_selector(value) for value in args.packet]
    identities = [(item.repository, item.issue_number) for item in selectors]
    if len(identities) != len(set(identities)):
        raise ValidationError("duplicate --packet selector")
    grouped: dict[str, list[int]] = {}
    for selector in selectors:
        grouped.setdefault(selector.repository, []).append(selector.issue_number)
    reader = GitHubWorkPacketAdapter()
    projections: list[dict] = []
    for repository in sorted(grouped):
        projections.extend(
            reader.read_selected_packet_projections(
                repository, sorted(grouped[repository])
            )
        )
    _print_json(
        plan_selected_packet_projections(projections, max_wip=args.max_wip)
    )
    return 0


def cmd_host_worker_run_once(args: argparse.Namespace) -> int:
    outcome = run_once(
        config=load_host_worker_config(Path(args.descriptors)),
        resume_requested=False,
    )
    _print_json(outcome)
    return 0


def _subprocess_runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def cmd_host_worker_dispose_once(args: argparse.Namespace) -> int:
    """Completed claim -> canonical GitHub packet -> locked host resume."""
    config = load_host_worker_config(Path(args.descriptors))
    if len(config.projects) != 1:
        raise ValidationError("dispose-once requires exactly one project descriptor")
    repository = config.projects[0].repository
    from atlas.audit_claim import WorkPacketSnapshot

    outcome = run_completed_audit_disposition(
        host_config=config,
        claim_store=GitHubContentsClaimStore(
            repository,
            command_runner=_subprocess_runner,
        ),
        packet_adapter=GitHubWorkPacketAdapter(command_runner=_subprocess_runner),
        issue_number=int(args.issue),
        branch=str(args.branch),
        workstream=str(args.workstream),
        head=str(args.head),
        packets=[
            WorkPacketSnapshot(
                repository=repository,
                issue_number=int(args.issue),
                branch=str(args.branch),
                head=str(args.head),
                status="ACTIVE",
            )
        ],
        git_runner=default_git_runner,
        spawn=lambda argv, cwd: _subprocess_runner(argv, cwd).returncode,
        host_probe=None,
    )
    _print_json(outcome)
    return 0


def cmd_host_worker_supervise_once(args: argparse.Namespace) -> int:
    """Discover each descriptor repository's unique ACTIVE packet and act once."""
    config = load_host_worker_config(Path(args.descriptors))
    adapter = GitHubWorkPacketAdapter(command_runner=_subprocess_runner)
    budget = AuditBudget(per_run_hard_usd=1.0, monthly_hard_usd=25.0)

    def claim_store_for(repository: str) -> GitHubContentsClaimStore:
        return GitHubContentsClaimStore(
            repository,
            command_runner=_subprocess_runner,
        )

    outcome = supervise_once(
        config=config,
        packet_adapter=adapter,
        claim_store_for=claim_store_for,
        auditor=BoundedResponsesAuditProvider(
            budget=budget,
            command_runner=_subprocess_runner,
            git_runner=default_git_runner,
        ),
        budget=budget,
        git_runner=default_git_runner,
        spawn=lambda argv, cwd: _subprocess_runner(argv, cwd).returncode,
        host_probe=None,
    )
    _print_json(outcome)
    return 0


def cmd_ops_readiness(args: argparse.Namespace) -> int:
    _print_json(_service(args).operations_readiness())
    return 0


def cmd_ops_check(args: argparse.Namespace) -> int:
    if args.prod:
        if not args.env_file:
            raise ValidationError("prod check requires --env-file")
        try:
            prod_env = Path(args.env_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError("prod check requires a readable env file") from exc
        validate_prod_deployment_env(prod_env)
    if args.env_file:
        report = assess_service_environment({}, env_file=Path(args.env_file))
    else:
        report = assess_service_environment(os.environ)
    _print_json(report)
    return 0 if report["status"] == "ready" else 1


def cmd_ops_stage(args: argparse.Namespace) -> int:
    target = stage_unit(Path(args.dest))
    _print_json({"unit": str(target)})
    return 0


def cmd_ops_prod_contract(_args: argparse.Namespace) -> int:
    _print_json(prod_launch_contract())
    return 0


def cmd_ops_backup(args: argparse.Namespace) -> int:
    _print_json(backup_data_root(Path(args.data_root), Path(args.dest)))
    return 0


def cmd_ops_restore_test(args: argparse.Namespace) -> int:
    _print_json(restore_test(Path(args.backup), Path(args.dest)))
    return 0


def cmd_ops_upgrade(args: argparse.Namespace) -> int:
    _print_json(upgrade_data_root(Path(args.data_root)))
    return 0


def cmd_ops_rollback(args: argparse.Namespace) -> int:
    _print_json(rollback_data_root(Path(args.data_root), Path(args.target_code)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
