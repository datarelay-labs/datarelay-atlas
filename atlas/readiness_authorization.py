"""Fail-closed single-effect authorization over fresh readiness evidence.

This module authorizes an effect intent only. It never performs the effect.
Any future mutator must recompute authorization at its own effect boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from atlas.provenance import ValidationError
from atlas.readiness_github import (
    PacketFactReader,
    plan_github_reconciled_readiness_file,
)
from atlas.readiness_graph import load_readiness_graph

AUTHORIZATION_SCHEMA_VERSION = 1
PlanReader = Callable[[], dict[str, Any]]

_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "graph_state",
        "graph_reasons",
        "max_wip",
        "active_count",
        "available_slots",
        "selected_node_ids",
        "nodes",
    }
)
_PLAN_NODE_KEYS = frozenset(
    {
        "node_id",
        "issue_number",
        "repository",
        "branch",
        "head",
        "readiness",
        "selected",
        "reasons",
        "blocked_by",
    }
)
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,254}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def _deny(*reasons: str, plan_digest: str | None = None) -> dict[str, Any]:
    normalized = sorted(set(reasons))
    if not normalized:
        normalized = ["AUTHORIZATION_DENIED"]
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "kind": "readiness_single_effect_authorization",
        "decision": "DENY",
        "reasons": normalized,
        "plan_digest": plan_digest,
        "selected_node": None,
    }


def _allow(*, plan_digest: str, selected_node: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "kind": "readiness_single_effect_authorization",
        "decision": "ALLOW",
        "reasons": [],
        "plan_digest": plan_digest,
        "selected_node": selected_node,
    }


def _canonical_plan_digest(plan: dict[str, Any]) -> str:
    encoded = json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_branch(value: object) -> bool:
    if not isinstance(value, str) or not _BRANCH_RE.fullmatch(value):
        return False
    components = value.split("/")
    return not (
        ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", "."))
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in components
        )
    )


def _validate_reason_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be a list")
    reasons: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _REASON_RE.fullmatch(item):
            raise ValidationError(f"{label} contains invalid reason")
        reasons.append(item)
    if reasons != sorted(set(reasons)):
        raise ValidationError(f"{label} must be sorted and unique")
    return reasons


def _validate_stable_plan(plan: object) -> dict[str, Any]:
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        raise ValidationError("readiness authorization plan schema is invalid")
    schema_version = plan["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
        or plan["kind"] != "dependency_readiness_plan"
    ):
        raise ValidationError("readiness authorization plan identity is invalid")

    max_wip = plan["max_wip"]
    if isinstance(max_wip, bool) or not isinstance(max_wip, int) or max_wip < 1:
        raise ValidationError("readiness authorization max_wip is invalid")
    for field in ("active_count", "available_slots"):
        value = plan[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"readiness authorization {field} is invalid")

    graph_state = plan["graph_state"]
    if not isinstance(graph_state, str) or graph_state not in {
        "READY",
        "HUMAN_REQUIRED",
    }:
        raise ValidationError("readiness authorization graph_state is invalid")
    _validate_reason_list(plan["graph_reasons"], label="graph_reasons")

    selected = plan["selected_node_ids"]
    if not isinstance(selected, list) or any(
        not isinstance(item, str) or not _NODE_ID_RE.fullmatch(item)
        for item in selected
    ):
        raise ValidationError("readiness authorization selected nodes are invalid")
    if len(selected) != len(set(selected)):
        raise ValidationError("readiness authorization selected nodes duplicate")

    nodes = plan["nodes"]
    if not isinstance(nodes, list) or len(nodes) > 256:
        raise ValidationError("readiness authorization nodes must be a bounded list")
    seen: set[str] = set()
    selected_flags: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != _PLAN_NODE_KEYS:
            raise ValidationError("readiness authorization node schema is invalid")
        node_id = node["node_id"]
        if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
            raise ValidationError("readiness authorization node_id is invalid")
        if node_id in seen:
            raise ValidationError("readiness authorization node_id duplicates")
        seen.add(node_id)

        issue_number = node["issue_number"]
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or issue_number < 1
        ):
            raise ValidationError("readiness authorization issue_number is invalid")
        repository = node["repository"]
        if (
            not isinstance(repository, str)
            or not _REPOSITORY_RE.fullmatch(repository)
        ):
            raise ValidationError("readiness authorization repository is invalid")
        if not _valid_branch(node["branch"]):
            raise ValidationError("readiness authorization branch is invalid")
        head = node["head"]
        if not isinstance(head, str) or not _HEAD_RE.fullmatch(head):
            raise ValidationError("readiness authorization head is invalid")
        readiness = node["readiness"]
        if not isinstance(readiness, str) or readiness not in {
            "ACTIVE",
            "COMPLETE",
            "READY",
            "BLOCKED",
            "HUMAN_REQUIRED",
        }:
            raise ValidationError("readiness authorization readiness is invalid")
        if not isinstance(node["selected"], bool):
            raise ValidationError("readiness authorization selected flag is invalid")
        node_reasons = _validate_reason_list(node["reasons"], label="node reasons")
        blocked_by = node["blocked_by"]
        if not isinstance(blocked_by, list) or any(
            not isinstance(item, str) or not _NODE_ID_RE.fullmatch(item)
            for item in blocked_by
        ):
            raise ValidationError("readiness authorization blocked_by is invalid")
        if blocked_by != sorted(set(blocked_by)):
            raise ValidationError("readiness authorization blocked_by is not canonical")
        if node["selected"]:
            selected_flags.add(node_id)
            if readiness != "READY" or node_reasons or blocked_by:
                raise ValidationError(
                    "readiness authorization selected node is inconsistent"
                )

    if any(node_id not in seen for node_id in selected):
        raise ValidationError("readiness authorization selected node is missing")
    if set(selected) != selected_flags:
        raise ValidationError("readiness authorization selection is inconsistent")
    if graph_state == "READY" and plan["graph_reasons"]:
        raise ValidationError("readiness authorization ready graph has reasons")
    if graph_state == "HUMAN_REQUIRED" and not plan["graph_reasons"]:
        raise ValidationError("readiness authorization unsafe graph lacks reasons")
    return plan


def authorize_single_effect(plan_reader: PlanReader) -> dict[str, Any]:
    """Authorize exactly one effect intent from two identical fresh plans."""
    if not callable(plan_reader):
        raise ValidationError("readiness authorization plan reader is required")
    try:
        first = plan_reader()
        second = plan_reader()
    except ValidationError:
        return _deny("RECONCILIATION_ERROR")

    if first != second:
        return _deny("PLAN_CHANGED_BETWEEN_READS")

    try:
        plan = _validate_stable_plan(first)
    except ValidationError:
        return _deny("PLAN_INVALID")

    digest = _canonical_plan_digest(plan)
    if plan["max_wip"] != 1:
        return _deny("MAX_WIP_NOT_ONE", plan_digest=digest)

    if plan["graph_state"] != "READY":
        reasons = ["GRAPH_NOT_READY"]
        reasons.extend(f"GRAPH_{reason}" for reason in plan["graph_reasons"])
        return _deny(*reasons, plan_digest=digest)

    selected_ids = plan["selected_node_ids"]
    if len(selected_ids) != 1:
        return _deny("SELECTED_NODE_COUNT_NOT_ONE", plan_digest=digest)

    selected_id = selected_ids[0]
    matches = [node for node in plan["nodes"] if node["node_id"] == selected_id]
    if len(matches) != 1:
        return _deny("PLAN_INVALID", plan_digest=digest)
    node = matches[0]
    if node["readiness"] != "READY" or node["selected"] is not True:
        return _deny("PLAN_INVALID", plan_digest=digest)

    selected_node = {
        "node_id": node["node_id"],
        "repository": node["repository"],
        "issue_number": node["issue_number"],
        "branch": node["branch"],
        "head": node["head"],
    }
    return _allow(plan_digest=digest, selected_node=selected_node)


def authorize_github_single_effect_file(
    path: Path,
    read_fact: PacketFactReader,
) -> dict[str, Any]:
    """Read a graph, double-reconcile GitHub facts, and authorize no effect itself."""
    max_wip, _nodes = load_readiness_graph(path)
    if max_wip != 1:
        return _deny("MAX_WIP_NOT_ONE")

    return authorize_single_effect(
        lambda: plan_github_reconciled_readiness_file(path, read_fact)
    )
