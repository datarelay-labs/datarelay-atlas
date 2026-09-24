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

from atlas.mcp_auth import HttpxIntrospectionTransport, Rfc7662TokenVerifier
from atlas.mcp_config import McpServeConfig
from atlas.mcp_context import AtlasContextTools
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
    tools = AtlasContextTools(retriever_factory=service.project_retriever)
    server = MCPServer(
        name="datarelay-atlas",
        instructions=(
            "Project-scoped Atlas retrieval. search_project returns provenance "
            "and a projection identity. Pass that identity to get_provenance."
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
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=_transport_security(config),
        host=config.bind_host,
    )


def serve_mcp(config: McpServeConfig) -> None:
    """Bind the MCP app with the operator TLS certificate. Blocks until exit."""
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
    hosts.add(f"{display}:*")
    if port:
        hosts.add(f"{display}:{port}")
    origins.add(f"{scheme}://{display}:*")
    if port:
        origins.add(f"{scheme}://{display}:{port}")
