# Atlas personal-source canonical-link correction (2026-10-19)

Status: **isolated Draft source fix**, not merged, not on production, no release authority.

## Direct ChatGPT-run actual Chromium finding

The ChatGPT acting persona launched Playwright Chromium **156.0.8078.4** on
`dev-atlas`, navigated the real browser at loopback to a test-only 7-project
Atlas Web UI running the **exact immutable source** `6118cd26b991008df39f7dc5b65aaea6c77fbf37`.

On `/projects/qa-atlas/sources/personal-qa-note`, a **personal**, local Markdown
projection correctly said `personal reference / non-authoritative` but also
rendered a fabricated **`canonical source ↗`** link pointing at
`https://github.com/local/markdown/blob/<synthetic revision>/qa-browser-note.md`.
The page also displayed the generic claim `canonical source remains
authoritative`, inappropriate for a local personal/reference snapshot.

This was confirmed again after fixing the *test-only fixture* content, so it
is a real UI classification defect, not a source-content seeding problem.

## Bounded code correction

- `_canonical_source_link` refuses the link for personal source class,
  local-markdown provider or `local/markdown` repository; existing GitHub
  engineering links remain unchanged.
- Personal projection view now says that it is a **read-only personal reference
  snapshot, not canonical engineering truth**.
- Existing personal dashboard and source-detail regression uses the actual
  registered local Markdown source and asserts no fake link/authority text.
  The affected Web UI, provenance and personal knowledge tests passed **36/36**
  on the isolated branch before committing.

## E2E and rollout boundaries

Other real browser flows observed on the frozen app: overview (7 projects),
project/source/provenance, local personal reference, project and cross-project
search with engineering-vs-personal filtering, lifecycle UNKNOWN, nine auxiliary
dashboard routes, unknown-project/unknown-path 404, oversized query 400,
form POST 405, CSP/no-store/nosniff headers, state unchanged after denied POST,
and a fresh browser context reading the same validated source.

A real Chromium process and its actual browser actions are evidence of executed
**supporting UI behavior**, not proof that the production MCP user E2E passed.
The test root contained only synthetic sources and no production credentials.
This correction is **not** in the owner-prepared `6118cd2` immutable r2 stage.
Keep that stage untouched; any release selecting this correction must freeze and
independently qualify a new app HEAD, manifest and operator package, or schedule
a separate future Web update. Do not silently mutate production or mark release
PASS.
