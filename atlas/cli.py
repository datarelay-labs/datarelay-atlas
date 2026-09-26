"""Minimal Atlas Phase 1 operator CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
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
from atlas.host_worker import load_host_worker_config, run_once
from atlas.supervisor import supervise_once
from atlas.data_protection import backup_data_root, restore_test
from atlas.schema_compat import rollback_data_root, upgrade_data_root
from atlas.ops import assess_service_environment, stage_unit
from atlas.provenance import ValidationError
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


def cmd_source_list(args: argparse.Namespace) -> int:
    svc = _service(args)
    _print_json([asdict(s) for s in svc.list_sources(args.project_id)])
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
        # Offline/fixed and metadata-only OpenAI default to recording so they
        # do not mutate GitHub. Codex remains the production GitHub path.
        packet_choice = "recording" if adapter in {"fixed", "openai"} else "github"
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

    slist = source_sub.add_parser("list", help="List sources for a project")
    slist.add_argument("project_id")
    slist.set_defaults(func=cmd_source_list)

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

    ops = sub.add_parser("ops", help="Service configuration and health")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)
    ops_check = ops_sub.add_parser(
        "check",
        help="Fail closed unless the service environment is ready",
    )
    ops_check.add_argument(
        "--env-file",
        default=None,
        help="Service env file. When set, the process environment is ignored.",
    )
    ops_check.set_defaults(func=cmd_ops_check)
    ops_stage = ops_sub.add_parser(
        "stage",
        help="Copy the systemd unit into a directory without installing it",
    )
    ops_stage.add_argument("--dest", required=True)
    ops_stage.set_defaults(func=cmd_ops_stage)
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


def cmd_ops_check(args: argparse.Namespace) -> int:
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
