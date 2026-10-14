# Atlas Surface Reconciliation Contract

## Scope and executor

Atlas is a mixed public-surface product: authenticated MCP is the primary AI
client workflow; CLI and read-only browser are operator/support surfaces.
ADR-0023 records the owner-approved mapping. CHATGPT_CHAT directly uses MCP and
independently audits results in AI_DIRECT mode. The owner need not drive the UI.
Explicit HUMAN_OWNER_WITH_CHATGPT_AUDIT remains optional for a separately chosen
human run; its unchanged canonical identity/evidence rules continue to apply.

## Required environment and immutable identity

Read this entire contract before acting. Record clean exact candidate HEAD,
committed contract SHA-256, archive/file hashes, isolated state root, timestamp,
MCP endpoint, TLS identity, test-scoped credentials (never logged), client version
and real Chromium/Chrome version. Use at least two synthetic projects plus
engineering and personal sources. Production credentials are never copied into
fixtures. Fixture setup has no authority to declare an actual user PASS.

## Mandatory capability inventory

- S01: discover real MCP tool descriptors and annotations; distinguish read tools
  from explicitly scoped isolated write probes. HTTP health/tools-list alone is
  not an application tool execution.
- S02: list_projects; bootstrap_datarelay_context with one explicit project hint;
  get_task_context with project_id/workstream; verify attributable lifecycle and
  currentness against the independently prepared source facts.
- S03: search_project -> get_provenance -> get_source_detail; follow returned
  identity rather than guess it. Check source revision, bounded content and
  DERIVED_READ_ONLY/non-authoritative labels.
- S04: search_knowledge across explicit project_ids; personal_search is scoped and
  remains non-authoritative; wrong-project results and ambiguous selection reject.
- S05: get_operations_readiness with legitimate durable metadata; memory
  candidates/effectiveness and optional intelligence return truthful UNKNOWN,
  NO_DATA or INSUFFICIENT rather than fabricated PASS. Optional extensions do not
  acquire production/dispatch authority from dashboard availability.
- S06: missing/wrong credentials and scope are denied; malformed workstream,
  missing scope and provenance mismatch fail safely without leakage. Correlate
  actual successful/blocked tool calls with privacy-minimal server event records.
- S07: real-browser auxiliary regression: overview -> project -> source/search
  -> lifecycle; engineering/personal labels, same source revision/content as MCP,
  ADR/intelligence and every remaining exposed dashboard route. Preserve prior
  browser capability inventory; new routes need explicit disposition.
- S08: browser empty registry/search, unknown project/route, invalid/oversized
  search, invalid/unavailable projection or lifecycle, inert source markup,
  security headers and rejected POST/write attempt; restore run-owned fixtures.

## Evidence and pass rule

Retain per-scenario actual actions/observations/results and raw bounded MCP
responses, browser evidence, exact identity and cleanup. A client transport driver
is permitted; prewritten scripted judgments, unit/CI PASS, HTTP counts, screenshots
without interaction, or a caller-authored gate JSON do not establish user PASS.
Keep every mandatory FAIL/PARTIAL/BLOCKED explicit. Both user gates remain
mandatory, require 100% applicable coverage, zero blocking findings and the SAME
exact HEAD. Findings are accumulated before a bounded fix batch; after a relevant
change run a fresh complete confirmation. A browser safety failure is not waived
because MCP is primary. No production change or release authority comes from
structural validation of this evidence.
