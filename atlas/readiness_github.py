"""Read-only GitHub reconciliation for dependency/readiness planning."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from atlas.provenance import ValidationError
from atlas.readiness_graph import (
    SCHEMA_VERSION,
    ReadinessNode,
    load_readiness_graph,
    plan_readiness,
    validate_readiness_graph,
)

PacketFactReader = Callable[[str, int], dict[str, Any]]

_FACT_KEYS = frozenset(
    {
        "repository",
        "issue_number",
        "branch",
        "head",
        "packet_status",
        "queue_state",
    }
)
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_PACKET_STATUSES = frozenset({"ACTIVE", "PAUSED", "BLOCKED", "COMPLETE"})
_QUEUE_STATES = frozenset({"NONE", "QUEUED"})


def _validate_fact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FACT_KEYS:
        raise ValidationError("readiness GitHub fact schema is invalid")
    repository = value["repository"]
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise ValidationError("readiness GitHub fact repository is invalid")
    issue_number = value["issue_number"]
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        raise ValidationError("readiness GitHub fact issue_number is invalid")
    branch = value["branch"]
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise ValidationError("readiness GitHub fact branch is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in branch):
        raise ValidationError("readiness GitHub fact branch is invalid")
    head = value["head"]
    if not isinstance(head, str) or not _HEAD_RE.fullmatch(head):
        raise ValidationError("readiness GitHub fact head is invalid")

    packet_status = value["packet_status"]
    if not isinstance(packet_status, str) or packet_status not in _PACKET_STATUSES:
        raise ValidationError("readiness GitHub fact packet_status is invalid")
    queue_state = value["queue_state"]
    if not isinstance(queue_state, str) or queue_state not in _QUEUE_STATES:
        raise ValidationError("readiness GitHub fact queue_state is invalid")
    return {
        "repository": repository,
        "issue_number": issue_number,
        "branch": branch,
        "head": head,
        "packet_status": packet_status,
        "queue_state": queue_state,
    }


def _node_payload(node: ReadinessNode, *, authority_state: str) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "issue_number": node.issue_number,
        "repository": node.repository,
        "branch": node.branch,
        "head": node.head,
        "packet_status": node.packet_status,
        "queue_state": node.queue_state,
        "dependencies": [
            {"node_id": dep.node_id, "relation": dep.relation}
            for dep in node.dependencies
        ],
        "resources": list(node.resources),

        "authority_state": authority_state,
        "owner_gate": node.owner_gate,
        "human_required": node.human_required,
        "priority": node.priority,
    }


def _reconcile_nodes(
    max_wip: int,
    nodes: list[ReadinessNode],
    read_fact: PacketFactReader,
) -> dict[str, Any]:
    if not callable(read_fact):
        raise ValidationError("readiness GitHub fact reader is required")

    reconciled: list[dict[str, Any]] = []
    for node in sorted(nodes, key=lambda item: item.node_id):
        observed_authority = "TRUSTED"
        try:
            fact = _validate_fact(read_fact(node.repository, node.issue_number))
        except ValidationError:
            observed_authority = "UNTRUSTED"
        else:
            expected = {
                "repository": node.repository,
                "issue_number": node.issue_number,
                "branch": node.branch,
                "head": node.head,
                "packet_status": node.packet_status,
                "queue_state": node.queue_state,
            }
            if fact != expected:
                observed_authority = "STALE"

        authority = (
            observed_authority
            if node.authority_state == "TRUSTED"
            else node.authority_state
        )
        reconciled.append(_node_payload(node, authority_state=authority))


    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": reconciled,
    }


def reconcile_readiness_graph(
    payload: object,
    read_fact: PacketFactReader,
) -> dict[str, Any]:
    """Re-read canonical packet identity and fail closed on any drift."""
    max_wip, nodes = validate_readiness_graph(payload)
    return _reconcile_nodes(max_wip, nodes, read_fact)


def reconcile_readiness_graph_file(
    path: Path,
    read_fact: PacketFactReader,
) -> dict[str, Any]:
    max_wip, nodes = load_readiness_graph(path)
    return _reconcile_nodes(max_wip, nodes, read_fact)


def plan_github_reconciled_readiness(
    payload: object,
    read_fact: PacketFactReader,
) -> dict[str, Any]:
    return plan_readiness(reconcile_readiness_graph(payload, read_fact))


def plan_github_reconciled_readiness_file(
    path: Path,
    read_fact: PacketFactReader,
) -> dict[str, Any]:
    return plan_readiness(reconcile_readiness_graph_file(path, read_fact))
