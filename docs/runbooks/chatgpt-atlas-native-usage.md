# Qualify actual ChatGPT → DataRelay Atlas MCP usage

This is a human-observable client test, not a server-health test or a
synthetic memory-effectiveness measurement.

## 1. Check the existing private plugin; do not create a replacement

The maintained DataRelay Atlas personal plugin points to
https://mcp.atlas.datarelay.run/mcp. On ChatGPT web, open
https://chatgpt.com/plugins, locate the existing DataRelay Atlas personal
plugin, and check that it is installed and available for this conversation.
If an OAuth connection is expired, follow the normal ChatGPT authorization
prompt. Do not paste access tokens, passwords or private keys into a chat.
A published plugin release or repository engineering rule does not
install MCP tools into every existing conversation.

In a fresh ChatGPT chat, type @ and select DataRelay Atlas, then ask:

> Use the DataRelay Atlas plugin only. Call the native list_projects tool
> with empty arguments. Report the actual tool name and project count.
> Do not use SSH, a terminal, GitHub, cached answers or browsing.

For a repository-scoped continuation, call bootstrap_datarelay_context
with repository datarelay-labs/engineering-system. A real scoped call
qualifies that conversation, not automatic activation in other chats.

## 2. Public authentication contract, without credentials

- GET /healthz on https://mcp.atlas.datarelay.run should be HTTP 200.
- GET /.well-known/oauth-protected-resource/mcp should advertise exactly
  https://mcp.atlas.datarelay.run/mcp as the resource and the Atlas
  Keycloak authorization server.
- Anonymous POST /mcp should be HTTP 401 with a standard
  WWW-Authenticate Bearer challenge pointing to resource metadata.
- OAuth public discovery should advertise authorization-code, refresh
  tokens, S256 PKCE, and offline_access. These alone do not prove that
  the user's ChatGPT OAuth authorization or plugin tool mount works.
  Do not turn off authentication or add atlas.write as a workaround.

## 3. Real tool execution vs. HTTP and refresh activity

After the separately reviewed instrumented service version is deployed,
a successfully dispatched authenticated MCP tool call logs an event like:

  ATLAS_MCP_TOOL_EXECUTION tool=bootstrap_datarelay_context outcome=SUCCESS

Other outcomes are BLOCK and ERROR. Events contain no tokens, client IDs,
owner identity, prompts, tool arguments, response bodies or error detail.
Discovery, tools/list, authentication failure, health checks and 15-minute
projection refresh are not counted as successful tool execution.

On the authorized Atlas host, aggregate only these events:

~~~sh
journalctl -u datarelay-atlas.service --since '24 hours ago' --no-pager -o cat |
  grep -F 'ATLAS_MCP_TOOL_EXECUTION ' |
  sed -n 's/.*ATLAS_MCP_TOOL_EXECUTION tool=\([a-z0-9_]*\) outcome=\([A-Z]*\).*/\1 \2/p' |
  sort | uniq -c
~~~

Server-side tool calls cannot identify which ChatGPT conversation made
them. Pair an actual ChatGPT native tool trace with the matching request
window before attributing it to ChatGPT. Do not fabricate effectiveness
observations or treat ordinary HTTP POST 200 as one tool execution.

## 4. Failure classification

| Observation | Interpretation |
| --- | --- |
| Healthy server but no native tool in chat | The conversation is not confirmed connected |
| Anonymous MCP 401 | Correct auth enforcement; not success |
| Native list_projects succeeds | Real MCP connection in that conversation |
| Native scoped bootstrap matches repository | Project context retrieved in that conversation |
| POST /mcp 200, no tool event | Could be handshake/discovery |
| Tool event SUCCESS without matching chat trace | Backend use observed, client not proven |
| No verified outcome observation | Memory effectiveness not established |

Do not substitute SSH/CLI output for a failed native Atlas MCP request.
Continue ordinary Git-backed engineering when Atlas context is optional.
