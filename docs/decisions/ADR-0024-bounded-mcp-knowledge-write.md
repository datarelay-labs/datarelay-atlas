# ADR-0024: Opt-in MCP candidate memory and canonical source registration

Status: **Development accepted; production authorization pending**
Date: 2026-10-17
Owner direction: implement ChatGPT-native Atlas knowledge registration without CLI/SSH.

## Goal
Let an authorized ChatGPT MCP caller retain bounded, **non-authoritative** project memory and map an already-created canonical GitHub Markdown document into Atlas search/projection with provenance. Avoid duplicating registry, memory, or sync implementations.

## Decision
- **Candidate memory:** reuse `AtlasService.ingest_memory_candidates` and memory store v2 with fixed classes, TTL, provenance, strict content bounds, secret screening, project→repository binding, deterministic replay and versioned persistence. MCP input is deliberately limited to `INTERACTION_SUMMARY` and `RUN_SUMMARY`, 1–8 items per call. AI-written memory cannot claim `CANONICAL_EVENT`. Owner can correct/forget through existing operator controls.
- **Canonical source mapping:** accept only `docs/**/*.md` in the *already registered project's* GitHub repository and configured default ref, with a valid source ID/title. Perform a verified bounded GitHub Contents fetch and secret screening **before** the registry write. Existing matching source IDs are idempotent and re-synced; conflicts fail closed. The sync creates a rebuildable derived projection with the canonical GitHub blob revision; it never commits content to GitHub.
- **Privileges:** existing `atlas.read` remains mandatory for the MCP connection. Candidate ingestion requires `atlas.write`; canonical source mapping additionally requires distinct `atlas.sources.write`. A memory-only token cannot register sources; source-only token cannot ingest memory. Existing isolated write probes remain unchanged.
- **Activation:** tool families are absent unless an operator explicitly opts in through `ATLAS_MCP_MEMORY_WRITE_ENABLED=1` or `ATLAS_MCP_SOURCE_WRITE_ENABLED=1`. Both default off; no production/OAuth/Keycloak changes are authorized by this ADR. Merely merging code is not approval to activate.
- **Audit:** use the existing tool/outcome-only `ATLAS_MCP_TOOL_EXECUTION` logger; never log arguments, token, caller identity, returned content or secret validation data. Failures do not create memory authority, CI PASS or deployment authority.

## Boundaries and rejected alternatives
No generic notes wiki, arbitrary URL/repository/ref, full transcript storage, automatic promotion of owner statements to canonical decisions, unsolicited recursive GitHub scanning or GitHub commits. No new durable schema; existing `registry.json`, `memory-candidates.json` and rebuildable projections remain canonical/derived exactly as before.

No per-user/project ACL has been introduced. Atlas is currently a single-organization service: enabling `atlas.write` gives the authenticated client candidate-write reach within that organization; stronger project ACL is a separate permission decision if multiple principals/tenants are introduced.

## Acceptance
- Real isolated HTTPS MCP tools/list/tools/call: discovery flags, authorized writes, read-back, identical replay, and read-only / wrong-write-scope denials.
- Isolated GitHub fixture: project repository binding, path rejection, conflict rejection, fetch failure before registry mutation, successful derived searchable projection/provenance, no GitHub write.
- Secret and malformed-content rejection, local data-root ownership checks, existing security tests, affected unit/integration tests and exact-HEAD CI.
- Product release quality gates and owner-approved production privilege changes **remain mandatory** before rollout. Operational success/meaningful memory effectiveness are measured from genuine future workload observations, never simulated fixture counts.

See [MCP Bounded Knowledge Write Contract](../contracts/MCP_BOUNDED_KNOWLEDGE_WRITES.md), ADR-0022 and ADR-0023.
