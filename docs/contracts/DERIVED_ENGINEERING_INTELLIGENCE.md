# Derived Engineering Intelligence v1

This contract defines deterministic, rebuildable, non-authoritative intelligence over Atlas validated projections.

## Output

The payload is always marked:

- `derived: true`
- `canonical: false`

Every derived item carries its kind/value, source project ID, projection identity, and validated projection provenance including repository, source path, revision, and source class.

The runtime also exposes deterministic indexes for concepts, repositories, source paths, ADR targets/backlinks, unanswered questions, configured-source knowledge gaps, and explicit contradiction evidence.
## v1 item kinds

- `concept_heading`: emitted only from an explicit Markdown heading in the projection body; it is a navigation anchor, not semantic entity inference.
- `cross_project_link`: emitted only when the projection body explicitly names another engineering repository present in the same validated input set.
- `decision_backlink`: emitted only from an explicit `ADR-NNNN` reference in the projection body.
- `unanswered_question`: emitted only from explicit `QUESTION:` or `TODO:` markers. Ordinary prose ending in `?` is not promoted.
- `contradiction_evidence`: emitted only from an explicit `CONTRADICTION:` marker or an unresolved Git merge-conflict block in a validated projection.

Projection provenance headers are excluded from body analysis so Atlas does not derive links, concepts, or contradiction evidence from its own metadata banner.
## Entity and navigation indexes

- repository entities come only from engineering-authority projection provenance;
- source-path entities retain project/source identity and source class;
- decision targets are resolved only when a validated engineering projection source path itself names an `ADR-NNNN` document;
- decision backlinks and targets are separate so a reference never fabricates a decision document;
- concept indexes group explicit headings without claiming semantic equivalence;
- project intelligence is project-scoped, while the overview uses an explicit set of enabled registered projects.

The Human UI exposes project and cross-project intelligence dashboards. CLI exposes `atlas intelligence show <project>` and `atlas intelligence overview`. Authenticated MCP may expose project-scoped intelligence as a read-only tool.
## Knowledge gaps

Project-scoped knowledge-gap detection compares enabled configured sources with current projection records.

A source is a gap when it has no current `success`, `unchanged`, or `ok` projection. This is a source-coverage gap only; it does not assert that the repository lacks knowledge on a topic.

## Contradiction semantics

Deterministic v1 does not attempt semantic contradiction inference.

- explicit contradiction evidence present → `DETECTED`;
- no explicit markers found → `NONE_OBSERVED`;
- semantic contradiction state always remains `UNKNOWN`.

`NONE_OBSERVED` is not proof of consistency.
## Non-goals / truth boundary

- No LLM or semantic contradiction inference.
- No inferred repository links to repositories absent from the validated input set.
- Personal/reference projections cannot create the authoritative engineering-repository target set.
- No GitHub or canonical writes.
- Tampered/malformed projection bytes fail closed through the same digest/provenance validation used by retrieval.
- Output is capped and deterministic; rebuild order does not change the result.
- Derived intelligence never overrides GitHub/OpenSpec/code/tests/ADR/CI authority.
