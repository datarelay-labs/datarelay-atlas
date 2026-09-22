"""Minimal Atlas Phase 1 operator CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from atlas.codex_audit import CodexAuditProvider
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.work_controller import (
    AuditResult,
    FixedAuditAdapter,
    GitHubWorkPacketAdapter,
    OpenAIResponsesAuditAdapter,
    PtyPersistCursorDispatcher,
    RecordingCursorDispatcher,
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


def _controller_from_args(args: argparse.Namespace) -> WorkController:
    """Build a controller with operator-selected adapters.

    Production default is Codex (ChatGPT-plan, no API key). Use
    `--audit-adapter fixed` explicitly for offline/deterministic verdicts.
    `--audit-adapter openai` remains optional API fallback only.

    Production Work Packet adapter mutates the same GitHub `[AI Work]` Issue
    before REWORK dispatch. `RecordingWorkPacketAdapter` is offline/test-only.
    """
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
    if packet_choice is None:
        # Offline/fixed defaults to recording so dogfood does not mutate GitHub.
        packet_choice = "recording" if adapter == "fixed" else "github"
    if packet_choice == "github":
        work_packet = GitHubWorkPacketAdapter()
    elif packet_choice == "recording":
        work_packet = RecordingWorkPacketAdapter()
    else:
        raise ValidationError(f"unsupported work packet adapter: {packet_choice}")

    if getattr(args, "spawn_dispatch", False):
        dispatcher = PtyPersistCursorDispatcher()
    else:
        dispatcher = RecordingCursorDispatcher()
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
            "(production default for codex/openai); recording=offline/test only "
            "(default when --audit-adapter fixed)"
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
        description="DataRelay Atlas Phase 1 operator surface",
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

    return parser


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
