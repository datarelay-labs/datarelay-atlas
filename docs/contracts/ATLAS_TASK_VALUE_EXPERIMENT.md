# Atlas task-value comparison — offline evidence contract v1

Status: DEVELOPMENT_EVAL / MEASUREMENT_ONLY
Related: Atlas roadmap #10 memory M5 and `docs/runbooks/atlas-value-validation.md`.
No production API, schema, memory-policy, authorization or routing change.

## Design boundary

**Goal:** measure whether Atlas reduces context preparation work without degrading verified correctness compared with canonical GitHub/Engineering System context only.

**Non-goals:** inferring task success from MCP call counts, collecting conversations/prompts, copying GitHub source code, analyzing employee productivity, automatically timing external tools, executing tasks, replacing canonical Git/CI review, granting engineering PASS, or modifying persistent production Atlas state.

**Input:** local JSON metadata-only study, with at least 20 matched pairs (40 observations), one CONTROL and one ATLAS treatment per pair. Compare only same repository, initial commit SHA and task class. Prefer randomized or counterbalanced *different comparable tasks*; rerunning the identical task after learning its answer is a confounding design. The evaluator cannot authenticate study assignment or reviewer evidence.

**Control:** canonical AGENTS.md, Engineering System and GitHub/current repository evidence without Atlas. **Treatment:** same canonical context plus one bounded Atlas project bootstrap and attributed JIT source retrieval when needed. Neither branch may skip tests, approval or review gates.

**State & migration:** none. The evaluator is offline/stdlib-only, never writes to Atlas, and reads at most 1 MiB from the explicitly named local study file. It returns only aggregate metadata, never reviewer identities, issue/pull URLs or tasks.

**Security:** only pseudonymous pair/reviewer IDs, repository slug, exact starting SHA, task class, timing, context byte counts and bounded outcome counters. No raw prompts, issue bodies, commands, tokens, customer context or keys. Review URLs must be HTTPS GitHub resources in the exact declared repository. Such URLs and reviewer claims are untrusted until independently revalidated against GitHub; no study JSON can authorize a merge, release, task PASS or routing change.

## Input schema

The JSON file contains exactly:

- `schema_version` = 1, `scope` = `METADATA_ONLY`, `study_id` = bounded pseudonymous identifier, `observations` = array
- Each observation: `pair_id`, `condition` (`CONTROL` or `ATLAS`), `repository` (`datarelay-labs/*`), `start_head` (40-digit lowercase hex), `work_class` (`BUGFIX`, `FEATURE`, `INVESTIGATION` or `DOCS`), `task_completed` (bool), `verified_outcome` (`PASS`, `FAIL` or `UNKNOWN`)
- Measured values: `preparation_seconds`, `context_bytes`, `reread_count`, `owner_reexplanations`, `stale_reference_count`, `first_pass_defects` (nonnegative bounded integers)
- Provenance and privacy: `used_atlas_reference` and `public_metadata_only` (bools), plus nullable `review` object with `reviewer_id`, `verified_at` (UTC Z), `evidence_url` (exact-repository GitHub pull/issue/CI-run/commit URL)
- `CONTROL` observations cannot claim to consume an Atlas reference. A declared PASS requires `task_completed` and a pair-matched record; an independently reviewed declaration cannot mark outcome `UNKNOWN`.

All fields are required, including `review: null` and zero-valued metrics when genuinely measured. Never replace unavailable measurements with zero to force a complete result.

## Execution

No-data confirmation:

    python3 scripts/atlas_task_value_eval.py

Real study after collection:

    python3 scripts/atlas_task_value_eval.py --study /path/to/approved-study.json

**Partial samples:** `NO_DATA`, `INSUFFICIENT` or `INCOMPLETE`; no savings rates, no invented success.
**Fully provided samples:** `COMPLETE_DECLARED_NOT_ATTESTED`; median preparation time and median context bytes, paired quality/staleness/first-pass defects, and **proposed** >=25% preparation reduction or >=20% context reduction with no regression are calculated. Threshold metrics require 20 pairs, declared independent outcome reviews and at least one real Atlas-reference usage. Even a positive result remains `productivity_benefit: NOT_ESTABLISHED` and `independent_review_attestation: NOT_ATTESTED` until source GitHub/CI and study-method evidence is independently audited.

The report includes an input SHA-256 digest for deterministic evidence binding. It does not contain raw task descriptions, links or private values.

## Acceptance / follow-up

Run at least 20 *real* matched task pairs, preserve exact-HEAD evidence and actual timing observations outside this tool, audit reviewer independence and real GitHub/CI results, then decide whether Atlas should be promoted based on correct outcomes and measured saved work, not MCP traffic or synthetic tests. If quality regresses, preserve the existing deterministic canonical workflow and keep all Atlas retrieval/ranking expansion measurement-gated. This contract does not change Engineering System release gates.
