# Source, Provider, and Provenance Contracts

Status: Accepted via ADR-0003
Scope: Phase 0 foundation contracts that unblock Phase 1
Runtime/language: intentionally unspecified

These contracts are implementation-neutral. They define required fields and
semantics, not storage engines, frameworks, or Athena/Wiki.js APIs.

Machine-checkable schema:
[`source-provider-provenance.schema.json`](source-provider-provenance.schema.json)

Example fixture:
[`fixtures/source-provider-provenance.example.json`](fixtures/source-provider-provenance.example.json)

## 1. Project identity / namespace

| Field | Required | Semantics |
|---|---|---|
| `project_id` | yes | Stable Atlas project identifier (slug). Used as the authorization and retrieval namespace. |
| `display_name` | no | Human label; not an identity key. |
| `namespace` | yes | Logical scope key; initially equal to `project_id` for single-org deployments. |

Rules:

- Retrieval defaults to one `project_id` unless a caller explicitly expands scope.
- Cross-project retrieval must name every included `project_id`.
- `project_id` is Atlas-owned identity. It is not a Wiki.js path and not a GitHub org name by itself.

## 2. Provider identity and authentication boundary

| Field | Required | Semantics |
|---|---|---|
| `provider` | yes | Provider id. First provider: `github`. |
| `provider_api_base` | no | Override for GHES/compat endpoints. |
| `auth_mode` | yes | How runtime obtains credentials: `github_app`, `installation_token`, `personal_token`, or future modes. |

Rules:

- Credentials/tokens/secrets MUST NOT appear in source configuration committed to Git, in provenance payloads, or in retrieval responses.
- Authentication proves read access to canonical objects; it does not create provenance by itself.
- Lack of credentials yields sync state `error` or `unknown`, never invented content.

## 3. Canonical source configuration

A source configuration selects what canonical content Atlas may project.

| Field | Required | Semantics |
|---|---|---|
| `source_id` | yes | Stable id within a project. |
| `project_id` | yes | Owning project. |
| `provider` | yes | Provider id (`github` first). |
| `repository` | yes | `owner/name` for GitHub. |
| `ref` | yes | Branch, tag, or commitish used as the sync selector. |
| `source_path` | yes | Path inside the repository. No leading `/`; no `..` segments. |
| `media_type` | no | Hint such as `text/markdown`. |
| `enabled` | yes | Whether sync should attempt this source. |

Non-public integration mapping (optional, private to an integration):

| Field | Allowed? | Semantics |
|---|---|---|
| `integration_projection_key` | optional private | Engine-specific projection locator (for example a Wiki path). NEVER the Atlas public provenance primary key. |

## 4. Immutable source revision / blob identity

When a sync successfully reads canonical bytes, it records:

| Field | Required on success | Semantics |
|---|---|---|
| `source_revision` | yes | Immutable object identity. For GitHub file contents API this is the Git blob SHA. |
| `resolved_commit` | recommended | Commit SHA when resolvable for the selected ref. |
| `fetched_at` | yes | Timestamp of the successful fetch. |
| `content_digest` | recommended | Hash of normalized payload bytes Atlas actually projected. |

Rules:

- Prefer provider-native immutable ids (`source_revision`) over mutable refs.
- A change to either bytes or revision identity requires rebuild of derived projections for that source.

## 5. Projection / rebuild identity

| Field | Required | Semantics |
|---|---|---|
| `projection_id` | yes | Derived identity for a projected artifact. |
| `projector` | yes | Integration/projector name + version/parameters digest. |
| `source_id` | yes | Configured source that produced it. |
| `source_revision` | yes on success | Revision projected. |
| `rebuild_key` | yes | Deterministic key: `project_id + source_id + source_revision + projector`. |

Rules:

- Projections are disposable and rebuildable.
- Replacing a projection with the same `rebuild_key` is an idempotent refresh.
- Projection identity must not be a Wiki.js page id in public contracts.

## 6. Provenance attached to derived records and retrieval results

Every derived record and every retrieval hit MUST include:

| Field | Required | Semantics |
|---|---|---|
| `project_id` | yes | Owning project namespace. |
| `provider` | yes | Canonical provider. |
| `repository` | yes | Canonical repository. |
| `ref` | yes | Configured selector ref. |
| `source_path` | yes | Canonical path. |
| `source_revision` | yes when known | Immutable revision/blob id. |
| `canonical` | yes | Always `false` for derived/retrieval payloads. |
| `provenance_complete` | yes | `true` only when required fields above are present and sync state was successful. |

Optional but recommended: `resolved_commit`, `source_id`, `projection_id`, `fetched_at`.

## 7. Canonical-vs-derived semantics

- Canonical = GitHub/repository artifacts selected by source configuration.
- Derived = any Atlas projection, index chunk, embedding, summary, or synthesis.
- If canonical and derived disagree, canonical wins.
- Synthesis/derived intelligence must be labeled derived and attributable; it must not be presented as canonical.

## 8. Deterministic re-sync / rebuild expectations

For the same:

- source configuration
- authenticated readable canonical object
- `source_revision`
- projector version/parameters

Atlas MUST produce an equivalent projection payload and provenance set (byte-stable where the projector claims determinism).

Changing projector parameters creates a new `rebuild_key` and does not rewrite history of prior projections unless an explicit migration says so.

## 9. Failure / unknown semantics

| Sync state | Meaning | User/AI visible rule |
|---|---|---|
| `success` | Canonical object fetched and projection rebuilt | Derived content may be served with complete provenance |
| `unchanged` | Remote revision matches last successful projection | Prior derived content remains current |
| `error` | Fetch/auth/decode/projection failed | Do not invent content; surface failure |
| `unknown` | State not yet observed or evidence missing | Do not treat as authoritative current knowledge |
| `disabled` | Source not enabled | Ignored by sync |

Forbidden:

- fabricating canonical bodies when fetch fails
- omitting provenance while claiming current canonical knowledge
- elevating Wiki/Athena identities above GitHub provenance

## 10. Mapping from Athena PoC fields

| PoC field | Atlas contract |
|---|---|
| `project` | `project_id` / `namespace` |
| `repository` | `repository` |
| `ref` | `ref` |
| `source_path` | `source_path` |
| Git blob SHA | `source_revision` |
| `wiki_path` | private `integration_projection_key` only; not public provenance PK |
| Wiki page id | not an Atlas public identity |
