"""Deterministic read-only dependency/readiness planning.

The planner consumes bounded normalized Work Packet facts. It never reads issue
bodies, mutates GitHub, or dispatches workers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
MAX_GRAPH_BYTES = 1024 * 1024
MAX_NODES = 256
MAX_DEPENDENCIES = 64
MAX_RESOURCES = 32
MAX_WIP = 32
MAX_PRIORITY = 1_000_000
_PACKET_STATUSES = frozenset({"ACTIVE", "PAUSED", "BLOCKED", "COMPLETE"})
_QUEUE_STATES = frozenset({"NONE", "QUEUED"})
_AUTHORITY_STATES = frozenset({"TRUSTED", "STALE", "AMBIGUOUS", "UNTRUSTED"})
_DEPENDENCY_RELATIONS = frozenset({"REQUIRES_COMPLETE"})
_READINESS_STATES = frozenset(
    {"ACTIVE", "COMPLETE", "READY", "BLOCKED", "HUMAN_REQUIRED"}
)
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#-]{0,127}$")
_TOP_KEYS = frozenset({"schema_version", "kind", "max_wip", "nodes"})
_DEPENDENCY_KEYS = frozenset({"node_id", "relation"})
_NODE_KEYS = frozenset(
    {
        "node_id",
        "issue_number",
        "repository",
        "branch",
        "head",
        "packet_status",
        "queue_state",
        "dependencies",
        "resources",
        "authority_state",
        "owner_gate",
        "human_required",
        "priority",
    }
)


@dataclass(frozen=True)
class DependencyRef:
    node_id: str
    relation: str


@dataclass(frozen=True)
class ReadinessNode:
    node_id: str
    issue_number: int
    repository: str
    branch: str
    head: str
    packet_status: str
    queue_state: str
    dependencies: tuple[DependencyRef, ...]
    resources: tuple[str, ...]
    authority_state: str
    owner_gate: bool
    human_required: bool
    priority: int

    @property
    def effective_resources(self) -> tuple[str, ...]:
        implicit = f"repo_branch:{self.repository}@{self.branch}"
        return tuple(sorted(set(self.resources + (implicit,))))


def _reject(message: str) -> None:
    raise ValidationError(message)


def _bounded_label(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _reject(f"{label} is invalid")
    return value


def _bounded_branch(value: object) -> str:
    if not isinstance(value, str) or not _BRANCH_RE.fullmatch(value):
        _reject("branch is invalid")
    if (
        ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        _reject("branch is invalid")
    return value


def _positive_int(value: object, *, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _reject(f"{label} must be a positive integer")
    if maximum is not None and value > maximum:
        _reject(f"{label} exceeds the supported maximum")
    return value


def _nonnegative_int(value: object, *, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _reject(f"{label} must be a non-negative integer")
    if maximum is not None and value > maximum:
        _reject(f"{label} exceeds the supported maximum")
    return value


def _bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        _reject(f"{label} must be boolean")
    return value


def _enum_string(value: object, *, label: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _reject(f"{label} is invalid")
    return value


def _bounded_string_list(
    value: object,
    *,
    label: str,
    maximum: int,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        _reject(f"{label} must be a bounded list")
    result: list[str] = []
    for item in value:
        result.append(_bounded_label(item, label=label, pattern=pattern))
    if len(result) != len(set(result)):
        _reject(f"{label} contains duplicates")
    return tuple(result)


def _parse_dependencies(value: object) -> tuple[DependencyRef, ...]:
    if not isinstance(value, list) or len(value) > MAX_DEPENDENCIES:
        _reject("dependencies must be a bounded list")
    result: list[DependencyRef] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != _DEPENDENCY_KEYS:
            _reject("dependency schema is invalid")
        node_id = _bounded_label(
            raw["node_id"], label="dependency node_id", pattern=_NODE_ID_RE
        )
        relation = _enum_string(
            raw["relation"],
            label="dependency relation",
            allowed=_DEPENDENCY_RELATIONS,
        )
        if node_id in seen:
            _reject("dependencies contains duplicate node_id")
        seen.add(node_id)
        result.append(DependencyRef(node_id=node_id, relation=relation))
    return tuple(result)


def _parse_node(raw: object) -> ReadinessNode:
    if not isinstance(raw, dict) or set(raw) != _NODE_KEYS:
        _reject("readiness node schema is invalid")
    node_id = _bounded_label(raw["node_id"], label="node_id", pattern=_NODE_ID_RE)
    issue_number = _positive_int(raw["issue_number"], label="issue_number")
    repository = _bounded_label(
        raw["repository"], label="repository", pattern=_REPOSITORY_RE
    )
    branch = _bounded_branch(raw["branch"])
    head = _bounded_label(raw["head"], label="head", pattern=_HEAD_RE)
    packet_status = _enum_string(
        raw["packet_status"], label="packet_status", allowed=_PACKET_STATUSES
    )
    queue_state = _enum_string(
        raw["queue_state"], label="queue_state", allowed=_QUEUE_STATES
    )
    dependencies = _parse_dependencies(raw["dependencies"])
    resources = _bounded_string_list(
        raw["resources"],
        label="resources",
        maximum=MAX_RESOURCES,
        pattern=_RESOURCE_RE,
    )
    authority_state = _enum_string(
        raw["authority_state"], label="authority_state", allowed=_AUTHORITY_STATES
    )
    owner_gate = _bool(raw["owner_gate"], label="owner_gate")
    human_required = _bool(raw["human_required"], label="human_required")
    priority = _nonnegative_int(
        raw["priority"], label="priority", maximum=MAX_PRIORITY
    )
    return ReadinessNode(
        node_id=node_id,
        issue_number=issue_number,
        repository=repository,
        branch=branch,
        head=head,
        packet_status=packet_status,
        queue_state=queue_state,
        dependencies=dependencies,
        resources=resources,
        authority_state=authority_state,
        owner_gate=owner_gate,
        human_required=human_required,
        priority=priority,
    )


def validate_readiness_graph(payload: object) -> tuple[int, list[ReadinessNode]]:
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _reject("readiness graph schema is invalid")
    schema_version = payload["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        _reject("readiness graph schema_version is unsupported")
    if payload["kind"] != "dependency_readiness_graph":
        _reject("readiness graph kind is invalid")
    max_wip = _positive_int(payload["max_wip"], label="max_wip", maximum=MAX_WIP)
    raw_nodes = payload["nodes"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES:
        _reject("nodes must be a bounded list")
    nodes = [_parse_node(raw) for raw in raw_nodes]
    node_ids = [node.node_id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        _reject("node_id values must be unique")
    issue_keys = [(node.repository, node.issue_number) for node in nodes]
    if len(issue_keys) != len(set(issue_keys)):
        _reject("repository/issue_number pairs must be unique")
    return max_wip, nodes


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("readiness graph contains a duplicate JSON key")
        result[key] = value
    return result


def load_readiness_graph(path: Path) -> tuple[int, list[ReadinessNode]]:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_GRAPH_BYTES + 1)
    except OSError as exc:
        raise ValidationError("readiness graph is not readable") from exc
    if len(raw) > MAX_GRAPH_BYTES:
        _reject("readiness graph exceeds the bounded import size")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("readiness graph is not valid UTF-8 JSON") from exc
    return validate_readiness_graph(payload)


def _cycle_members(nodes: Iterable[ReadinessNode]) -> set[str]:
    index = {node.node_id: node for node in nodes}
    color: dict[str, int] = {}
    stack: list[str] = []
    stack_index: dict[str, int] = {}
    cyclic: set[str] = set()

    def visit(node_id: str) -> None:
        color[node_id] = 1
        stack_index[node_id] = len(stack)
        stack.append(node_id)
        for dependency in index[node_id].dependencies:
            dependency_id = dependency.node_id
            if dependency_id not in index:
                continue
            state = color.get(dependency_id, 0)
            if state == 0:
                visit(dependency_id)
            elif state == 1:
                start = stack_index[dependency_id]
                cyclic.update(stack[start:])
        stack.pop()
        stack_index.pop(node_id, None)
        color[node_id] = 2

    for node_id in sorted(index):
        if color.get(node_id, 0) == 0:
            visit(node_id)
    return cyclic


def _base_readiness(node: ReadinessNode) -> tuple[str, set[str]]:
    reasons: set[str] = set()
    if node.authority_state != "TRUSTED":
        reasons.add(f"AUTHORITY_{node.authority_state}")
    if node.owner_gate:
        reasons.add("OWNER_GATE")
    if node.human_required:
        reasons.add("HUMAN_REQUIRED_GATE")
    if reasons:
        return "HUMAN_REQUIRED", reasons

    if node.packet_status == "ACTIVE":
        if node.queue_state != "NONE":
            return "HUMAN_REQUIRED", {"ACTIVE_QUEUE_STATE_CONFLICT"}
        return "ACTIVE", set()
    if node.packet_status == "COMPLETE":
        if node.queue_state != "NONE":
            return "HUMAN_REQUIRED", {"COMPLETE_QUEUE_STATE_CONFLICT"}
        return "COMPLETE", set()
    if node.packet_status == "BLOCKED":
        return "BLOCKED", {"PACKET_BLOCKED"}
    if node.queue_state != "QUEUED":
        return "BLOCKED", {"NOT_QUEUED"}
    return "READY", set()


def plan_readiness_file(path: Path) -> dict:
    """Load and plan one bounded graph file without external side effects."""
    max_wip, nodes = load_readiness_graph(path)
    payload = {
        "schema_version": SCHEMA_VERSION,
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
    return plan_readiness(payload)


def plan_readiness(payload: object) -> dict:
    """Return a deterministic plan without any external side effect."""
    max_wip, nodes = validate_readiness_graph(payload)
    index = {node.node_id: node for node in nodes}
    cycles = _cycle_members(nodes)
    state: dict[str, str] = {}
    reasons: dict[str, set[str]] = {}
    blocked_by: dict[str, set[str]] = {node.node_id: set() for node in nodes}

    for node in nodes:
        readiness, node_reasons = _base_readiness(node)
        state[node.node_id] = readiness
        reasons[node.node_id] = set(node_reasons)
        unknown = sorted(
            dependency.node_id
            for dependency in node.dependencies
            if dependency.node_id not in index
        )
        if unknown:
            state[node.node_id] = "HUMAN_REQUIRED"
            reasons[node.node_id].add("UNKNOWN_DEPENDENCY")
            blocked_by[node.node_id].update(unknown)
        if node.node_id in cycles:
            state[node.node_id] = "HUMAN_REQUIRED"
            reasons[node.node_id].add("DEPENDENCY_CYCLE")

    # ACTIVE and COMPLETE lifecycle claims are trusted only when their entire
    # dependency chain is COMPLETE. Iterate to a fixed point so the result does
    # not depend on input node ordering.
    changed = True
    while changed:
        changed = False
        for node in sorted(nodes, key=lambda item: item.node_id):
            if node.packet_status not in {"ACTIVE", "COMPLETE"}:
                continue
            if state[node.node_id] == "HUMAN_REQUIRED":
                continue
            unsafe: list[str] = []
            incomplete: list[str] = []
            for dependency in node.dependencies:
                dependency_id = dependency.node_id
                if dependency_id not in index:
                    continue
                dependency_state = state[dependency_id]
                if dependency_state == "HUMAN_REQUIRED":
                    unsafe.append(dependency_id)
                elif dependency_state != "COMPLETE":
                    incomplete.append(dependency_id)
            if unsafe or incomplete:
                state[node.node_id] = "HUMAN_REQUIRED"
                reasons[node.node_id].add("LIFECYCLE_DEPENDENCY_CONFLICT")
                blocked_by[node.node_id].update(unsafe + incomplete)
                changed = True

    # Packet status, not trust classification, represents resident ACTIVE WIP.
    # Even an invalid/untrusted active node must continue consuming capacity
    # until an operator resolves the inconsistency.
    active = [node for node in nodes if node.packet_status == "ACTIVE"]
    active_resources: dict[str, list[str]] = {}
    for node in active:
        for resource in node.effective_resources:
            active_resources.setdefault(resource, []).append(node.node_id)

    graph_reasons: set[str] = set()
    if cycles:
        graph_reasons.add("DEPENDENCY_CYCLE")
    if any(node.authority_state != "TRUSTED" for node in nodes):
        graph_reasons.add("AUTHORITY_UNSAFE")
    if any(
        dependency.node_id not in index
        for node in nodes
        for dependency in node.dependencies
    ):
        graph_reasons.add("UNKNOWN_DEPENDENCY")
    if len(active) > max_wip:
        graph_reasons.add("ACTIVE_WIP_EXCEEDS_LIMIT")
    for owners in active_resources.values():
        if len(owners) > 1:
            graph_reasons.add("ACTIVE_RESOURCE_CONFLICT")
            for node_id in owners:
                state[node_id] = "HUMAN_REQUIRED"
                reasons[node_id].add("ACTIVE_RESOURCE_CONFLICT")
                blocked_by[node_id].update(
                    owner for owner in owners if owner != node_id
                )

    if any(
        state[node.node_id] == "HUMAN_REQUIRED"
        and node.packet_status == "ACTIVE"
        for node in nodes
    ):
        graph_reasons.add("ACTIVE_NODE_HUMAN_REQUIRED")

    # Evaluate queued dependency and active-resource gates only after lifecycle
    # states have stabilized.
    for node in nodes:
        if state[node.node_id] != "READY":
            continue
        unsafe: list[str] = []
        incomplete: list[str] = []
        for dependency in node.dependencies:
            dependency_id = dependency.node_id
            if dependency_id not in index:
                continue
            dependency_state = state[dependency_id]
            if dependency_state == "HUMAN_REQUIRED":
                unsafe.append(dependency_id)
            elif dependency_state != "COMPLETE":
                incomplete.append(dependency_id)
        if unsafe:
            state[node.node_id] = "BLOCKED"
            reasons[node.node_id].add("DEPENDENCY_HUMAN_REQUIRED")
            blocked_by[node.node_id].update(unsafe)
            continue
        if incomplete:
            state[node.node_id] = "BLOCKED"
            reasons[node.node_id].add("DEPENDENCY_NOT_COMPLETE")
            blocked_by[node.node_id].update(incomplete)
            continue
        conflicts = sorted(
            resource
            for resource in node.effective_resources
            if resource in active_resources
        )
        if conflicts:
            state[node.node_id] = "BLOCKED"
            reasons[node.node_id].add("RESOURCE_CONFLICT")
            blocked_by[node.node_id].update(
                owner
                for resource in conflicts
                for owner in active_resources[resource]
            )

    if graph_reasons:
        for node in nodes:
            if state[node.node_id] == "READY":
                state[node.node_id] = "BLOCKED"
                reasons[node.node_id].add("GRAPH_UNSAFE")

    selected: list[str] = []
    if not graph_reasons:
        slots = max(0, max_wip - len(active))
        reserved_by = {
            resource: set(owners) for resource, owners in active_resources.items()
        }
        candidates = sorted(
            (node for node in nodes if state[node.node_id] == "READY"),
            key=lambda node: (node.priority, node.node_id),
        )
        for node in candidates:
            conflicts = sorted(
                resource
                for resource in node.effective_resources
                if resource in reserved_by
            )
            if conflicts:
                state[node.node_id] = "BLOCKED"
                reasons[node.node_id].add("RESOURCE_CONFLICT")
                blocked_by[node.node_id].update(
                    owner
                    for resource in conflicts
                    for owner in reserved_by[resource]
                )
                continue
            if len(selected) >= slots:
                state[node.node_id] = "BLOCKED"
                reasons[node.node_id].add("WIP_LIMIT")
                continue
            selected.append(node.node_id)
            for resource in node.effective_resources:
                reserved_by.setdefault(resource, set()).add(node.node_id)

    graph_state = "HUMAN_REQUIRED" if graph_reasons else "READY"
    active_count = len(active)
    available_slots = (
        max(0, max_wip - active_count) if not graph_reasons else 0
    )
    results = []
    for node in sorted(nodes, key=lambda item: item.node_id):
        readiness = state[node.node_id]
        if readiness not in _READINESS_STATES:
            _reject("internal readiness state is invalid")
        results.append(
            {
                "node_id": node.node_id,
                "issue_number": node.issue_number,
                "repository": node.repository,
                "branch": node.branch,
                "head": node.head,
                "readiness": readiness,
                "selected": node.node_id in selected,
                "reasons": sorted(reasons[node.node_id]),
                "blocked_by": sorted(blocked_by[node.node_id]),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "dependency_readiness_plan",
        "graph_state": graph_state,
        "graph_reasons": sorted(graph_reasons),
        "max_wip": max_wip,
        "active_count": active_count,
        "available_slots": available_slots,
        "selected_node_ids": selected,
        "nodes": results,
    }
