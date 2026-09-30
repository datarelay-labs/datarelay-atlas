# Derived Engineering Intelligence v1

This contract defines deterministic, rebuildable, non-authoritative intelligence over Atlas validated projections.

## Output

The payload is always marked:

- `derived: true`
- `canonical: false`

Every item carries:

- `kind`
- `value`
- source project ID
- projection identity
- validated projection provenance including repository/path/revision and source classification

## v1 item kinds

- `concept_heading`: emitted only from an explicit Markdown heading in the projection body; it is a navigation anchor, not semantic entity inference.
- `cross_project_link`: emitted only when the projection body explicitly names another engineering repository that is present in the same validated input set.
- `decision_backlink`: emitted only from an explicit `ADR-NNNN` reference in the projection body.
- `unanswered_question`: emitted only from explicit `QUESTION:` or `TODO:` markers. Ordinary prose ending in `?` is not promoted.

Projection provenance headers are excluded from analysis so Atlas does not derive links from its own metadata banner.

## Non-goals / truth boundary

- No LLM or semantic inference.
- No contradiction inference in v1; contradiction state is explicitly UNKNOWN.
- No inferred repository links to repositories absent from the validated input set.
- Personal/reference projections cannot create the authoritative engineering-repository target set.
- No GitHub or canonical writes.
- Tampered/malformed projection bytes fail closed through the same digest/provenance validation used by retrieval.
- Output is capped and deterministic; rebuild order does not change the result.
