# Atlas Core standards conformance review — 2026-10-18

Status: **focused source-level audit, not a product-wide certification**.
Scope: Atlas Core MCP transport, token verification, protected-resource discovery, source provenance and evidence contracts.
Review base: GitHub `origin/main` at `9177c976a591143b045ebdeabdbccd0d48b87c6c`.
All code changes in this review remain in an independent development worktree. This note grants no production, OAuth, release or canonical write authority.

## Decision and minimal design gate

- **Goal:** remove independently reproducible discrepancies between the existing accepted Atlas contracts and published MCP/OAuth specifications without altering deployed credentials, audiences, permissions or operational state.
- **Non-goals:** migrating the OAuth provider, replacing Atlas provenance, creating a universal W3C PROV store, adopting general-purpose agent memory, implementing new MCP task extensions, rewriting the release evidence system or deploying to production.
- **Public behavior:** the existing read/write tool inventory and scopes remain unchanged. Only explicitly invalid introspection-claim shapes that previously passed are denied. Protected-resource metadata gains the optional human-facing `resource_name`.
- **State and rollback:** no database migration, no durable-state mutation; rollback is reverting the isolated code commit after normal exact-HEAD safety review.
- **Acceptance:** deterministic negative/positive introspection tests; verified OAuth metadata; real local HTTPS MCP 2026-07-28 and legacy clients on one server; affected full tests and PR CI.
- **Boundary:** approval of a source-only PR does **not** enable production writes or supersede Issue #346's rollout and separate authorization controls.

## Normative comparison

| Standard | Requirement / target | Atlas observation | Disposition |
| --- | --- | --- | --- |
| [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) | Modern stateless client and compatibility with earlier MCP | Official `mcp==2.2.0` SDK and `/mcp`; both modern and legacy real HTTPS client paths tested | **Confirmed for tool listing; not a full wire conformance certification** |
| [RFC 7662](https://www.rfc-editor.org/rfc/rfc7662) | Strict typed introspection attributes; presence and absence distinguished | Prior code accepted present JSON `null` for `iss`, `aud`, `resource`, `exp`, `nbf` and `scope` in some otherwise valid tokens | **Fixed** via explicit missing sentinel, strict typing and regression cases |
| [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) / [RFC 9207](https://www.rfc-editor.org/rfc/rfc9207) | Exact issuer identifier comparison; no URI-equivalence assumptions | Prior token verifier stripped leading whitespace/trailing slashes from a present issuer | **Fixed** exact comparison with positive and malformed-issuer tests |
| [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519) | Expiration is exclusive (now < exp); not-before is inclusive (now >= nbf) | Prior verifier accepted tokens for the exact expiration second | **Fixed** exclusive expiry boundary and tested nbf boundary |
| [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) | Bind access token to intended resource | Existing verifier checks `aud` and `resource` against the MCP URL; malformed and contradictory claims rejected | **Existing control retained**, not independently certified end to end against Keycloak |
| [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) | Protected-resource metadata includes required `resource`; `resource_name` recommended | Metadata already provided `resource`, authorization server and supported scopes; `resource_name` absent | **Improved** with `resource_name` and test |
| [JSON Schema 2020-12](https://json-schema.org/draft/2020-12) | Machine-checkable source/derived/retrieval contracts | Atlas owns `source-provider-provenance.schema.json` with draft 2020-12 and a fixture validation gate | **Existing coverage**; runtime data-entry validation not exhaustively assessed here |
| [W3C PROV](https://www.w3.org/TR/prov-o/) | Interoperable provenance representation and relationships | Atlas retains repository, ref, path, revision and `canonical=false` in its own contracts; no W3C PROV-O/PROV-JSON serialization claim | **Semantic overlap only**; external interchange format is optional, not a demonstrated gap in Atlas Core requirements |
| [SLSA](https://slsa.dev/spec/v1.2/) and [in-toto](https://in-toto.io/) | Verifiable software supply-chain attestations | Exact-HEAD CI/quality evidence exists within Engineering System authority | **Separate release-governance assessment**; do not invent attestations or silently replace Engineering System |
| [W3C Trace Context](https://www.w3.org/TR/trace-context/) / [OpenTelemetry](https://opentelemetry.io/docs/specs/otel/) | Cross-service tracing context and evidence | Tool-name/outcome-only Atlas audit is implemented; end-to-end trace propagation not qualified in this review | **Optional observability work**, subject to secret/privacy minimization |
| [OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/) | Untrusted retrieved content must not become privileged instructions | Atlas explicitly keeps derived/provenance context non-authoritative; no comprehensive prompt-injection red-team certification | **Maintain trust boundary**; dedicated untrusted-content E2E belongs to later security assessment |

## Verified regression intent

1. Accept canonical issuer and tokens with truly **omitted** optional claims where previous production behavior allowed them.
2. Reject **present** JSON null for identity, audience, scopes and temporal validity even when the remaining claims are valid.
3. Reject issuer spellings differing by slash, whitespace or host-case variation, and reject exp == now while preserving nbf == now.
4. Keep `atlas.read`, `atlas.write`, `atlas.sources.write` and feature flags unchanged; source mapping still edits no GitHub content.
5. Exercise actual HTTPS tool discovery with `mode="2026-07-28"` and `mode="legacy"` on the same SDK server.
6. Verify RFC 9728 protected-resource metadata, including `resource_name`.

## Follow-up RFC 7662 subject hardening

A second source audit found that a present introspection `sub` value of JSON null, number, array, object, or blank string was silently treated as an absent subject. RFC 7662 defines `sub` as a string when present. The verifier now rejects those malformed values while retaining the previous behavior for a truly omitted optional `sub`. The dedicated positive/negative test covers all these cases. This does not claim production Keycloak E2E.

## Provenance contract identity validation

JSON Schema 2020-12 validates each object independently but does not enforce cross-object equality. The contract fixture validator now checks project, source, repository, ref, path and revision consistency between canonical source, sync result, derived record and retrieval hit. Negative mutations are checked for both GitHub and personal fixtures. This is a fixture contract gate, not a claim that every runtime ingestion path has been audited.

## Runtime observability provenance hardening

The runtime observability document count previously used ProjectionStore.list_documents(), which reads successful projection bytes without validating their digest and provenance. The count now uses the existing iter_validated_projections reader for each registered project. A tampered derived document is rejected instead of contributing to an apparently valid count. No raw document body is included in the observability response. The focused runtime observability and retrieval regressions pass.

## Unverified / not promoted to PASS

Production Keycloak/ChatGPT OAuth reconsent, live write-tool enablement, an all-message MCP spec test suite, browser user-acceptance gates, complete OWASP LLM threat testing, SLSA/in-toto provenance, universal W3C PROV interchange, and production deployment. No assertion here changes the status of Issue #346 or its already denied privileged cutover policy.
