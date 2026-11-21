# Atlas utility: baseline, minimal intervention, proof protocol

Status: DEVELOPMENT-ONLY, no production deployment or release claim.
Authority: observability and evaluation only; never supersedes GitHub, source, CI, tests, or the Engineering System.

## Why this exists

A successful Atlas MCP tool request is not the same as a completed engineering task.
A stored memory observation is not proof that Atlas saved time. Separate:

1. **Availability**: service, auth, and MCP tool-call receipts.
2. **Adoption**: actual distinct human/AI task sessions using Atlas (not derivable from anonymous tool receipts).
3. **Context quality**: important facts present and current at the exact repository/HEAD.
4. **Productivity**: verified time, rework, context bytes and repeated owner explanations compared with a control cohort.

## Measured starting point — 2026-11-19

Read-only native Atlas calls and live prod-atlas journal sample (November 1–19;
actual sampled end time is the audit execution time):

| Evidence | Observed |
| --- | --- |
| get_memory_effectiveness | INSUFFICIENT, 2 observations of minimum 10 |
| Atlas-project Engineering System evidence | 0 records |
| Service MCP execution receipts | 113 total: 66 SUCCESS, 47 BLOCK |
| bootstrap_datarelay_context | 43 SUCCESS, 46 BLOCK |
| Separate failure log messages | 47 (44 invalid workstream, 3 unknown repository) |

The latter failure log entries can describe the *same* executions counted as
BLOCK, so they are never added to the total. Journal entries cannot distinguish
test calls from user tasks; neither user adoption nor time savings is measured.
No valid data supports a 100% first-pass success-rate claim from only two
memory observations.

## Minimal product intervention

- Project-only context now advertises **up to four provenance-verified entrypoint
  references** from README.md and docs/product/PRODUCT-CHARTER.md if present and
  matched to the project display name; source content is not injected. Unknown
  or unrelated files remain omitted, explicit workstream retrieval is unchanged.
- MCP tool description and usage contract now explicitly require callers to
  **omit workstream unless the exact ACTIVE Work Packet slug is known**.
  Branch names with slashes and free-form prose are invalid. Existing
  fail-closed workstream validation is **not** relaxed.
- A local, read-only reporting script counts execution receipts and categorizes
  sanitized error reasons without persisting or printing prompts, user IDs,
  request arguments, keys, or unbounded error text.

Read-only usage diagnostic on an authorized host:

    journalctl -u datarelay-atlas.service --since '7 days ago' -o json --no-pager |
      python3 scripts/atlas_mcp_usage_report.py

Run a before/after comparison only after an independently qualified exact-HEAD
deployment and its actual native-client tests. Do not claim the development
branch has fixed the production deployment.

## Actual value experiment

Run at least **20 comparable, independently verified engineering tasks per
condition**, randomized or counterbalanced across project/task difficulty:

- Control: mandatory canonical GitHub/AGENTS/project.yaml context only.
- Treatment: identical canonical context plus one Atlas bootstrap and JIT source
  retrieval only when it supplies relevant, verified information.
- Preserve the exact task acceptance criteria and current repository/HEAD.
  Do not let Atlas replace canonical branch/PR/CI verification.

Persist **aggregated** (non-sensitive) task outcomes only: anonymous task ID,
condition, task class, project, baseline/current HEAD, completion and test outcome,
elapsed active minutes, context bytes, tool calls, rereads, first-pass defects,
repeated owner explanations, and whether a reference was used and corroborated.
Do not persist prompts, secrets, source payloads, raw shell output, or tokens.
Compare median elapsed time and context bytes, not just success percentages.

Proposed investment threshold: treatment >=25% shorter task context preparation,
>=20% fewer context bytes or rereads, no decrease in verified correct outcomes,
and no increase in stale/cross-project reference use. These are **targets, not
observed improvement claims**. Missing/unavailable samples remain UNKNOWN; never
replace them with synthetic successes. Run a separate paired task review before
promoting memory-effectiveness from INSUFFICIENT.

If the proof fails, retain only the low-cost provenance-backed retrieval and
consider retiring the optional Automation/Decision layers. This experiment does
not authorize production changes; coordinate any eventual cutover with the
separately approved Atlas/DRLink hosting plan.


## Executable task-value A/B measurement tool

The no-network metadata comparison is described by
`docs/contracts/ATLAS_TASK_VALUE_EXPERIMENT.md`. The CLI now implements the
20-paired-task measurement gate without inferring task value from MCP
success counts. It produces truthful `NO_DATA` when no real evidence is
supplied; all values are EVALUATION_ONLY and never override canonical Git.

~~~sh
python3 scripts/atlas_task_value_eval.py
python3 scripts/atlas_task_value_eval.py --study /path/to/approved-study.json
~~~

Never submit synthetic unit-test observations as real deployment productivity.
