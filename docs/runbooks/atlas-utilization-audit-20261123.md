# Atlas utility and roadmap audit — 2026-11-23

Classification: OBSERVABILITY_ONLY for the evidence below; this document
does not grant release/production/credential authority.

## Source of truth and present usage

- Canonical: datarelay-labs/datarelay-atlas, Issue #10 roadmap and ACTIVE
  Work Packet #346; protected main at the time of audit:
  b08f727839ddfabbd04491cbd08e9e719cd01b28.
- Connected Atlas MCP: OBSERVED_READY; seven registered/enabled projects,
  57 configured sources and 56 projected documents; one disabled source.
  This is availability, NOT task success or confirmed operational benefit.
- Memory-effectiveness observation count 2 (minimum required 10):
  INSUFFICIENT. Apparent 100% sample first-pass success is not statistically
  meaningful and is not an actual engineering productivity claim.
- Controller workstreams: zero active; task-level effect on owner workload
  has not been established.

### Actual MCP tool-call receipts

Read-only production systemd journal (tool execution receipts only):

| Window as of 2026-11-23 | Calls | SUCCESS | BLOCK | Success |
| --- | ---: | ---: | ---: | ---: |
| 24 hours | 18 | 12 | 6 | 66.67% |
| 7 days | 53 | 45 | 8 | 84.91% |
| 30 days | 278 | 193 | 85 | 69.42% |

Of 278 calls in the 30-day window, 217 were bootstrap_datarelay_context.
Of 85 BLOCK receipts, invalid_workstream was the failure diagnostic on
75 separate error messages, ambiguous_repository on 4, and
unknown_repository on 6. Failure messages are associated diagnostics,
not independent additional tool invocations. Tests and actual end-user
tasks cannot be distinguished from journal receipts. A high bootstrap count
does not prove workflow benefit.

Existing usage-report CLI:

    journalctl -u datarelay-atlas.service --since '7 days ago' -o json --no-pager |
      python3 scripts/atlas_mcp_usage_report.py

It emits privacy-bounded aggregates and does not persist source arguments.

## Reproduced defect: stale operator + hidden open work

1. The dev-atlas scheduled production refresh ran and returned
   PROD_REFRESH=PASS, but the installed /usr/local/lib/datarelay-atlas/
   operator-src/atlas/work_controller.py is an October 5 version lacking
   the recognized Markdown title envelope for current ACTIVE AI Work.
2. The live production GitHub snapshot labelled Issue #346 INVALID,
   noncanonical, missing TARGET_REPO/BRANCH/HEAD/STATUS due parser failure.
   Atlas then selected historical COMPLETE packets rather than pointing to
   the outstanding Issue #346.
3. Using the **current main** reader to recapture the exact repository
   through GitHub (read-only), Issue #346 parses as ACTIVE, BRANCH=main,
   historical HEAD=9fe563252b184a65381922eccc3e5787dffdeef3. But its
   implicit branch-only association with unrelated closed PR #349
   (PR HEAD=9177c976a591143b045ebdeabdbccd0d48b87c6c) yields
   PR_HEAD_MISMATCH, canonical_fact=false. This is an unverified active
   declaration, not an authoritative current HEAD.
4. The source fix added in this branch makes both Atlas MCP task context and
   Human Web UI signal the bounded Issue number + reason and never quietly
   substitute historical COMPLETE work as the current task. When zero
   canonical facts exist, historical STALE semantics remain intact; no
   unverified candidate gains execution/release authority.
5. The production refresh script now has a read-only
   --check-operator option and verifies real titled Work Packet parsing
   before authenticated GitHub requests or production writes. Testing the
   installed October operator: FAIL, testing the current repository:
   OPERATOR_PARSER_CONTRACT=PASS. The script does not silently self-upgrade.

Development-only read-only operator audit:

    python3 scripts/prod-context-refresh.py --check-operator \
      --repo-root /usr/local/lib/datarelay-atlas/operator-src \
      --atlas-python /home/aella/.local/share/datarelay-atlas/prod-refresh-venv/bin/python

On an authorized operator host, a FAIL indicates that the pinned operator
code needs **a separate exact-candidate qualified upgrade**, not a request
to disable parsers or manually rewrite canonical Work Packet facts.

## Roadmap audit disposition

| Workstream | Status after audit | Next real acceptance |
| --- | --- | --- |
| Core project registry/source/provenance/MCP | Implemented and operating | Current version and user journey |
| Memory M1–M5 | Implemented; value INSUFFICIENT | >=10 real memory observations; >=20 genuine paired task studies |
| Work Packet / lifecycle / derived currentness | Fix implemented in this branch, not production | Actual prod operator upgrade + live readback |
| Issue #346 bounded MCP knowledge/source write | ACTIVE, protected production rollout NO-GO | Exact-head native ChatGPT permission/E2E, backup, rollback, separate production approval |
| ATLAS-TOOL-1 public MCP A/B | Public provider 30/30 PoC; paired comparison INCOMPLETE | Identical native baseline + independent correctness/cost |
| ATLAS-TOOL-2 production external provider | NOT_ADOPTED_WITH_EVIDENCE | Legal, privacy, retention, caching and licensing clearance |
| ATLAS-TOOL-3,4 | Implemented, PR #364/#365 merged; CI tested | Product release gate separately |
| Provider Capacity Broker #55 | MEASUREMENT_READY | Real attributable capacity/cost/outcome evidence |
| Decision Plane #56 | MEASUREMENT_READY, previously no measurable wall-time benefit | Verified correct outcomes + real useful time/cost reduction |
| Adaptive routing #254 | Experimental BACKLOG, no production providers | Approved real multi-provider runtime and quality/cost evidence |
| Stagehand #19 | Optional credential-gated | Approved model credential and native non-synthetic browser gate |
| Raschke trading research #144 | Separate future research project, not Atlas utility core | Explicit research activation and independent market data |
| Governance knowledge #164 | Reference/noncanonical input | Only promote if an actual policy defect is reproduced |
| Draft protected production PR #350 | DRAFT and not release | Approved qualification and real user gates |

Earlier historical checkboxes in Issue #10 may describe already merged or
superseded work. Do not count each old checkbox as an outstanding new
implementation; reconcile it against current GitHub merged evidence.
No external model route or private search SaaS is auto-activated.

## Practical priority order

1. Keep the scheduled production refresh healthy and existing derived source
   content intact. Qualify the **exact** current operator source separately;
   the fix here is source-only. Do not replace running production services
   without approved backups, immutable code identity, same-candidate
   Surface Reconciliation + Full User E2E and rollback.
2. Correct current-context misleadingness first, without granting authority
   to an unverified open issue. Independently repair stale Packet/PR
   association in canonical Issue #346 as a separate owner-controlled edit;
   do not falsely set an unqualified release candidate to the new main HEAD.
3. After an approved production release, repeat actual native MCP
   bootstrap/search/provenance/task-context tests. Audit that fresh Issue #346
   appears as either canonical ACTIVE or an explicit verified warning,
   not buried under COMPLETE. Keep credential scopes unchanged without
   separate approval.
4. Re-measure 7-day invalid_workstream/scope failure rates using the same
   read-only receipts. Mark a reduction only after actual post-deployment
   evidence, not because code changed on GitHub.
5. Use real paired tasks to measure whether retrieving specific, current
   source links prevents rereads, re-explanations and first-pass defects.
   Stop expanding provider/decision automation without proof of benefit.

Completion definition: task context accuracy and safe source provenance,
real native workflow acceptance, stable operations, measured user value.
Tool-call volume, synthetic unit tests, and self-attested success are not
the completion criteria.
