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
from functools import partial
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
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
    tools = AtlasContextTools(retriever_factory=service.project_retriever, intelligence_factory=service.project_intelligence, intelligence_overview_factory=service.intelligence_overview, source_detail_factory=service.source_detail, operations_readiness_factory=service.operations_readiness, provider_dashboard_factory=service.provider_dashboard, provider_transition_preview_factory=service.provider_transition_preview, decision_plane_factory=service.decision_plane_dashboard, decision_context_candidates_factory=service.decision_plane_optional_context_candidates, decision_check_candidates_factory=service.decision_plane_focused_check_candidates, instruction_governance_factory=service.instruction_governance_dashboard, instruction_governance_routing_factory=service.instruction_governance_routing, instruction_governance_disposition_factory=service.instruction_governance_disposition_dashboard, instruction_governance_canary_factory=service.instruction_governance_canary_dashboard, concurrency_factory=service.concurrency_dashboard, concurrency_authorization_factory=service.concurrency_dispatch_authorization_dashboard, concurrency_effects_factory=service.concurrency_dispatch_effect_dashboard, concurrency_joins_factory=service.concurrency_dispatch_join_dashboard, concurrency_execution_factory=service.concurrency_execution_dashboard, personal_knowledge_factory=service.personal_knowledge_dashboard, personal_search_factory=lambda project_id, query, limit: service.personal_search(project_id, query, limit=limit), engineering_evidence_factory=service.engineering_evidence_dashboard, knowledge_search_factory=lambda query, project_ids, source_class, limit: service.search_across_projects(query, project_ids=project_ids, source_class=source_class, limit_per_project=limit), provider_route_quality_factory=service.provider_route_quality_dashboard, decision_canary_factory=service.decision_canary_readiness, decision_canary_admission_factory=service.decision_canary_dashboard, decision_limited_active_factory=service.decision_limited_active_dashboard, decision_focused_check_limited_active_factory=service.decision_focused_check_limited_active_dashboard, decision_measured_active_factory=service.decision_measured_active_dashboard, task_context_factory=lambda project_id, repository, workstream: service.task_context(project_id=project_id, repository=repository, workstream=workstream), project_list_factory=service.list_project_summaries, bootstrap_context_factory=lambda project_hint, repository, workstream: service.bootstrap_task_context(project_hint=project_hint, repository=repository, workstream=workstream), memory_candidates_factory=lambda project_id, repository, workstream, limit: service.memory_candidates(project_id=project_id, repository=repository, workstream=workstream, limit=limit), memory_effectiveness_factory=service.memory_effectiveness_report)
    server = MCPServer(
        name="datarelay-atlas",
        instructions=(
            "Project-scoped Atlas retrieval and engineering context for DataRelay work only. "
            "For DataRelay continuation or resume when an internal project_id is not already known, "
            "call bootstrap_datarelay_context first with an exact repository, project id, or display-name hint; "
            "use list_projects only when project discovery is needed. Keep get_task_context for callers that "
            "already know the explicit project_id or repository. These tools are not for unrelated general requests. "
            "search_project returns provenance and a projection identity; pass that identity to "
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
            "get_decision_plane_limited_active reports optional-context LIMITED_ACTIVE receipts with deterministic fallback and no release/PASS authority; "
            "get_decision_plane_focused_check_limited_active reports focused-check LIMITED_ACTIVE receipts while terminal gates remain mandatory; "
            "get_decision_plane_measured_active reports downstream measured-active quality/cost evidence and deterministic rollback state without expansion authority; "
            "Decision Plane candidate tools only prepare bounded options and never execute model choices; "
            "get_instruction_governance returns Engineering-System-referenced managed-surface audit history with no mutation authority; "
            "get_instruction_governance_routing derives current candidate routing without creating PRs or mutating instructions; "
            "get_instruction_governance_disposition reports digest-bound PR handoff evidence without PR/merge/release authority; "
            "get_instruction_governance_canary reports canary/adoption-gate evidence without GitHub mutation authority; "
            "get_concurrency_admission returns advisory multi-node admission and measurement with no dispatch authority; "
            "get_concurrency_dispatch_authorization reports exact-plan authorization with no dispatch effect authority; "
            "get_concurrency_dispatch_effects reports one-shot effect receipts without join or PASS authority; "
            "get_concurrency_dispatch_joins reports effect-bound completion measurement with measurement-only PASS semantics; "
            "get_concurrency_execution_cycles reports plan-replay-protected orchestration state with no engineering PASS authority; "
            "get_personal_knowledge and search_personal_knowledge keep personal/reference content explicitly non-authoritative; "
            "get_engineering_evidence returns bounded Engineering System evidence metadata without execution authority; "
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

    @server.custom_route("/.well-known/oauth-authorization-server/mcp", methods=["GET"], include_in_schema=False)
    @server.custom_route("/mcp/.well-known/oauth-authorization-server", methods=["GET"], include_in_schema=False)
    @server.custom_route("/.well-known/oauth-authorization-server", methods=["GET"], include_in_schema=False)
    @server.custom_route("/mcp/.well-known/openid-configuration", methods=["GET"], include_in_schema=False)
    @server.custom_route("/.well-known/openid-configuration/mcp", methods=["GET"], include_in_schema=False)
    @server.custom_route("/.well-known/openid-configuration", methods=["GET"], include_in_schema=False)
    async def oauth_discovery(_request: Request) -> JSONResponse:
        issuer = config.issuer_url.rstrip("/")
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/protocol/openid-connect/auth",
                "token_endpoint": f"{issuer}/protocol/openid-connect/token",
                "registration_endpoint": f"{issuer}/clients-registrations/openid-connect",
                "scopes_supported": ["openid", "offline_access", READ_SCOPE],
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_basic",
                    "client_secret_post",
                    "private_key_jwt",
                ],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )

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
    read_tool = partial(
        server.tool,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        meta={
            "securitySchemes": [
                {"type": "oauth2", "scopes": [READ_SCOPE]},
            ]
        },
    )

    @read_tool(
        name="list_projects",
        description="List registered DataRelay Atlas projects so a model can select one explicit read-only project scope",
        structured_output=False,
    )
    async def list_projects() -> str:
        return _call_tool(tools, "list_projects", {})

    @read_tool(
        name="bootstrap_datarelay_context",
        description="For DataRelay continuation/resume, resolve one project from an exact repository, project id, or display-name hint and return bounded current task context",
        structured_output=False,
    )
    async def bootstrap_datarelay_context(
        project_hint: str = "",
        repository: str = "",
        workstream: str = "",
    ) -> str:
        return _call_tool(
            tools,
            "bootstrap_datarelay_context",
            {
                "project_hint": project_hint,
                "repository": repository,
                "workstream": workstream,
            },
        )

    @read_tool(
        name="get_task_context",
        description="Return one bounded current task-context bootstrap with JIT retrieval references",
        structured_output=False,
    )
    async def get_task_context(
        project_id: str = "",
        repository: str = "",
        workstream: str = "",
    ) -> str:
        return _call_tool(
            tools,
            "get_task_context",
            {
                "project_id": project_id,
                "repository": repository,
                "workstream": workstream,
            },
        )

    @read_tool(
        name="get_memory_candidates",
        description="Return bounded non-authoritative candidate memory for one explicit project/repository scope",
        structured_output=False,
    )
    async def get_memory_candidates(
        project_id: str = "",
        repository: str = "",
        workstream: str = "",
        limit: int = 100,
    ) -> str:
        return _call_tool(
            tools,
            "get_memory_candidates",
            {
                "project_id": project_id,
                "repository": repository,
                "workstream": workstream,
                "limit": limit,
            },
        )

    @read_tool(
        name="get_memory_effectiveness",
        description="Return measurement-only memory/context effectiveness metrics without policy mutation",
        structured_output=False,
    )
    async def get_memory_effectiveness(project_id: str = "") -> str:
        return _call_tool(tools, "get_memory_effectiveness", {"project_id": project_id})

    @read_tool(
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

    @read_tool(
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

    @read_tool(
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

    @read_tool(
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

    @read_tool(
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

    @read_tool(
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

    @read_tool(
        name="get_provider_route_quality",
        description="Return verified provider-route outcome measurements with no broker ranking or execution authority",
        structured_output=False,
    )
    async def get_provider_route_quality() -> str:
        return _call_tool(tools, "get_provider_route_quality", {})

    @read_tool(
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

    @read_tool(
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

    @read_tool(
        name="get_decision_canary_readiness",
        description="Return deterministic readiness evidence for a bounded Decision Plane canary request",
        structured_output=False,
    )
    async def get_decision_canary_readiness() -> str:
        return _call_tool(tools, "get_decision_canary_readiness", {})

    @read_tool(
        name="get_decision_plane_canary",
        description="Return the bounded Decision Plane canary admission snapshot and replay-evidence binding state",
        structured_output=False,
    )
    async def get_decision_plane_canary() -> str:
        return _call_tool(tools, "get_decision_plane_canary", {})

    @read_tool(
        name="get_decision_plane_limited_active",
        description="Return bounded optional-context LIMITED_ACTIVE effect evidence with deterministic fallback",
        structured_output=False,
    )
    async def get_decision_plane_limited_active() -> str:
        return _call_tool(tools, "get_decision_plane_limited_active", {})

    @read_tool(
        name="get_decision_plane_focused_check_limited_active",
        description="Return focused-check LIMITED_ACTIVE evidence with terminal checks preserved",
        structured_output=False,
    )
    async def get_decision_plane_focused_check_limited_active() -> str:
        return _call_tool(
            tools,
            "get_decision_plane_focused_check_limited_active",
            {},
        )

    @read_tool(
        name="get_decision_plane_measured_active",
        description="Return measured LIMITED_ACTIVE outcomes, expansion evidence and rollback state",
        structured_output=False,
    )
    async def get_decision_plane_measured_active() -> str:
        return _call_tool(
            tools,
            "get_decision_plane_measured_active",
            {},
        )

    @read_tool(
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

    @read_tool(
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

    @read_tool(
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

    @read_tool(
        name="get_instruction_governance_routing",
        description="Return deterministic instruction candidate routing without repository mutation",
        structured_output=False,
    )
    async def get_instruction_governance_routing() -> str:
        return _call_tool(tools, "get_instruction_governance_routing", {})

    @read_tool(
        name="get_instruction_governance_disposition",
        description="Return audit-bound instruction disposition and digest-bound PR handoff evidence",
        structured_output=False,
    )
    async def get_instruction_governance_disposition() -> str:
        return _call_tool(tools, "get_instruction_governance_disposition", {})

    @read_tool(
        name="get_instruction_governance_canary",
        description="Return instruction-governance canary/adoption-gate evidence without GitHub mutation authority",
        structured_output=False,
    )
    async def get_instruction_governance_canary() -> str:
        return _call_tool(tools, "get_instruction_governance_canary", {})

    @read_tool(
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

    @read_tool(
        name="get_concurrency_dispatch_authorization",
        description="Return exact-plan multi-node dispatch authorization without dispatch effect authority",
        structured_output=False,
    )
    async def get_concurrency_dispatch_authorization() -> str:
        return _call_tool(tools, "get_concurrency_dispatch_authorization", {})

    @read_tool(
        name="get_concurrency_dispatch_effects",
        description="Return one-shot multi-node dispatch effect receipts without join or PASS authority",
        structured_output=False,
    )
    async def get_concurrency_dispatch_effects() -> str:
        return _call_tool(tools, "get_concurrency_dispatch_effects", {})

    @read_tool(
        name="get_concurrency_dispatch_joins",
        description="Return dispatch-bound completion/join evidence with measurement-only PASS semantics",
        structured_output=False,
    )
    async def get_concurrency_dispatch_joins() -> str:
        return _call_tool(tools, "get_concurrency_dispatch_joins", {})

    @read_tool(
        name="get_concurrency_execution_cycles",
        description="Return provider-neutral execution-cycle orchestration state without engineering PASS authority",
        structured_output=False,
    )
    async def get_concurrency_execution_cycles() -> str:
        return _call_tool(tools, "get_concurrency_execution_cycles", {})

    @read_tool(
        name="get_personal_knowledge",
        description="Return non-authoritative personal/reference inventory and bounded import/quarantine metadata",
        structured_output=False,
    )
    async def get_personal_knowledge() -> str:
        return _call_tool(tools, "get_personal_knowledge", {})

    @read_tool(
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

    @read_tool(
        name="get_engineering_evidence",
        description="Return bounded Engineering System evidence federation state without execution authority",
        structured_output=False,
    )
    async def get_engineering_evidence(project_ids: list[str] | None = None) -> str:
        args: dict[str, object] = {}
        if project_ids is not None:
            args["project_ids"] = project_ids
        return _call_tool(tools, "get_engineering_evidence", args)

    @read_tool(
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

    @read_tool(
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
