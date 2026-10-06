# ADR-0018: OAuth standard-443 SNI ingress

Status: Accepted
Date: 2026-10-07

## Context

Production Atlas exposes the MCP resource on standard HTTPS through a
systemd-activated TCP 443 socket, but ADR-0012 originally forwarded every TLS
connection to the MCP process on loopback 8443. The production Keycloak
authorization server is separately reachable on TCP 9443 and Atlas therefore
advertises an issuer and OAuth endpoints containing `:9443`.

ChatGPT Web successfully reaches the MCP resource and the MCP-origin OAuth
discovery compatibility endpoints, but custom MCP creation still fails before
interactive authorization. Current ChatGPT OAuth behavior uses the discovered
authorization server's authorization, token, and registration endpoints
directly. Both public DNS names resolve to the same production host, while
`auth.atlas.datarelay.run:443` currently reaches the MCP TLS backend and
therefore presents the wrong certificate.

## Minimal design gate

1. **Goal** — Make both the MCP resource and OAuth authorization server
   reachable on standard TCP 443 while retaining their existing backend TLS
   termination and Atlas read-only scope.
2. **Non-goals** — TLS termination in the ingress, new write scopes, client
   credentials, anonymous Atlas access, public Plugin publication, or removal
   of Keycloak's 9443 backend listener.
3. **Affected public contract** — `mcp.atlas.datarelay.run:443` routes to
   loopback 8443; `auth.atlas.datarelay.run:443` routes to Keycloak 9443.
   The public issuer becomes
   `https://auth.atlas.datarelay.run/realms/atlas`.
4. **State / migration impact** — No Atlas durable schema change. Production
   configuration changes are limited to ingress runtime, Keycloak public
   hostname, and Atlas issuer/introspection URLs.
5. **Security / operations impact** — The ingress inspects only TLS
   ClientHello SNI and performs TCP passthrough. It references no certificates,
   keys, tokens, or Atlas service environment. Unknown TLS SNI is rejected.
6. **Architecture boundary** — systemd owns TCP 443; the Atlas stdlib SNI ingress consumes the
   inherited listening descriptor and routes by SNI. Atlas and Keycloak
   continue to terminate TLS on loopback 8443 and 9443 respectively.
7. **Acceptance / rollback** — Both public hostnames must validate their own
   certificates on 443, OAuth metadata must contain no `:9443`, unauthenticated
   MCP remains 401 with protected-resource metadata, and rollback restores the
   previous MCP-only socket proxy plus the prior issuer URL.

## Decision

1. Replace the MCP-only `systemd-socket-proxyd` service behind
   `datarelay-atlas-ingress.socket` with the Atlas Python stdlib TCP router using the inherited
   systemd listening file descriptor.
2. Route SNI `mcp.atlas.datarelay.run` to `127.0.0.1:8443` and SNI
   `auth.atlas.datarelay.run` to `127.0.0.1:9443`. Reject other TLS SNI.
3. Do not configure the ingress with TLS termination, certificate, key, or service-env
   paths. TLS remains end-to-end from client to the selected backend.
4. The ingress service requires Atlas MCP but only wants/orders after Keycloak,
   so a Keycloak outage does not intentionally take the MCP ingress down.
5. Production `ops check --prod` requires the standard-HTTPS Keycloak issuer
   without `:9443`; generic non-production checks remain unchanged.
6. Keep the existing Keycloak 9443 listener during rollout and rollback. The
   public Keycloak hostname changes to standard HTTPS only after the SNI ingress
   is installed and validated.
