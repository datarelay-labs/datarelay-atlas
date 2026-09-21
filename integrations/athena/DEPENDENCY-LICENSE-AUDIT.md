# Athena Dependency / License Compatibility Audit

Status: evidence recorded for Phase 0 foundation closure
Audited source: `datarelay-labs/athena@atlas-poc-20260921`
Audited commit: `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`
Pinned upstream baseline: `6720b948744d42f1332f86f9a8157ff588e40d6e`
Recorded at: 2026-09-21

This document records **factual license metadata** gathered from declared upstream
sources. It is **not** legal advice and does not assert a final distribution model
for DataRelay Atlas.

## Scope

| Layer | Included | Evidence |
|---|---|---|
| Athena top-level license | Yes | Athena `LICENSE` at PoC HEAD → Apache-2.0; no `NOTICE` file |
| Athena npm/Bun lockfile packages | Yes | `bun.lock` at PoC HEAD → [`evidence/athena-poc-npm-licenses.tsv`](evidence/athena-poc-npm-licenses.tsv) |
| Athena workspace direct deps | Yes | `package.json` + `packages/*/package.json` |
| Runtime container images used by Athena PoC compose/Dockerfile | Yes | declared image tags + upstream GitHub license metadata |
| Atlas-owned code | Out of scope for this Athena audit | future Atlas license remains separate |

## Method

1. Parse PoC `bun.lock` package identities and pinned versions.
2. Query `npm view <package>@<version> license` for each locked package.
3. Record declared SPDX/license strings without inventing missing values.
4. Inspect Athena compose/Dockerfile image references and look up declared upstream licenses.
5. Classify inclusion as direct-runtime, transitive-runtime-candidate, direct/transitive-dev, override-pinned, or runtime-image.
6. Mark ambiguous/missing/copyleft-adjacent findings as **review-required** rather than guessing compatibility for a future Atlas release package.

## Athena top-level

| Field | Value |
|---|---|
| Component | Athena |
| Upstream | `jannismilz/athena` |
| Fork | `datarelay-labs/athena` |
| Declared license | Apache-2.0 |
| Evidence | PoC `LICENSE` header + GitHub license metadata |
| NOTICE | not present at audited revision |
| Attribution obligation | Apache-2.0 NOTICE/attribution conditions apply if Atlas redistributes Athena source or binaries |

## npm / Bun lockfile inventory

- Locked packages audited: **116**
- Lookup failures: **0**
- Declared license histogram:

| Declared license | Count |
|---|---|
| MIT | 95 |
| MIT OR Apache-2.0 | 9 |
| ISC | 7 |
| BSD-3-Clause | 2 |
| BSD-2-Clause | 1 |
| Apache-2.0 | 1 |
| Unlicense | 1 |

### Direct runtime dependencies

| Package | Pinned version | Declared license | Inclusion |
|---|---|---|---|
| `postgres` | 3.4.9 | Unlicense | direct-runtime |
| `zod` | 3.25.76 | MIT | direct-runtime |
| `express` | 5.2.1 | MIT | direct-runtime |
| `@modelcontextprotocol/sdk` | 1.30.0 | MIT | direct-runtime |

### Security overrides present in PoC

| Package | Pinned version | Declared license | Notes |
|---|---|---|---|
| `fast-uri` | 3.1.6 | BSD-3-Clause | override-pinned transitive |
| `hono` | 4.13.8 | MIT | override-pinned transitive |
| `qs` | 6.16.0 | BSD-3-Clause | override-pinned transitive |

### Lockfile findings

- No GPL/AGPL/LGPL/SSPL package licenses were declared in the audited npm lockfile set.
- All audited npm package licenses resolved to permissive or public-domain-style declarations above.
- Full machine-readable rows: [`evidence/athena-poc-npm-licenses.tsv`](evidence/athena-poc-npm-licenses.tsv).

## Runtime images / non-npm components

These are Athena PoC runtime dependencies, not Atlas product packages. They matter
if Atlas later ships or redistributes an Athena-based deployment.

| Component | Declared identity | Declared license evidence | Inclusion | Disposition |
|---|---|---|---|---|
| Wiki.js | `ghcr.io/requarks/wiki:2` / `requarks/wiki` | GitHub SPDX **AGPL-3.0** | runtime-image / direct runtime dependency of Athena | **review-required** before any Atlas distribution that includes Wiki.js |
| Bun runtime | `oven/bun:1.3-alpine` | Bun itself MIT; statically linked JavaScriptCore/WebKit **LGPL-2**; additional linked libs with mixed licenses per Bun `LICENSE.md` | runtime toolchain / image | **review-required** for redistribution of Bun binaries; OK as build/runtime dependency with attribution/relink obligations reviewed |
| PostgreSQL + pgvector | `pgvector/pgvector:pg18` | PostgreSQL-style license text in `pgvector` `LICENSE` | runtime-image | review attribution text on packaging; no AGPL signal found |
| Text Embeddings Inference | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` | GitHub SPDX **Apache-2.0** | optional runtime-image | permissive declared license |
| Alpine base | `alpine:3.22` / Bun Alpine image base | Alpine historically MIT; confirm package set at release packaging time | runtime-image base | packaging-time package audit still required |

## Incompatible / unknown / review-required summary

| Finding | Severity for Phase 0 | Why |
|---|---|---|
| Wiki.js AGPL-3.0 | **review-required** | Strongest copyleft signal in the Athena PoC stack. Blocks silent assumption that Athena+Wiki.js can be redistributed under Atlas-only terms. |
| Bun LGPL-linked components | review-required | Relevant if Atlas redistributes Bun binaries; less relevant if Bun remains an external runtime. |
| Final Atlas distribution/package boundary | pending by design | Phase 0 does not invent a distribution model. Record obligations; choose packaging later. |
| Athena `NOTICE` absent | informational | Apache-2.0 NOTICE handling still required if NOTICE content appears in dependencies/redistribution set. |

## Distinction: facts vs interpretation

**Facts recorded here**

- Declared license strings and source URLs/paths
- Pinned versions/revisions
- Whether a component appears direct, transitive, dev-only, override-pinned, or runtime-image

**Not decided here**

- Whether AGPL Wiki.js is acceptable for a future Atlas release image
- Whether Atlas will vendor, containerize, or call Athena remotely
- Whether Bun must be redistributed by Atlas
- Final SBOM/release legal sign-off

## Reproducibility

```text
source: datarelay-labs/athena@f38e20ec4d4ea22c71f1457d9a5361da1e92773a
lockfile: bun.lock
npm license probe: npm view <pkg>@<ver> license --json
container refs: Dockerfile + docker-compose.yml at same commit
```

## Phase 0 conclusion

- Athena top-level Apache-2.0 review remains valid.
- Transitive npm/Bun lockfile audit is **complete for the pinned PoC revision** with no declared GPL-family npm licenses.
- Runtime stack audit surfaces **Wiki.js AGPL-3.0** and **Bun LGPL-linked components** as explicit review-required items before any redistribution decision.
- `THIRD_PARTY.md` status is updated from “transitive pending” to this evidence-backed state.
