# ADR-0023: MCP-primary Atlas acceptance with retained UI regression

Status: Accepted by explicit owner direction, 2026-10-14
Supersedes: the browser-only interpretation of Atlas acceptance in ADR-0016.

## Decision and minimum design

1. Goal: prove ChatGPT can actually resume a project using current scoped Atlas
   context, check sources against GitHub, and use that context in real work.
2. Non-goals: remove the UI, weaken authentication or schema validation, waive
   production approval, fabricate usage/effectiveness, or require owner GUI labor.
3. Public contract: keep existing MCP tools, CLI and read-only browser unchanged;
   classify the surface as mixed (MCP primary). Retain BOTH mandatory user gates,
   actual-browser regression, same-HEAD binding and full scenario disposition.
4. State: no data migration, credential change, external exposure or API change.
5. Security: rejected/missing credentials, wrong scope, project ambiguity, stale
   evidence and dangerous inputs must remain fail closed. Prior failed runs are
   retained, not relabeled PASS. Any new platform denial stops that action.
6. Execution: CHATGPT_CHAT is both the actual MCP user and auditor. A standard MCP
   client/REPL is a transport driver, not an automated scenario executor. Fixture
   setup and deterministic regressions support but do not replace direct actions.
   Owner-operated mode remains optional only when explicitly requested for a run.
7. Acceptance: actual TLS MCP calls, scoped discovery/resume/search/source and
   provenance, explicit cross-project scope, unknown/stale truth, fresh session,
   privacy-minimal event correlation, real browser parity and write rejection,
   immutable candidate, data unchanged by reads, cleanup, current CI and rollback.

## Why

The previous release profile mistook the dashboard for the sole primary user.
Its browser-only E2E did not prove the core MCP workflow and imposed manual
operator work unrelated to the owner's intended usage. The owner explicitly
corrected this: Atlas is used by ChatGPT, and ChatGPT must test and operate it.
This decision changes that product mapping, not the shared methodology or
security boundary. All shipped UI safety properties still require verification.

## Operational completion versus adoption measurement

Deploy the approved readiness and tool-event logging correction only after
candidate qualification. Then invoke the native connected MCP and correlate real
server events, rather than declaring tool use from HTTP handshakes. One verified
ChatGPT workflow is scoped evidence, not universal automatic adoption; efficacy
remains insufficient until genuine comparable observations accumulate.
