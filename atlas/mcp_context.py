"""MCP-facing retrieval/context surface (Atlas-owned library contract).

Tool semantics stay here. Streamable HTTP and OAuth resource-server transport
live in ``atlas.mcp_http`` (ADR-0008).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from atlas.provenance import Provenance, ValidationError, provenance_dict
from atlas.retrieval import Retriever, normalize_path
from atlas.security import READ_SCOPE, WRITE_SCOPE, authorize_tool, reject_canonical_mutation

WRITE_TOOL_NAMES = {"create_note"}  # placeholder non-canonical only; no Wiki writes


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any
    error: str | None = None


class AtlasContextTools:
    def __init__(
        self,
        retriever: Retriever | None = None,
        *,
        retriever_factory: Callable[[str], Retriever] | None = None,
        intelligence_factory: Callable[[str], dict[str, Any]] | None = None,
        intelligence_overview_factory: Callable[[list[str]], dict[str, Any]] | None = None,
        source_detail_factory: Callable[[str, str], dict[str, Any]] | None = None,
        operations_readiness_factory: Callable[[], dict[str, object]] | None = None,
        provider_dashboard_factory: Callable[[], dict[str, object]] | None = None,
        provider_transition_preview_factory: Callable[..., dict[str, object]] | None = None,
        decision_plane_factory: Callable[[], dict[str, object]] | None = None,
        decision_context_candidates_factory: Callable[[list[str]], dict[str, object]] | None = None,
        decision_check_candidates_factory: Callable[[list[str]], dict[str, object]] | None = None,
        instruction_governance_factory: Callable[[], dict[str, object]] | None = None,
        instruction_governance_routing_factory: Callable[[], dict[str, object]] | None = None,
        concurrency_factory: Callable[[], dict[str, object]] | None = None,
        concurrency_authorization_factory: Callable[[], dict[str, object]] | None = None,
        concurrency_effects_factory: Callable[[], dict[str, object]] | None = None,
        concurrency_joins_factory: Callable[[], dict[str, object]] | None = None,
        concurrency_execution_factory: Callable[[], dict[str, object]] | None = None,
        personal_knowledge_factory: Callable[[], dict[str, object]] | None = None,
        personal_search_factory: Callable[[str, str, int], list[Any]] | None = None,
        knowledge_search_factory: Callable[[str, list[str], str, int], dict[str, Any]] | None = None,
        provider_route_quality_factory: Callable[[], dict[str, object]] | None = None,
        decision_canary_factory: Callable[[], dict[str, object]] | None = None,
        decision_canary_admission_factory: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        if (retriever is None) == (retriever_factory is None):
            raise ValueError("AtlasContextTools requires exactly one retriever source")
        self.retriever = retriever
        self._retriever_factory = retriever_factory
        self._intelligence_factory = intelligence_factory
        self._intelligence_overview_factory = intelligence_overview_factory
        self._source_detail_factory = source_detail_factory
        self._operations_readiness_factory = operations_readiness_factory
        self._provider_dashboard_factory = provider_dashboard_factory
        self._provider_transition_preview_factory = provider_transition_preview_factory
        self._decision_plane_factory = decision_plane_factory
        self._decision_context_candidates_factory = decision_context_candidates_factory
        self._decision_check_candidates_factory = decision_check_candidates_factory
        self._instruction_governance_factory = instruction_governance_factory
        self._instruction_governance_routing_factory = instruction_governance_routing_factory
        self._concurrency_factory = concurrency_factory
        self._concurrency_authorization_factory = concurrency_authorization_factory
        self._concurrency_effects_factory = concurrency_effects_factory
        self._concurrency_joins_factory = concurrency_joins_factory
        self._concurrency_execution_factory = concurrency_execution_factory
        self._personal_knowledge_factory = personal_knowledge_factory
        self._personal_search_factory = personal_search_factory
        self._knowledge_search_factory = knowledge_search_factory
        self._provider_route_quality_factory = provider_route_quality_factory
        self._decision_canary_factory = decision_canary_factory
        self._decision_canary_admission_factory = decision_canary_admission_factory

    def _retriever_for(self, project_id: str) -> Retriever:
        if self._retriever_factory is not None:
            return self._retriever_factory(project_id)
        assert self.retriever is not None
        return self.retriever

    def list_tools(self, scopes: list[str]) -> list[dict[str, str]]:
        tools = [
            {
                "name": "search_project",
                "description": "Project-scoped keyword/hybrid retrieval with provenance",
            },
            {
                "name": "get_provenance",
                "description": "Return provenance for a projected path in a project",
            },
        ]
        if self._intelligence_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_project_intelligence",
                    "description": "Return deterministic non-authoritative concepts, links, ADR backlinks, questions, and knowledge gaps",
                }
            )
        if self._intelligence_overview_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_intelligence_overview",
                    "description": "Return deterministic cross-project intelligence for an explicit project_ids scope",
                }
            )
        if self._source_detail_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_source_detail",
                    "description": "Return one registered source with bounded validated projection content and provenance",
                }
            )
        if self._operations_readiness_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_operations_readiness",
                    "description": "Return read-only operations, runtime-health, and release-gate readiness without executing operations",
                }
            )
        if self._provider_dashboard_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_provider_dashboard",
                    "description": "Return validated provider capacity evidence and recomputed advisory broker plan",
                }
            )
        if self._provider_route_quality_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_provider_route_quality",
                    "description": "Return verified provider-route outcome measurements with no broker influence",
                }
            )
        if self._provider_transition_preview_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_provider_transition_preview",
                    "description": "Return one advisory failover recommendation without executing a provider transition",
                }
            )
        if self._decision_plane_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_decision_plane",
                    "description": "Return Decision Plane shadow/replay measurements without activation authority",
                }
            )
        if self._decision_canary_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_decision_canary_readiness",
                    "description": "Return deterministic readiness evidence for a bounded Decision Plane canary request",
                }
            )
        if self._decision_canary_admission_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_decision_plane_canary",
                    "description": "Return the bounded Decision Plane canary admission snapshot and replay-evidence binding state",
                }
            )
        if self._decision_context_candidates_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_decision_context_candidates",
                    "description": "Prepare optional-context candidates while preserving deterministic mandatory context",
                }
            )
        if self._decision_check_candidates_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_decision_focused_check_candidates",
                    "description": "Prepare affected focused-check candidates while keeping terminal release gates separate",
                }
            )
        if self._instruction_governance_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_instruction_governance",
                    "description": "Return managed instruction inventory and advisory audit history without mutation authority",
                }
            )
        if self._instruction_governance_routing_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_instruction_governance_routing",
                    "description": "Return deterministic instruction candidate routing without mutation authority",
                }
            )
        if self._concurrency_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_concurrency_admission",
                    "description": "Return provider-neutral multi-node admission and measured join evidence without dispatch authority",
                }
            )
        if self._concurrency_authorization_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_concurrency_dispatch_authorization",
                    "description": "Return exact-plan multi-node dispatch authorization with no dispatch effect authority",
                }
            )
        if self._concurrency_effects_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_concurrency_dispatch_effects",
                    "description": "Return one-shot multi-node dispatch effect receipts without join or PASS authority",
                }
            )
        if self._concurrency_joins_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_concurrency_dispatch_joins",
                    "description": "Return dispatch-bound join evidence with measurement-only PASS semantics",
                }
            )
        if self._concurrency_execution_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_concurrency_execution_cycles",
                    "description": "Return provider-neutral execution-cycle state with no engineering PASS authority",
                }
            )
        if self._personal_knowledge_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "get_personal_knowledge",
                    "description": "Return non-authoritative personal/reference source inventory, provenance state, and bounded import/quarantine metadata",
                }
            )
        if self._personal_search_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "search_personal_knowledge",
                    "description": "Search only personal/reference projections within one explicit project",
                }
            )
        if self._knowledge_search_factory is not None and READ_SCOPE in scopes:
            tools.append(
                {
                    "name": "search_knowledge",
                    "description": "Search an explicit project_ids scope with source_class all/engineering/personal filtering and provenance",
                }
            )
        if authorize_tool("create_note", scopes, write_tools=WRITE_TOOL_NAMES):
            tools.append(
                {
                    "name": "create_note",
                    "description": "Create a derived note only; never mutates GitHub canonical state",
                }
            )
        return tools

    def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        scopes: list[str],
    ) -> ToolResult:
        if not authorize_tool(tool_name, scopes, write_tools=WRITE_TOOL_NAMES):
            return ToolResult(ok=False, data=None, error="unauthorized")

        if tool_name in {"search_project", "get_provenance", "get_project_intelligence", "get_intelligence_overview", "get_source_detail", "get_operations_readiness", "get_provider_dashboard", "get_provider_route_quality", "get_provider_transition_preview", "get_decision_plane", "get_decision_canary_readiness", "get_decision_plane_canary", "get_decision_context_candidates", "get_decision_focused_check_candidates", "get_instruction_governance", "get_instruction_governance_routing", "get_concurrency_admission", "get_concurrency_dispatch_authorization", "get_concurrency_dispatch_effects", "get_concurrency_dispatch_joins", "get_concurrency_execution_cycles", "get_personal_knowledge", "search_personal_knowledge", "search_knowledge"} and READ_SCOPE not in scopes:
            return ToolResult(ok=False, data=None, error="unauthorized")

        if tool_name == "search_project":
            try:
                project_id = _required_text(args, "project_id")
                query = _required_text(args, "query")
                limit = _positive_limit(args.get("limit", 8))
                retriever = self._retriever_for(project_id)
                hits = retriever.search(project_id, query, limit=limit)
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(
                ok=True,
                data=[
                    {
                        "project_id": h.project_id,
                        "path": h.path,
                        "identity": h.identity,
                        "title": h.title,
                        "content": h.content,
                        "match": h.match,
                        "score": h.score,
                        "provenance": h.provenance,
                    }
                    for h in hits
                ],
            )

        if tool_name == "get_project_intelligence":
            if self._intelligence_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_project_intelligence")
            try:
                project_id = _required_text(args, "project_id")
                payload = self._intelligence_factory(project_id)
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_intelligence_overview":
            if self._intelligence_overview_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_intelligence_overview")
            try:
                project_ids = _required_project_ids(args, "project_ids")
                payload = self._intelligence_overview_factory(project_ids)
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_operations_readiness":
            if self._operations_readiness_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_operations_readiness")
            try:
                payload = self._operations_readiness_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_concurrency_admission":
            if self._concurrency_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_concurrency_admission")
            try:
                payload = self._concurrency_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_concurrency_dispatch_authorization":
            if self._concurrency_authorization_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_concurrency_dispatch_authorization")
            try:
                payload = self._concurrency_authorization_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_concurrency_dispatch_effects":
            if self._concurrency_effects_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_concurrency_dispatch_effects")
            try:
                payload = self._concurrency_effects_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_concurrency_dispatch_joins":
            if self._concurrency_joins_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_concurrency_dispatch_joins")
            try:
                payload = self._concurrency_joins_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_concurrency_execution_cycles":
            if self._concurrency_execution_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_concurrency_execution_cycles")
            try:
                payload = self._concurrency_execution_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_personal_knowledge":
            if self._personal_knowledge_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_personal_knowledge")
            try:
                payload = self._personal_knowledge_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "search_personal_knowledge":
            if self._personal_search_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:search_personal_knowledge")
            try:
                project_id = _required_text(args, "project_id")
                query = _required_text(args, "query")
                limit = _positive_limit(args.get("limit", 8))
                hits = self._personal_search_factory(project_id, query, limit)
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(
                ok=True,
                data=[
                    {
                        "project_id": h.project_id,
                        "path": h.path,
                        "identity": h.identity,
                        "title": h.title,
                        "content": h.content,
                        "match": h.match,
                        "score": h.score,
                        "provenance": h.provenance,
                    }
                    for h in hits
                ],
            )

        if tool_name == "search_knowledge":
            if self._knowledge_search_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:search_knowledge")
            try:
                project_ids = _required_project_ids(args, "project_ids")
                query = _required_text(args, "query")
                source_class = (_optional_text(args, "source_class") or "all").lower()
                limit_per_project = _positive_limit(args.get("limit_per_project", 5))
                payload = self._knowledge_search_factory(
                    query, project_ids, source_class, limit_per_project
                )
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_instruction_governance":
            if self._instruction_governance_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_instruction_governance")
            try:
                payload = self._instruction_governance_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_instruction_governance_routing":
            if self._instruction_governance_routing_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_instruction_governance_routing")
            try:
                payload = self._instruction_governance_routing_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_decision_plane":
            if self._decision_plane_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_decision_plane")
            try:
                payload = self._decision_plane_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_decision_canary_readiness":
            if self._decision_canary_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_decision_canary_readiness")
            try:
                payload = self._decision_canary_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_decision_plane_canary":
            if self._decision_canary_admission_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_decision_plane_canary")
            try:
                payload = self._decision_canary_admission_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_decision_context_candidates":
            if self._decision_context_candidates_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_decision_context_candidates")
            optional_paths = args.get("optional_paths", [])
            if not isinstance(optional_paths, list) or any(
                not isinstance(item, str) or not item.strip() for item in optional_paths
            ):
                return ToolResult(ok=False, data=None, error="optional_paths must be a list of repository paths")
            try:
                payload = self._decision_context_candidates_factory(
                    [item.strip() for item in optional_paths]
                )
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_decision_focused_check_candidates":
            if self._decision_check_candidates_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_decision_focused_check_candidates")
            changed_paths = args.get("changed_paths", [])
            if not isinstance(changed_paths, list) or not changed_paths or any(
                not isinstance(item, str) or not item.strip() for item in changed_paths
            ):
                return ToolResult(ok=False, data=None, error="changed_paths must be a non-empty list of repository paths")
            try:
                payload = self._decision_check_candidates_factory(
                    [item.strip() for item in changed_paths]
                )
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_provider_dashboard":
            if self._provider_dashboard_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_provider_dashboard")
            try:
                payload = self._provider_dashboard_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_provider_route_quality":
            if self._provider_route_quality_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_provider_route_quality")
            try:
                payload = self._provider_route_quality_factory()
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_provider_transition_preview":
            if self._provider_transition_preview_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_provider_transition_preview")
            try:
                current_route_id = _required_text(args, "current_route_id")
                failure_reason = _required_text(args, "failure_reason")
                prior = args.get("prior_failed_route_ids", [])
                if not isinstance(prior, list) or any(
                    not isinstance(item, str) or not item.strip() for item in prior
                ):
                    raise ValidationError("prior_failed_route_ids must be a list of route IDs")
                max_attempts = args.get("max_attempts", 3)
                if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
                    raise ValidationError("max_attempts must be an integer")
                payload = self._provider_transition_preview_factory(
                    current_route_id=current_route_id,
                    failure_reason=failure_reason,
                    prior_failed_route_ids=[item.strip() for item in prior],
                    max_attempts=max_attempts,
                )
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_source_detail":
            if self._source_detail_factory is None:
                return ToolResult(ok=False, data=None, error="unknown_tool:get_source_detail")
            try:
                project_id = _required_text(args, "project_id")
                source_id = _required_text(args, "source_id")
                payload = self._source_detail_factory(project_id, source_id)
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            return ToolResult(ok=True, data=payload)

        if tool_name == "get_provenance":
            try:
                project_id = _required_text(args, "project_id")
                identity = _optional_text(args, "identity")
                path = _optional_text(args, "path")
                if not identity and not path:
                    raise ValidationError("identity or path is required")
                retriever = self._retriever_for(project_id)
                found = _lookup_provenance(
                    retriever,
                    project_id,
                    identity=identity,
                    path=path,
                )
            except ValidationError as exc:
                return ToolResult(ok=False, data=None, error=str(exc))
            if isinstance(found, str):
                return ToolResult(ok=False, data=None, error=found)
            return ToolResult(ok=True, data=dict(provenance_dict(found)))

        if tool_name == "create_note":
            reject_canonical_mutation(str(args.get("target", "derived")))
            if WRITE_SCOPE not in scopes:
                return ToolResult(ok=False, data=None, error="unauthorized")
            # Derived-only acknowledgment; durable note store is a later phase.
            return ToolResult(
                ok=True,
                data={
                    "derived": True,
                    "canonical": False,
                    "project_id": args.get("project_id"),
                    "note": args.get("text", ""),
                },
            )

        return ToolResult(ok=False, data=None, error=f"unknown_tool:{tool_name}")


def default_read_scopes() -> list[str]:
    return [READ_SCOPE]


def _required_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} is required")
    return value


def _required_project_ids(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 32
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValidationError(f"{key} must be a non-empty list of at most 32 project IDs")
    return sorted(set(item.strip() for item in value))


def _optional_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{key} is required")
    return value.strip()


def _positive_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("limit must be a positive integer")
    return value


def _lookup_provenance(
    retriever: Retriever,
    project_id: str,
    *,
    identity: str,
    path: str,
) -> Provenance | str:
    """Resolve provenance without collapsing projections that share a source path.

    ``identity`` is the projection key (``source_id@ref``). ``path`` is the
    user-visible source path. A shared source path is ambiguous unless the
    caller passes ``identity``.
    """
    table = retriever.provenance_by_path
    if identity:
        found = table.get((project_id, identity))
        return found if found is not None else "not_found"
    # Path lookup compares source_path only. The table key is source_id@ref and
    # must not satisfy a path that happens to equal another projection's identity.
    wanted = normalize_path(path)
    matches = [
        prov
        for (pid, _key), prov in table.items()
        if pid == project_id and normalize_path(prov.source_path) == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "ambiguous"
    return "not_found"
