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

Flags of the same names override those variables, except the introspection
client secret. That secret is read from `ATLAS_MCP_INTROSPECTION_CLIENT_SECRET`
or from `--introspection-client-secret-file` when the flag is set. It is not
accepted as a command-line value. The process refuses to bind when any required
setting is missing, when the resource URL is not HTTPS `/mcp`, or when the
certificate, key, or secret file is missing.

An introspection response that includes `iss` must match the configured issuer.
A response that omits `iss` remains acceptable. When both `aud` and `resource`
are present, each must identify this MCP resource URL. An `aud` list is valid
when it includes that URL.

Protected resource metadata is published at
`/.well-known/oauth-protected-resource/mcp`. Read tools require scope
`atlas.read`. A token whose audience is a different resource is rejected.

`search_project` returns `path` and `identity`. Pass `identity` to
`get_provenance`. A source path alone is accepted only when one projection
uses it. Path lookup compares `source_path` and does not treat another
projection's identity key as a path.

The MCP process uses keyword retrieval. Semantic ranking stays on
`python -m atlas search --embedding-endpoint ...`.

## Local certificate

A non-production smoke may generate a self-signed certificate in a temp
directory. Do not commit that key or certificate. Production certificates stay
outside Git.

## Non-goals

This command does not certify a live ChatGPT or Cursor session, and it does
not install a system service or public reverse proxy.
