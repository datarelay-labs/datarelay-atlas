# Authenticated HTTPS MCP

Serve project-scoped Atlas retrieval to an MCP client. Atlas is an OAuth 2.1
resource server. An external authorization server issues tokens and answers
RFC 7662 introspection. This process does not issue tokens.

## Prerequisites

- `pip install -r requirements.txt` (`mcp==2.2.0`)
- A data root with registered projects and synced projections
- TLS certificate and private key files outside the Git worktree
- Runtime introspection client id and secret, also outside Git

## Serve

Set the public resource URL to the HTTPS URL clients will call. The path is
`/mcp`.

```bash
export ATLAS_MCP_RESOURCE_URL=https://127.0.0.1:8443/mcp
export ATLAS_MCP_ISSUER_URL=https://issuer.example
export ATLAS_MCP_INTROSPECTION_URL=https://issuer.example/oauth/introspect
export ATLAS_MCP_INTROSPECTION_CLIENT_ID=atlas-resource
export ATLAS_MCP_INTROSPECTION_CLIENT_SECRET=replace-at-runtime
export ATLAS_MCP_TLS_CERT=/path/outside/git/cert.pem
export ATLAS_MCP_TLS_KEY=/path/outside/git/key.pem
PYTHONPATH=. python3 -m atlas mcp serve --host 127.0.0.1 --port 8443
```

Flags of the same names override those variables. The process refuses to bind
when any of them is missing, when the resource URL is not HTTPS `/mcp`, or
when the certificate or key file is missing.

Protected resource metadata is published at
`/.well-known/oauth-protected-resource/mcp`. Read tools require scope
`atlas.read`. A token whose audience is a different resource is rejected.

`search_project` returns `path` and `identity`. Pass `identity` to
`get_provenance`. A source path alone is accepted only when one projection
uses it.

The MCP process uses keyword retrieval. Semantic ranking stays on
`python -m atlas search --embedding-endpoint ...`.

## Local certificate

A non-production smoke may generate a self-signed certificate in a temp
directory. Do not commit that key or certificate. Production certificates stay
outside Git.

## Non-goals

This command does not certify a live ChatGPT or Cursor session, and it does
not install a system service or public reverse proxy.
