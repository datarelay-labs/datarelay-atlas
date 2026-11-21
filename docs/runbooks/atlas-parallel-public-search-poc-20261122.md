# Atlas Parallel MCP public search — executable roadmap result, 2026-11-22

Scope: ATLAS-TOOL-1/2/4 from Atlas Issue #10. The integration decision is
NOT_ADOPTED_WITH_EVIDENCE for production. Only isolated public-search evaluation
is implemented; no Atlas runtime, auth, credential, persistent state or
canonical-engineering-authority changes.

## Design and security

Goal: measure fixed 30-question official-source retrieval against existing
public search and decide whether an external provider merits adoption.
Non-goals: private repo/customer logs, internal Work Packets, indexed content,
arbitrary browsing, output resale, automatic source writes, autonomous model
choice, deployment/permission/release authority, or fabricated quality scores.

The optional dev-only CLI calls exactly the public Parallel MCP web_search tool
at https://mcp.parallel.ai/mcp?tools=web_search. It transmits only approved
manifest public questions, no credentials or private content, with fixed
query scope, 0.35s pacing, bounded timeouts, one remote MCP session and
three-error stop. Output has only public candidate URLs and digests and timing,
never web source bodies/excerpts, cookies, raw errors, user task data or tokens.
The default command performs zero network effects:

    python3 scripts/atlas_parallel_public_poc.py

Explicit public-only lab call (never run from production service):

    python3 scripts/atlas_parallel_public_poc.py --execute-public-poc --limit 30

All responses are untrusted evidence. Exact official hosts are allowlisted;
malformed/foreign URLs do not become official source citations. No quote or
page output is stored in Atlas or GitHub. The tool does not attest whether a
page supports a factual claim.

## Actual 30-question native Parallel MCP results

Two separately executed 30-question public runs on dev-atlas, same fixed
question bodies, as-of 2026-11-21 UTC:

| Observed metric | Before host correction | After host correction |
| --- | ---: | ---: |
| Successful Parallel MCP calls | 30/30 | 30/30 |
| Allowlisted official-host result | 28/30 | 30/30 |
| First result from official host | 25/30 | 28/30 |
| Median observed call latency | 1,016 ms | 1,054 ms |
| p95 observed call latency | 1,378 ms | 1,893 ms |
| Verified per-request provider cost | UNKNOWN | UNKNOWN |
| Independent source factual review | NOT_PERFORMED | NOT_PERFORMED |
| Identical native baseline 30 responses | NOT_COLLECTED | NOT_COLLECTED |

The 28-to-30 difference is **not** proof of model improvement. The fixed
official-host manifest had initially omitted canonical www.cisa.gov and
www.rfc-editor.org. Adding those exact approved hosts corrected the
classification. Official-host hits are not factual agreement scores.

Read-only metadata evidence under the dev-atlas owner home, NOT committed:
- atlas-research-poc/parallel-30-20261122.json
  SHA256 b6732469409144c010028dd88027395ca71853d92f7552f9bfe5f4ca0efd4a6d
- atlas-research-poc/parallel-30-revised-20261122.json
  SHA256 0c612852d88c95cd587963ad70810d3c2337b75de5bc04fd54ca97124e7df005

For actual official-document crosschecking, separate public MITRE documents
were retrieved, hashed and evaluated via the project research skill.
Result: one DECLARED_CORROBORATION, 29 NO_SOURCES;
independent_content_attestation=NOT_ATTESTED; no canonical memory write.

## Terms and privacy checkpoint / conditional integration

Parallel MCP docs https://parallel.ai/mcp advertise keyless free search with
lower limits. Customer Terms https://parallel.ai/customer-terms as published
effective 2026-11-10 include conditions on reuse, caching and sharing
outputs across end customers and restrictions around commercial integration.
Privacy https://parallel.ai/privacy-policy effective 2026-08-11 does not
independently establish zero request retention or permission to send DataRelay
private source content. Commercial intended-use, DPA, model-training,
retention, source output reuse and caching must be independently approved
before ANY production/tenant/customer data are shared.

Disposition: production provider and permanent indexing
NOT_ADOPTED_WITH_EVIDENCE. The isolated public-only PoC is retained.
No credential, OAuth, external provider production runtime, auto-write,
data egress of internal materials, or model routing was activated.

## Acceptance truth and remaining evidence

ATLAS-TOOL-1: isolated public search provider executed on 30/30 actual
questions, but paired native baseline accuracy/time/cost not collected.
Full two-provider A/B remains INCOMPLETE; do not invent baseline timings or
zero cost when a provider does not expose metering.

ATLAS-TOOL-2: conditional production integration not adopted based on
unresolved licensing/privacy/caching boundaries.

ATLAS-TOOL-3: official-source crosscheck skill implemented in merged PR #364,
with native read-only partial live proof and security tests.

ATLAS-TOOL-4: real keyless remote MCP search, native client handshake, CLI
roundtrip and security unit tests observed. Exact-head GitHub CI, whole
product Surface Reconciliation and Full User E2E are independent gates;
a search PoC is not a substitute for a product release user gate.

No experiment grants engineering PASS, GitHub source authority, permission
change, code deployment or production service rollout.
