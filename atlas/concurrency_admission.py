"""Provider-neutral measured concurrency admission.

This module reuses dependency/readiness planning and adds an advisory execution
slot admission layer plus run/join measurement. It has no dispatch authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError
from atlas.readiness_graph import MAX_WIP, plan_readiness, validate_readiness_graph

SCHEMA_VERSION = 1
SNAPSHOT_KIND = "concurrency_admission_snapshot"
PLAN_KIND = "concurrency_admission_plan"
RUN_KIND = "concurrency_run_observation"
SNAPSHOT_FILENAME = "concurrency-admission.json"
RUNS_FILENAME = "concurrency-runs.json"
AUTHORITY = "ADVISORY_ONLY"
DISPATCH_AUTHORITY = "NO_DISPATCH_AUTHORITY"
SLOT_STATES = frozenset({"AVAILABLE", "BUSY", "UNAVAILABLE", "UNKNOWN"})
GATE_STATES = frozenset({"ALLOW", "DENY", "UNKNOWN"})
SLOT_GATES = ("trust", "budget", "rate", "quota", "blast_radius")
RUN_OUTCOMES = frozenset({"COMPLETE", "FAILED", "HUMAN_REQUIRED"})
_MAX_BYTES = 1024 * 1024
_MAX_SLOTS = 64
_MAX_RUNS = 500
_MAX_PROJECTS = 64
_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000
_MAX_EVIDENCE_AGE_SECONDS = 366 * 24 * 60 * 60
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#\-]{0,255}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")

def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("concurrency payload is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _identity(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER if provider else _ID
    if not isinstance(value, str) or pattern.fullmatch(value) is None or _SECRET.search(value):
        _reject(f"concurrency {label} is invalid")
    return value


def _utc(value: object, *, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject(f"concurrency {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"concurrency {label} must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject(f"concurrency {label} must be UTC")
    return value, parsed


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _reject(f"concurrency {label} is invalid")
    return value


def _load_json(path: Path, *, label: str, max_bytes: int = _MAX_BYTES) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"concurrency {label} path is unsafe")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"concurrency {label} is unreadable") from exc
    if len(raw) > max_bytes:
        raise ValidationError(f"concurrency {label} exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"concurrency {label} is invalid JSON") from exc
    assert_content_free(payload)
    return payload

def _validate_policy(value: object, repositories: set[str]) -> dict[str, object]:
    keys = {
        "authority",
        "max_parallel_admission",
        "max_slot_evidence_age_seconds",
        "project_limits",
    }
    if not isinstance(value, dict) or set(value) != keys:
        _reject("concurrency admission policy schema is invalid")
    if value.get("authority") != "ADMISSION_POLICY_ONLY":
        _reject("concurrency admission policy authority is invalid")
    max_parallel = _bounded_int(
        value.get("max_parallel_admission"),
        label="max_parallel_admission",
        minimum=1,
        maximum=MAX_WIP,
    )
    max_age = _bounded_int(
        value.get("max_slot_evidence_age_seconds"),
        label="max_slot_evidence_age_seconds",
        minimum=0,
        maximum=_MAX_EVIDENCE_AGE_SECONDS,
    )
    raw_limits = value.get("project_limits")
    if not isinstance(raw_limits, list) or not raw_limits or len(raw_limits) > _MAX_PROJECTS:
        _reject("concurrency project_limits must be a bounded non-empty list")
    limits: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in raw_limits:
        if not isinstance(item, dict) or set(item) != {"repository", "max_wip"}:
            _reject("concurrency project limit schema is invalid")
        repository = item.get("repository")
        if not isinstance(repository, str) or _REPO.fullmatch(repository) is None or repository in seen:
            _reject("concurrency project limit repository is invalid")
        seen.add(repository)
        limits.append(
            {
                "repository": repository,
                "max_wip": _bounded_int(
                    item.get("max_wip"),
                    label="project max_wip",
                    minimum=1,
                    maximum=MAX_WIP,
                ),
            }
        )
    if seen != repositories:
        _reject("concurrency project_limits must cover every graph repository exactly")
    return {
        "authority": "ADMISSION_POLICY_ONLY",
        "max_parallel_admission": max_parallel,
        "max_slot_evidence_age_seconds": max_age,
        "project_limits": sorted(limits, key=lambda item: str(item["repository"])),
    }


def _validate_slot(value: object) -> dict[str, object]:
    keys = {
        "slot_id", "worker_id", "provider", "runtime", "route_id",
        "state", "gates", "evidence_ref", "observed_at"
    }
    if not isinstance(value, dict) or set(value) != keys:
        _reject("concurrency execution slot schema is invalid")
    slot_id = _identity(value.get("slot_id"), label="slot_id")
    worker_id = _identity(value.get("worker_id"), label="worker_id")
    provider = _identity(value.get("provider"), label="provider", provider=True)
    runtime = _identity(value.get("runtime"), label="runtime")
    route_raw = value.get("route_id")
    route_id = None if route_raw is None else _identity(route_raw, label="route_id")
    state = value.get("state")
    if state not in SLOT_STATES:
        _reject("concurrency slot state is invalid")
    gates = value.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(SLOT_GATES):
        _reject("concurrency slot gates schema is invalid")
    normalized_gates = {}
    for name in SLOT_GATES:
        gate = gates.get(name)
        if gate not in GATE_STATES:
            _reject(f"concurrency slot {name} gate is invalid")
        normalized_gates[name] = gate
    observed_at, _ = _utc(value.get("observed_at"), label="slot observed_at")
    return {
        "slot_id": slot_id,
        "worker_id": worker_id,
        "provider": provider,
        "runtime": runtime,
        "route_id": route_id,
        "state": state,
        "gates": normalized_gates,
        "evidence_ref": _identity(value.get("evidence_ref"), label="evidence_ref"),
        "observed_at": observed_at,
    }

def validate_concurrency_snapshot(payload: object) -> dict[str, object]:
    keys = {
        "schema_version", "kind", "observed_at", "graph",
        "policy", "execution_slots"
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        _reject("concurrency snapshot schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        _reject("concurrency snapshot schema_version is unsupported")
    if payload.get("kind") != SNAPSHOT_KIND:
        _reject("concurrency snapshot kind is invalid")
    observed_at, _ = _utc(payload.get("observed_at"), label="snapshot observed_at")

    max_wip, nodes = validate_readiness_graph(payload.get("graph"))
    repositories = {node.repository for node in nodes}
    policy = _validate_policy(payload.get("policy"), repositories)

    raw_slots = payload.get("execution_slots")
    if not isinstance(raw_slots, list) or len(raw_slots) > _MAX_SLOTS:
        _reject("concurrency execution_slots must be a bounded list")
    slots = [_validate_slot(item) for item in raw_slots]
    slot_ids = [str(item["slot_id"]) for item in slots]
    worker_ids = [str(item["worker_id"]) for item in slots]
    if len(slot_ids) != len(set(slot_ids)):
        _reject("concurrency slot_id values must be unique")
    if len(worker_ids) != len(set(worker_ids)):
        _reject("concurrency worker_id values must be unique in first slice")

    normalized_graph = {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": [
            {
                "node_id": node.node_id,
                "issue_number": node.issue_number,
                "repository": node.repository,
                "branch": node.branch,
                "head": node.head,
                "packet_status": node.packet_status,
                "queue_state": node.queue_state,
                "dependencies": [
                    {"node_id": dependency.node_id, "relation": dependency.relation}
                    for dependency in node.dependencies
                ],
                "resources": list(node.resources),
                "authority_state": node.authority_state,
                "owner_gate": node.owner_gate,
                "human_required": node.human_required,
                "priority": node.priority,
            }
            for node in nodes
        ],
    }
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": SNAPSHOT_KIND,
        "observed_at": observed_at,
        "graph": normalized_graph,
        "policy": policy,
        "execution_slots": sorted(slots, key=lambda item: str(item["slot_id"])),
    }
    assert_content_free(normalized)
    return normalized

def _slot_reasons(
    slot: dict[str, object],
    *,
    snapshot_time: datetime,
    max_age: timedelta,
) -> list[str]:
    reasons: list[str] = []
    if slot["state"] != "AVAILABLE":
        reasons.append(f'SLOT_{slot["state"]}')
    for name in SLOT_GATES:
        state = slot["gates"][name]
        if state != "ALLOW":
            reasons.append(f'{name.upper()}_{state}')
    _, observed = _utc(slot["observed_at"], label="slot observed_at")
    if observed > snapshot_time:
        reasons.append("SLOT_EVIDENCE_FUTURE")
    elif snapshot_time - observed > max_age:
        reasons.append("SLOT_EVIDENCE_STALE")
    return reasons


def plan_concurrency_admission(payload: object) -> dict[str, object]:
    snapshot = validate_concurrency_snapshot(payload)
    graph = snapshot["graph"]
    graph_plan = plan_readiness(graph)
    _, nodes = validate_readiness_graph(graph)
    node_by_id = {node.node_id: node for node in nodes}
    _, snapshot_time = _utc(snapshot["observed_at"], label="snapshot observed_at")
    max_age = timedelta(seconds=int(snapshot["policy"]["max_slot_evidence_age_seconds"]))

    eligible_slots: list[dict[str, object]] = []
    ineligible_slots: list[dict[str, object]] = []
    for slot in snapshot["execution_slots"]:
        reasons = _slot_reasons(slot, snapshot_time=snapshot_time, max_age=max_age)
        summary = {
            "slot_id": slot["slot_id"],
            "worker_id": slot["worker_id"],
            "provider": slot["provider"],
            "runtime": slot["runtime"],
            "route_id": slot["route_id"],
            "evidence_ref": slot["evidence_ref"],
            "observed_at": slot["observed_at"],
        }
        if reasons:
            ineligible_slots.append({**summary, "reasons": reasons})
        else:
            eligible_slots.append({**summary, "reasons": ["ELIGIBLE"]})

    limits = {
        str(item["repository"]): int(item["max_wip"])
        for item in snapshot["policy"]["project_limits"]
    }
    project_active = {repository: 0 for repository in limits}
    for node in nodes:
        if node.packet_status == "ACTIVE":
            project_active[node.repository] += 1

    assignments: list[dict[str, object]] = []
    blocked: list[dict[str, object]] = []
    available_slots = list(eligible_slots)
    admitted_by_project = {repository: 0 for repository in limits}
    max_parallel = int(snapshot["policy"]["max_parallel_admission"])

    for node_id in graph_plan["selected_node_ids"]:
        node = node_by_id[node_id]
        reasons: list[str] = []
        if len(assignments) >= max_parallel:
            reasons.append("MAX_PARALLEL_ADMISSION")
        if project_active[node.repository] + admitted_by_project[node.repository] >= limits[node.repository]:
            reasons.append("PROJECT_WIP_LIMIT")
        if not available_slots:
            reasons.append("NO_EXECUTION_SLOT")
        if reasons:
            blocked.append(
                {
                    "node_id": node.node_id,
                    "repository": node.repository,
                    "issue_number": node.issue_number,
                    "reasons": reasons,
                }
            )
            continue
        slot = available_slots.pop(0)
        assignments.append(
            {
                "node_id": node.node_id,
                "repository": node.repository,
                "issue_number": node.issue_number,
                "branch": node.branch,
                "head": node.head,
                "slot_id": slot["slot_id"],
                "worker_id": slot["worker_id"],
                "provider": slot["provider"],
                "runtime": slot["runtime"],
                "route_id": slot["route_id"],
                "slot_evidence_ref": slot["evidence_ref"],
            }
        )
        admitted_by_project[node.repository] += 1

    plan_without_digest = {
        "schema_version": SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "authority": AUTHORITY,
        "dispatch_authority": DISPATCH_AUTHORITY,
        "observed_at": snapshot["observed_at"],
        "graph_state": graph_plan["graph_state"],
        "graph_reasons": list(graph_plan["graph_reasons"]),
        "global_max_wip": graph_plan["max_wip"],
        "active_count": graph_plan["active_count"],
        "max_parallel_admission": max_parallel,
        "project_limits": list(snapshot["policy"]["project_limits"]),
        "graph_selected_node_ids": list(graph_plan["selected_node_ids"]),
        "assignments": assignments,
        "blocked_selected_nodes": blocked,
        "eligible_slots": eligible_slots,
        "ineligible_slots": ineligible_slots,
    }
    digest = _canonical_digest(plan_without_digest)
    return {**plan_without_digest, "plan_digest": digest}

def publish_concurrency_snapshot(data_root: Path, snapshot: object) -> dict[str, object]:
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("concurrency data root is not a directory")
    normalized = validate_concurrency_snapshot(snapshot)
    # Compute admission before publication so a snapshot cannot persist if its
    # derived planning semantics fail.
    plan_concurrency_admission(normalized)
    with data_root_write_lock(root):
        atomic_write_text(
            root / SNAPSHOT_FILENAME,
            json.dumps(normalized, indent=2, sort_keys=True) + "\n",
        )
    return concurrency_dashboard(root)


def _load_snapshot(data_root: Path) -> dict[str, object] | None:
    path = Path(data_root) / SNAPSHOT_FILENAME
    if not path.exists():
        return None
    return validate_concurrency_snapshot(_load_json(path, label="snapshot"))


def _empty_runs() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "concurrency_run_ledger",
        "authority": "MEASUREMENT_ONLY",
        "runs": [],
    }


def _load_runs(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / RUNS_FILENAME
    if not path.exists():
        return _empty_runs()
    payload = _load_json(path, label="run ledger")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "authority", "runs"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != "concurrency_run_ledger"
        or payload.get("authority") != "MEASUREMENT_ONLY"
        or not isinstance(payload.get("runs"), list)
        or len(payload["runs"]) > _MAX_RUNS
    ):
        raise ValidationError("concurrency run ledger schema is invalid")
    seen: set[str] = set()
    runs = []
    for item in payload["runs"]:
        if not isinstance(item, dict):
            raise ValidationError("concurrency run ledger entry is invalid")
        run_id = item.get("run_id")
        if not isinstance(run_id, str) or _ID.fullmatch(run_id) is None or run_id in seen:
            raise ValidationError("concurrency run ledger run_id is invalid")
        seen.add(run_id)
        runs.append(dict(item))
    return {**_empty_runs(), "runs": runs}

def _validate_run_observation(
    observation: object,
    *,
    plan: dict[str, object],
) -> dict[str, object]:
    keys = {
        "schema_version", "kind", "run_id", "plan_digest",
        "started_at", "completed_at", "outcomes"
    }
    if not isinstance(observation, dict) or set(observation) != keys:
        raise ValidationError("concurrency run observation schema is invalid")
    if observation.get("schema_version") != SCHEMA_VERSION or isinstance(observation.get("schema_version"), bool):
        raise ValidationError("concurrency run observation schema_version is unsupported")
    if observation.get("kind") != RUN_KIND:
        raise ValidationError("concurrency run observation kind is invalid")
    run_id = _identity(observation.get("run_id"), label="run_id")
    plan_digest = observation.get("plan_digest")
    if not isinstance(plan_digest, str) or _SHA256.fullmatch(plan_digest) is None:
        raise ValidationError("concurrency run plan_digest is invalid")
    if plan_digest != plan["plan_digest"]:
        raise ValidationError("concurrency run is bound to a different admission plan")
    started_at, started = _utc(observation.get("started_at"), label="started_at")
    completed_at, completed = _utc(observation.get("completed_at"), label="completed_at")
    if completed < started:
        raise ValidationError("concurrency run completion precedes start")
    wall_ms = int((completed - started).total_seconds() * 1000)

    assignments = {
        str(item["node_id"]): item for item in plan["assignments"]
    }
    raw_outcomes = observation.get("outcomes")
    if not isinstance(raw_outcomes, list) or len(raw_outcomes) != len(assignments):
        raise ValidationError("concurrency run must contain one outcome per admitted assignment")
    normalized = []
    seen: set[str] = set()
    for item in raw_outcomes:
        keys = {
            "node_id", "slot_id", "worker_id", "provider",
            "outcome", "duration_ms", "evidence_ref"
        }
        if not isinstance(item, dict) or set(item) != keys:
            raise ValidationError("concurrency run outcome schema is invalid")
        node_id = _identity(item.get("node_id"), label="run node_id")
        assignment = assignments.get(node_id)
        if assignment is None or node_id in seen:
            raise ValidationError("concurrency run outcome node is not an admitted assignment")
        seen.add(node_id)
        slot_id = _identity(item.get("slot_id"), label="run slot_id")
        worker_id = _identity(item.get("worker_id"), label="run worker_id")
        provider = _identity(item.get("provider"), label="run provider", provider=True)
        if (
            slot_id != assignment["slot_id"]
            or worker_id != assignment["worker_id"]
            or provider != assignment["provider"]
        ):
            raise ValidationError("concurrency run outcome attribution does not match admission")
        outcome = item.get("outcome")
        if outcome not in RUN_OUTCOMES:
            raise ValidationError("concurrency run outcome is invalid")
        duration_ms = _bounded_int(
            item.get("duration_ms"),
            label="duration_ms",
            minimum=0,
            maximum=_MAX_DURATION_MS,
        )
        normalized.append(
            {
                "node_id": node_id,
                "slot_id": slot_id,
                "worker_id": worker_id,
                "provider": provider,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "evidence_ref": _identity(item.get("evidence_ref"), label="run evidence_ref"),
            }
        )
    normalized.sort(key=lambda item: str(item["node_id"]))
    outcomes = [item["outcome"] for item in normalized]
    complete_count = outcomes.count("COMPLETE")
    failed_count = outcomes.count("FAILED")
    human_count = outcomes.count("HUMAN_REQUIRED")
    if outcomes and complete_count == len(outcomes):
        result = "PASS"
    elif human_count:
        result = "HUMAN_REQUIRED"
    elif complete_count:
        result = "PARTIAL"
    else:
        result = "FAILED"
    sum_duration = sum(int(item["duration_ms"]) for item in normalized)
    parallelism_basis_points = (
        (sum_duration * 10000) // wall_ms
        if wall_ms > 0
        else (10000 if sum_duration == 0 else 0)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RUN_KIND,
        "run_id": run_id,
        "plan_digest": plan_digest,
        "started_at": started_at,
        "completed_at": completed_at,
        "result": result,
        "wall_time_ms": wall_ms,
        "sum_node_duration_ms": sum_duration,
        "parallelism_basis_points": parallelism_basis_points,
        "complete_count": complete_count,
        "failed_count": failed_count,
        "human_required_count": human_count,
        "outcomes": normalized,
    }

def record_concurrency_run(data_root: Path, observation: object) -> dict[str, object]:
    root = Path(data_root)
    snapshot = _load_snapshot(root)
    if snapshot is None:
        raise ValidationError("concurrency snapshot is not loaded")
    plan = plan_concurrency_admission(snapshot)
    run = _validate_run_observation(observation, plan=plan)
    assert_content_free(run)
    with data_root_write_lock(root):
        ledger = _load_runs(root)
        if len(ledger["runs"]) >= _MAX_RUNS:
            raise ValidationError("concurrency run ledger limit reached")
        if any(item.get("run_id") == run["run_id"] for item in ledger["runs"]):
            raise ValidationError("concurrency run_id already exists")
        ledger["runs"].append(run)
        ledger["runs"].sort(key=lambda item: (str(item.get("started_at", "")), str(item["run_id"])))
        atomic_write_text(
            root / RUNS_FILENAME,
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        )
    return concurrency_dashboard(root)


def concurrency_dashboard(data_root: Path) -> dict[str, object]:
    root = Path(data_root)
    snapshot = _load_snapshot(root)
    ledger = _load_runs(root)
    runs = list(ledger["runs"])
    plan = plan_concurrency_admission(snapshot) if snapshot is not None else None
    counts = {"PASS": 0, "PARTIAL": 0, "FAILED": 0, "HUMAN_REQUIRED": 0}
    for run in runs:
        result = run.get("result")
        if result in counts:
            counts[result] += 1
    total_nodes = sum(len(run.get("outcomes", [])) for run in runs)
    completed_nodes = sum(int(run.get("complete_count", 0)) for run in runs)
    partial_failure_runs = sum(run.get("result") in {"PARTIAL", "HUMAN_REQUIRED"} for run in runs)
    return {
        "state": "OBSERVED" if snapshot is not None else "UNKNOWN",
        "authority": AUTHORITY,
        "dispatch_authority": DISPATCH_AUTHORITY,
        "plan": plan,
        "run_count": len(runs),
        "result_counts": counts,
        "total_measured_nodes": total_nodes,
        "completed_nodes": completed_nodes,
        "partial_failure_runs": partial_failure_runs,
        "max_parallelism_basis_points": max(
            (int(run.get("parallelism_basis_points", 0)) for run in runs),
            default=0,
        ),
        "latest_run": runs[-1] if runs else None,
        "runs": runs[-50:],
    }
