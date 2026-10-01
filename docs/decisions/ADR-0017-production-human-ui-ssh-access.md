# ADR-0017: Production Human UI through SSH-authenticated loopback

Status: Accepted
Date: 2026-10-01

## Context

Atlas Core now includes a read-only Human UI, but the existing prod-atlas launch
contract deploys only the authenticated MCP service. Publishing a second public
HTTPS application would require a new public authentication/session boundary and
a 443 routing redesign. The initial product is self-hosted, single-organization,
and primarily operated by one authenticated engineering owner.

## Decision

1. Run the Human UI as `datarelay-atlas-web.service` under `atlas:atlas`.
2. Bind only `127.0.0.1:8788`. The existing Web UI loopback Host-authority and
   GET-only/read-only restrictions remain mandatory.
3. Remote production access uses authenticated SSH local forwarding from the
   operator machine to `prod-atlas:127.0.0.1:8788`. SSH is the initial remote
   authentication boundary.
4. Do not expose port 8788 publicly and do not add the Web UI to the existing
   MCP port-443 socket proxy.
5. The Web UI unit receives no MCP service env file or secrets and has
   read-only access to `/var/lib/datarelay-atlas`.
6. A public `app.atlas.datarelay.run` surface is future work and requires a
   separate explicit authentication/exposure ADR.
7. Surface Reconciliation and Full User E2E must use an actual browser against
   the production UI on the exact deployed candidate; unit/component checks do
   not establish those PASS states.

## Consequences

This completes a secure initial production Human UI path without increasing the
public attack surface or introducing a second login system. It is intentionally
optimized for the current self-hosted single-user product boundary rather than
for SaaS/public-web convenience.
