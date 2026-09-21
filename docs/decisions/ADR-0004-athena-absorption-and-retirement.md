# ADR-0004: Athena Absorption and Repository Retirement

Status: Accepted
Date: 2026-09-21
Supersedes: ADR-0002 (long-term fork / external-knowledge-engine strategy only)
Does not rewrite: ADR-0001, ADR-0002 historical record, ADR-0003 contracts

## Minimal design gate

1. **Goal** — Absorb every Atlas-required capability stranded in `datarelay-labs/athena` into `datarelay-atlas`, eliminate Atlas build/test/deploy/runtime dependency on the Athena repository, and make Athena safe to archive/delete after verified independence.
2. **Non-goals** — Merging `engineering-system` into Atlas; deleting Athena during this change; production deployment/server provisioning; preserving Wiki.js or Bun merely for PoC parity; multi-tenant SaaS / generic RAG / Git hosting / CI replacement.
3. **Affected public contract** — Architecture/docs that previously described Athena as a maintained external knowledge-engine dependency; integration metadata; Atlas retrieval/MCP context surfaces; third-party attribution for any copied/derived Athena-origin code.
4. **State / migration impact** — No Atlas production persistent store yet. Migration preserves pinned Athena refs as historical evidence. Future durable storage must still conform to ADR-0003 and will need its own persistence ADR.
5. **Security / operations impact** — Removes a second product repository from the runtime trust/deploy path. Provider credentials remain outside Git. Athena deletion remains an irreversible owner-approved final action after independence gates PASS.
6. **Architecture boundary** — `datarelay-labs/engineering-system` stays independent and canonical. `datarelay-labs/datarelay-atlas` owns all Atlas product behavior. `datarelay-labs/athena` becomes temporary migration input only.
7. **Acceptance / regression criteria** — Superseding docs/ADR consistency; durable Athena inventory/disposition; Atlas-owned code/tests for required PoC capabilities or explicit DROP rationale; zero Athena-repo dependency gate; license/provenance records; Athena PR #1 dispositioned; retirement checklist ready for owner archive/delete.

## Context

ADR-0002 accepted a long-term strategy in which `datarelay-labs/athena` remained an upstream-tracking fork and Atlas pinned Athena revisions as a replaceable external knowledge-engine integration. That strategy reduced early risk but left Atlas product capabilities stranded outside the product repository.

Owner decision for this workstream supersedes that long-term fork posture: Athena is migration input, not a future Atlas runtime/release dependency.

## Decision

1. **Engineering System remains independent.** `datarelay-labs/engineering-system` is not absorbed into Atlas and remains the canonical methodology repository for Data Relay Labs projects.
2. **Atlas owns all product behavior.** Runtime/code/contracts/tests required by DataRelay Atlas live in `datarelay-labs/datarelay-atlas`.
3. **Athena repository is migration input only.** Atlas must not require `datarelay-labs/athena` (or `jannismilz/athena`) at build, test, deploy, upgrade, restore, or runtime. No Git submodule/subtree/clone/fetch of Athena is permitted in Atlas product paths.
4. **Pinned Athena revisions remain historical evidence.** Preserve:
   - upstream snapshot `6720b948744d42f1332f86f9a8157ff588e40d6e`
   - PoC snapshot `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`
   - generic-hardening evidence `5b90969a7e0fecadae587144a0bd7f444352799f` / `datarelay-labs/athena#1` (closed unmerged as superseded)
5. **Disposition vocabulary for Athena capabilities/files:**
   - `ATLAS-NATIVE` — required Atlas behavior reimplemented/owned in Atlas
   - `THIRD-PARTY-EMBEDDED` — Athena-origin/open-source code copied or derived into Atlas with license/provenance
   - `EXTERNAL-DEPENDENCY` — general package/image dependency not requiring the Athena repository
   - `DROP` — Athena-specific behavior not required by Atlas
   - `HISTORICAL-EVIDENCE` — retained only as provenance/migration evidence
6. **Do not mechanically copy the Athena tree.** Prefer Atlas-native reimplementation of required behavior. If any Athena-origin source is copied/derived, record Apache-2.0 (or other applicable) obligations in `THIRD_PARTY.md` and keep them distinct from Atlas-owned licensing.
7. **Wiki.js is not an Atlas public product concept.** Human browsing may later use an Atlas UI or an EXTERNAL-DEPENDENCY chosen on Atlas terms. PoC Wiki path identity remains non-public.
8. **Runtime/framework choice is Atlas-owned.** First Atlas-owned absorption implementation uses Python stdlib + existing repository Python test tooling because Atlas already validates contracts in Python. This is not an endorsement of Athena's Bun/TypeScript/Wiki.js stack and does not freeze Python as the only forever runtime.
9. **Required PoC capabilities preserved under Atlas ownership:**
   - authenticated canonical GitHub source ingestion
   - project/repository namespace
   - repo/ref/path/immutable-revision provenance
   - deterministic projection/rebuild
   - project-scoped exact/keyword retrieval and hybrid fusion with a pluggable semantic provider
   - MCP-facing retrieval/context with provenance
   - derived knowledge clearly marked non-canonical
   - security regressions for path validation and project/scope gating
10. **Athena retirement criteria:** independence gate PASS; inventory complete; required tests PASS; docs no longer describe Athena as a maintained product dependency; open Athena PR/issues dispositioned; retirement checklist complete; owner explicitly approves archive/delete. Repository deletion is a separate irreversible owner action.

## Reimplemented vs dropped (summary)

| Area | Disposition |
|---|---|
| GitHub sync + provenance projection | `ATLAS-NATIVE` |
| Project namespace + retrieval scope | `ATLAS-NATIVE` |
| Keyword retrieval + RRF hybrid fusion | `ATLAS-NATIVE` (reimplemented; PoC behavior as evidence) |
| Semantic embeddings / pgvector / TEI | `EXTERNAL-DEPENDENCY` interface now; provider wiring later |
| MCP retrieval/context tool surface | `ATLAS-NATIVE` library contract; transport/auth server later as Atlas-owned or EXTERNAL |
| Wiki.js coupling / wiki write tools as product identity | `DROP` from Atlas product |
| Athena dashboard/metrics UI | `DROP` for now (Atlas UI is a later phase) |
| Athena Docker/Compose packaging | `DROP` as Athena-repo packaging; Atlas packaging later |
| Generic Athena MCP/search hardening PR #1 | `HISTORICAL-EVIDENCE` / superseded; useful ideas absorbed into Atlas-native security/retrieval tests |
| Apache-2.0 Athena source tree | Not vendored; `HISTORICAL-EVIDENCE` via pinned refs |

## Consequences

- ADR-0002 remains historical context for why Athena was forked and pinned; its long-term external-integration strategy is no longer the target state.
- ADR-0001's product-boundary intent remains valid; Athena is no longer described as a required/preferred runtime integration.
- ADR-0003 contracts remain authoritative and implementation-neutral.
- Atlas documentation and integration metadata must describe Athena as migration evidence, not a maintained dependency.
- Athena archive/delete waits for the retirement checklist and explicit owner approval.
