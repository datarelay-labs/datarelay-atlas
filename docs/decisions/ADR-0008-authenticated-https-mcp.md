# ADR-0008: Authenticated HTTPS MCP resource server

Status: Accepted
Date: 2026-09-24

## Context

Operational Phase 2 needs Cursor and ChatGPT to call Atlas retrieval over MCP.
`atlas/mcp_context.py` already defines project-scoped tool semantics, but it has
no transport. Search hits expose the user-visible `source_path`, while
provenance lookup is keyed by projection identity (`source_id@ref`). A client
that feeds a search `path` back into provenance lookup can miss, and two
projections that share a `source_path` must stay distinguishable.

The official MCP Python SDK stable line for this slice is `mcp==2.2.0`
(protocol revision 2026-07-28, Streamable HTTP).

## Minimal design gate

1. **Goal** — Serve current project-scoped retrieval on an authenticated HTTPS
   MCP endpoint without a second copy of retrieval logic.
2. **Non-goals** — An Atlas authorization server or token issuer; ChatGPT or
   Cursor live-client certification; a human UI; cross-project search;
   production reverse-proxy packaging; persisting embeddings.
3. **Affected public contract** — `python -m atlas mcp serve` with runtime
   resource URL, issuer, RFC 7662 introspection client credentials, and TLS
   certificate/key. Streamable HTTP is mounted at `/mcp`. Search hits gain an
   `identity` field. `path` remains the source path. `get_provenance` accepts
   that identity, or a source path when exactly one projection uses it.
4. **State / migration impact** — No new durable store and no registry schema
   change. Each tool call rebuilds a project retriever from current projections.
5. **Security / operations impact** — Atlas is an OAuth 2.1 resource server
   only. The SDK `TokenVerifier` and `AuthSettings` publish RFC 9728 protected
   resource metadata and enforce bearer authentication. Read tools require
   `atlas.read`. Tokens whose audience/resource is not the configured MCP
   resource URL are rejected. When both audience and resource claims are
   present, each must name that URL. An explicit introspection `iss` that
   differs from the configured issuer is rejected; an omitted `iss` stays
   acceptable. Introspection credentials and private keys stay in the
   environment or operator files, never in Git. The introspection client
   secret is not a command-line argument. Missing auth or TLS configuration
   refuses to bind.
6. **Architecture boundary** — `AtlasService` / `atlas.mcp_context` remain the
   retrieval and tool-semantics owners. `atlas.mcp_http` owns Streamable HTTP
   and resource-server wiring. The SDK owns protocol negotiation. No
   authorization-server routes are mounted.
7. **Acceptance / regression criteria** — Deterministic tests cover anonymous
   rejection, `atlas.read`, wrong scope, wrong resource, provenance round trip,
   shared source paths, cross-project isolation, and one local HTTPS process
   smoke using a generated certificate that is not committed.

## Decision

1. Pin `mcp==2.2.0` in `requirements.txt`. CI installs that file before tests.
2. Mount the SDK Streamable HTTP app at `/mcp` and terminate TLS in the
   `mcp serve` process with operator-supplied certificate and key material.
3. Verify bearer tokens by RFC 7662 introspection. The verifier is replaceable
   in tests. The production transport does not follow redirects.
4. Keep keyword retrieval for this slice. Semantic ranking remains the
   operator `search` command, not the MCP process.
5. Return projection `identity` on search hits and resolve `get_provenance`
   by that identity when `identity` is supplied. A source path resolves only
   by `source_path`, and only when it matches one projection; several matches
   fail closed as `ambiguous`. Path lookup does not consult identity keys.

## Consequences

- Operators can point an MCP client at `https://<host>:<port>/mcp` once an
  authorization server issues `atlas.read` tokens for that resource URL.
- Live ChatGPT/Cursor certification is a later packet. This ADR does not claim
  that certification.
- A later deployment slice can put a reverse proxy in front of this process.
  It must not turn Atlas into an authorization server.
