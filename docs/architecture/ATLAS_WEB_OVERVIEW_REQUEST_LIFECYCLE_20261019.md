# Atlas Web overview request-scoped lifecycle validation — 2026-10-19

**Status:** isolated source remediation only; no production release authorization.

## Observed production symptom

Issue #346 owner-run package `d561eed` completed `inspect` and `prepare`, but
its **preflight failed** (`TimeoutError: timed out`) with no code/service swap.

Independent **read-only** `prod-atlas` observations before any change:

| URL (prod loopback unless noted) | Result |
| --- | --- |
| `/operations` | HTTP 200, ~0.08 seconds |
| `/providers` | HTTP 200, ~0.02 seconds |
| `/instruction-governance` | HTTP 200, ~0.92 seconds |
| Seven distinct `/projects/<id>/lifecycle` calls | HTTP 200, ~0.96–1.17 seconds each |
| `/` | HTTP 200, 7.56 seconds on one uninterrupted request; other attempts exceeded 4–6 seconds |
| Public MCP `/healthz`, Keycloak OIDC metadata | HTTP 200 with verified TLS |

The overview synchronously called `lifecycle_view` for **each** project.
Each call re-read/re-validated the same `github-lifecycle.json` and associated
metadata, then performed a repository-specific lookup. Seven projects can
spend ~7 seconds on repeated source validation alone; the remaining dashboard
work can push the **8-second operator Web timeout** over the threshold.

This does not prove the Web service is globally unhealthy. It proves the
operator `_web_ready()` home-page response gate is timing-sensitive on the
old application. Do not relax that health gate or use `/operations` to hide
an unusable home page.

## Bounded correction

- `lifecycle_views(data_root, repositories)` reads and validates the shared
  lifecycle snapshot **once per overview request**. Repository-specific
  canonical/noncanonical, freshness, work-packet, CI/test/release and human
  evidence states are independently derived from the validated data.
- The result is **never globally cached or retained across requests**.
  Subsequent requests re-read the latest snapshot. Single-project pages
  still call `lifecycle_view` and check their evidence afresh.
- Missing, malformed, stale, noncanonical and mismatched-HEAD evidence stay
  UNKNOWN, UNAVAILABLE, STALE, or other non-PASS states.
- Dedicated deterministic regressions cover seven registered projects,
  repeated request freshness and invalid snapshot fail-closed behavior,
  asserting exactly **one** validated shared read per overview.

## Deployment/release separation

Existing owner package `atlas-owner-deploy-d561eed-20261019.tar.gz`
(SHA-256 `8de896bfe71c721ac9ad5f1fbf2628fe04b4b14b6e2fa75836542ed04c61c44f`)
**does not include this fix**. The owner has already prepared its original
isolated stage, so do **not** retry its `prepare`, remove its stage or fresh
backup slots, edit its frozen source, or invoke `deploy` after a failed preflight.

Qualification of an entirely NEW immutable application HEAD, tracked manifest,
separate operator source+rollback paths, fresh backup slot, offline release
package and exact-HEAD CI remains necessary before another owner-operated
preflight. This isolated change is not permission to change an existing
Desktop Commander denial or to bypass normal production release gates.
