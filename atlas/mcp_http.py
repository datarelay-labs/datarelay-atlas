"""Authenticated Streamable HTTP MCP resource server.

Design gate (ADR-0008):
- Goal: expose current project-scoped retrieval over HTTPS MCP.
- Non-goals: an authorization server, token issuance, ChatGPT/Cursor live
  E2E, a web UI, and cross-project search.
- Contract: ``python -m atlas mcp serve`` binds TLS and mounts ``/mcp``.
- State: no new durable store. Tools rebuild a project retriever per call.
- Security: OAuth 2.1 resource server only. ``atlas.read`` and the configured
  resource URL are required. Introspection credentials and TLS keys stay
  outside Git.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from atlas.mcp_auth import HttpxIntrospectionTransport, Rfc7662TokenVerifier
from atlas.mcp_config import McpServeConfig
from atlas.mcp_context import AtlasContextTools
from atlas.ops import data_root_runtime_ready, require_ready_to_bind
from atlas.provenance import ValidationError
from atlas.security import READ_SCOPE
from atlas.service import AtlasService

_UNLISTED_BIND_HOSTS = frozenset({"0.0.0.0", "::", ""})


def build_mcp_application(
    service: AtlasService,
    config: McpServeConfig,
    verifier: TokenVerifier,
) -> Starlette:
    """SDK Streamable HTTP app with resource-server auth and Atlas tools."""
    tools = AtlasContextTools(retriever_factory=service.project_retriever, intelligence_factory=service.project_intelligence, intelligence_overview_factory=service.intelligence_overview, source_detail_factory=service.source_detail, operations_readiness_factory=service.operations_readiness, provider_dashboard_factory=service.provider_dashboard, provider_transition_preview_factory=service.provider_transition_preview, decision_plane_factory=service.decision_plane_dashboard, decision_context_candidates_factory=service.decision_plane_optional_context_candidates, decision_check_candidates_factory=service.decision_plane_focused_check_candidates, instruction_governance_factory=service.instruction_governance_dashboard, instruction_governance_routing_factory=service.instruction_governance_routing, instruction_governance_disposition_factory=service.instruction_governance_disposition_dashboard, concurrency_factory=service.concurrency_dashboard, personal_knowledge_factory=service.personal_knowledge_dashboard, personal_search_factory=lambda project_id, query, limit: service.personal_search(project_id, query, limit=limit), knowledge_search_factory=lambda query, project_ids, source_class, limit: service.search_across_projects(query, project_ids=project_ids, source_class=source_class, limit_per_project=limit), provider_route_quality_factory=service.provider_route_quality_dashboard, decision_canary_factory=service.decision_canary_readiness, decision_canary_admission_factory=service.decision_canary_dashboard)
    server = MCPServer(
        name="datarelay-atlas",
        instructions=(
            "Project-scoped Atlas retrieval and engineering context. search_project "
            "returns provenance and a projection identity; pass that identity to "
            "get_provenance. get_source_detail returns bounded validated projection "
            "content. get_project_intelligence returns non-authoritative derived context; "
            "get_intelligence_overview requires an explicit project_ids scope; "
            "get_operations_readiness is read-only and never executes operational actions; "
            "get_provider_dashboard recomputes advisory broker selection without provider execution; "
            "get_provider_route_quality reports verified route outcomes with no broker influence; "
            "get_provider_transition_preview returns ADVISORY_ONLY failover planning without effect authority; "
            "get_decision_plane returns SHADOW/REPLAY measurements with no activation authority; "
            "get_decision_canary_readiness reports REPLAY_PASS-derived request readiness without activation or execution authority; "
            "get_decision_plane_canary reports the scoped canary admission snapshot and stale evidence binding without execution authority; "
            "Decision Plane candidate tools only prepare bounded options and never execute model choices; "
            "get_instruction_governance returns Engineering-System-referenced managed-surface audit history with no mutation authority; "
            "get_instruction_governance_routing derives current candidate routing without creating PRs or mutating instructions; "
            "get_instruction_governance_disposition reports digest-bound PR handoff evidence without PR/merge/release authority; "
            "get_concurrency_admission returns advisory multi-node admission and measurement with no dispatch authority; "
            "get_personal_knowledge and search_personal_knowledge keep personal/reference content explicitly non-authoritative; "
            "search_knowledge requires explicit project_ids and supports all/engineering/personal source-class filtering."
        ),
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=config.issuer_url,
            resource_server_url=config.resource_url,
            required_scopes=[READ_SCOPE],
            validate_token_resource=True,
        ),
    )
    _register_tools(server, tools)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> JSONResponse:
        if data_root_runtime_ready(config.data_root):
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=_transport_security(config),
        host=config.bind_host,
    )


def serve_mcp(config: McpServeConfig) -> None:
    """Bind the MCP app with the operator TLS certificate. Blocks until exit."""
    require_ready_to_bind(config)
    import uvicorn

    service = AtlasService(config.data_root)
    verifier = Rfc7662TokenVerifier(
        introspection_url=config.introspection_url,
        client_id=config.introspection_client_id,
        client_secret=config.introspection_client_secret,
        resource_url=config.resource_url,
        issuer_url=config.issuer_url,
        transport=HttpxIntrospectionTransport(),
    )
    app = build_mcp_application(service, config, verifier)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.bind_host,
            port=config.port,
            ssl_certfile=str(config.tls_cert),
            ssl_keyfile=str(config.tls_key),
            log_level="info",
        )
    )
    server.run()


def _register_tools(server: MCPServer, tools: AtlasContextTools) -> None:
    @server.tool(
        name="search_project",
        description="Project-scoped keyword retrieval with attributable provenance",
        structured_output=False,
    )
    async def search_project(project_id: str, query: str, limit: int = 8) -> str:
        return _call_tool(
            tools,
            "search_project",
            {"project_id": project_id, "query": query, "limit": limit},
        )

    @server.tool(
        name="search_knowledge",
        description="Search an explicit project scope with all/engineering/personal filtering and attributable provenance",
        structured_output=False,
    )
    async def search_knowledge(
        project_ids: list[str],
        query: str,
        source_class: str = "all",
        limit_per_project: int = 5,
    ) -> str:
        return _call_tool(
            tools,
            "search_knowledge",
            {
                "project_ids": project_ids,
                "query": query,
                "source_class": source_class,
                "limit_per_project": limit_per_project,
            },
        )

    @server.tool(
        name="get_project_intelligence",
        description="Return deterministic non-authoritative project intelligence from validated projections",
        structured_output=False,
    )
    async def get_project_intelligence(project_id: str) -> str:
        return _call_tool(
            tools,
            "get_project_intelligence",
            {"project_id": project_id},
        )

    @server.tool(
        name="get_intelligence_overview",
        description="Return deterministic cross-project intelligence for an explicit project_ids scope",
        structured_output=False,
    )
    async def get_intelligence_overview(project_ids: list[str]) -> str:
        return _call_tool(
            tools,
            "get_intelligence_overview",
            {"project_ids": project_ids},
        )

    @server.tool(
        name="get_operations_readiness",
        description="Return read-only runtime, operations, and release readiness without executing operations",
        structured_output=False,
    )
    async def get_operations_readiness() -> str:
        return _call_tool(
            tools,
            "get_operations_readiness",
            {},
        )

    @server.tool(
        name="get_provider_dashboard",
        description="Return validated provider capacity evidence and recomputed advisory broker state",
        structured_output=False,
    )
    async def get_provider_dashboard() -> str:
        return _call_tool(
            tools,
            "get_provider_dashboard",
            {},
        )

    @server.tool(
        name="get_provider_route_quality",
        description="Return verified provider-route outcome measurements with no broker ranking or execution authority",
        structured_output=False,
    )
    async def get_provider_route_quality() -> str:
        return _call_tool(tools, "get_provider_route_quality", {})

    @server.tool(
        name="get_provider_transition_preview",
        description="Return one advisory failover recommendation without executing a provider transition",
        structured_output=False,
    )
    async def get_provider_transition_preview(
        current_route_id: str,
        failure_reason: str,
        prior_failed_route_ids: list[str] | None = None,
        max_attempts: int = 3,
    ) -> str:
        return _call_tool(
            tools,
            "get_provider_transition_preview",
            {
                "current_route_id": current_route_id,
                "failure_reason": failure_reason,
                "prior_failed_route_ids": prior_failed_route_ids or [],
                "max_attempts": max_attempts,
            },
        )

    @server.tool(
        name="get_decision_plane",
        description="Return Decision Plane shadow/replay measurements without activation authority",
        structured_output=False,
    )
    async def get_decision_plane() -> str:
        return _call_tool(
            tools,
            "get_decision_plane",
            {},
        )

    @server.tool(
        name="get_decision_canary_readiness",
        description="Return deterministic readiness evidence for a bounded Decision Plane canary request",
        structured_output=False,
    )
    async def get_decision_canary_readiness() -> str:
        return _call_tool(tools, "get_decision_canary_readiness", {})

    @server.tool(
        name="get_decision_plane_canary",
        description="Return the bounded Decision Plane canary admission snapshot and replay-evidence binding state",
        structured_output=False,
    )
    async def get_decision_plane_canary() -> str:
        return _call_tool(tools, "get_decision_plane_canary", {})

    @server.tool(
        name="get_decision_context_candidates",
        description="Prepare optional-context candidates while preserving mandatory repository context",
        structured_output=False,
    )
    async def get_decision_context_candidates(optional_paths: list[str] | None = None) -> str:
        return _call_tool(
            tools,
            "get_decision_context_candidates",
            {"optional_paths": optional_paths or []},
        )

    @server.tool(
        name="get_decision_focused_check_candidates",
        description="Prepare affected focused-check candidates while keeping terminal release gates separate",
        structured_output=False,
    )
    async def get_decision_focused_check_candidates(changed_paths: list[str]) -> str:
        return _call_tool(
            tools,
            "get_decision_focused_check_candidates",
            {"changed_paths": changed_paths},
        )

    @server.tool(
        name="get_instruction_governance",
        description="Return managed instruction inventory and advisory audit history without mutation authority",
        structured_output=False,
    )
    async def get_instruction_governance() -> str:
        return _call_tool(
            tools,
            "get_instruction_governance",
            {},
        )

    @server.tool(
        name="get_instruction_governance_routing",
        description="Return deterministic instruction candidate routing without repository mutation",
        structured_output=False,
    )
    async def get_instruction_governance_routing() -> str:
        return _call_tool(tools, "get_instruction_governance_routing", {})

    @server.tool(
        name="get_instruction_governance_disposition",
        description="Return audit-bound instruction disposition and digest-bound PR handoff evidence",
        structured_output=False,
    )
    async def get_instruction_governance_disposition() -> str:
        return _call_tool(tools, "get_instruction_governance_disposition", {})

    @server.tool(
        name="get_concurrency_admission",
        description="Return provider-neutral multi-node admission and measured join evidence without dispatch authority",
        structured_output=False,
    )
    async def get_concurrency_admission() -> str:
        return _call_tool(
            tools,
            "get_concurrency_admission",
            {},
        )

    @server.tool(
        name="get_personal_knowledge",
        description="Return non-authoritative personal/reference inventory and bounded import/quarantine metadata",
        structured_output=False,
    )
    async def get_personal_knowledge() -> str:
        return _call_tool(tools, "get_personal_knowledge", {})

    @server.tool(
        name="search_personal_knowledge",
        description="Search only personal/reference projections within one explicit project",
        structured_output=False,
    )
    async def search_personal_knowledge(project_id: str, query: str, limit: int = 8) -> str:
        return _call_tool(
            tools,
            "search_personal_knowledge",
            {"project_id": project_id, "query": query, "limit": limit},
        )

    @server.tool(
        name="get_source_detail",
        description="Return one registered source with bounded validated projection content and provenance",
        structured_output=False,
    )
    async def get_source_detail(project_id: str, source_id: str) -> str:
        return _call_tool(
            tools,
            "get_source_detail",
            {"project_id": project_id, "source_id": source_id},
        )

    @server.tool(
        name="get_provenance",
        description=(
            "Return provenance for one projection. Pass the search hit identity "
            "when more than one projection shares a source path."
        ),
        structured_output=False,
    )
    async def get_provenance(project_id: str, path: str = "", identity: str = "") -> str:
        args: dict[str, object] = {"project_id": project_id}
        if path:
            args["path"] = path
        if identity:
            args["identity"] = identity
        return _call_tool(tools, "get_provenance", args)


def _call_tool(tools: AtlasContextTools, name: str, args: dict[str, object]) -> str:
    token = get_access_token()
    if token is None or READ_SCOPE not in list(token.scopes):
        raise ToolError("unauthorized")
    try:
        result = tools.call(name, args, scopes=list(token.scopes))
    except ValidationError as exc:
        raise ToolError(str(exc)) from exc
    if not result.ok:
        raise ToolError(result.error or "error")
    return json.dumps(result.data, sort_keys=True)


def _transport_security(config: McpServeConfig) -> TransportSecuritySettings:
    resource = urlsplit(config.resource_url)
    hosts: set[str] = set()
    origins: set[str] = set()
    _add_host(hosts, origins, resource.hostname or "", resource.port, resource.scheme)
    if config.bind_host not in _UNLISTED_BIND_HOSTS:
        _add_host(hosts, origins, config.bind_host, config.port, "https")
        _add_host(hosts, origins, config.bind_host, config.port, "http")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def _add_host(hosts: set[str], origins: set[str], host: str, port: int | None, scheme: str) -> None:
    if not host:
        return
    display = f"[{host}]" if ":" in host and not host.startswith("[") else host
    hosts.add(display)
    hosts.add(f"{display}:*")
    if port:
        hosts.add(f"{display}:{port}")
    origins.add(f"{scheme}://{display}")
    origins.add(f"{scheme}://{display}:*")
    if port:
        origins.add(f"{scheme}://{display}:{port}")
