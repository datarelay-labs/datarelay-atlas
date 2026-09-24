# Autonomous Work Controller PoC — dogfood runbook

Bounded self-dogfood loop for ADR-0006: Cursor completion event → independent
audit → `PASS` / `REWORK` / `HUMAN_REQUIRED`, with automatic fresh
`/work-resume` dispatch on rework.

## Prerequisites

- This repository checkout (validated worktree path)
- Python 3.11+
- Cursor CLI (`agent persist --help` / `agent persist list`)
- Optional: `OPENAI_API_KEY` when using `--audit-adapter openai`
- Never place secrets in Git, Issue bodies, or `.atlas-data/`

## Data root

- Default: `./.atlas-data/` (gitignored)
- Controller state: `<data-root>/work-controller.json`
- Completion inbox: `<data-root>/completion-inbox/`
- Processed events: `<data-root>/completion-processed/`
- Override: `--data-root` or `ATLAS_DATA_ROOT`
- Backup: copy the data root directory (same implication as ADR-0005)

## Canonical resume command

Engineering System / repository command file is
`.cursor/commands/work-resume.md`). `.cursor/commands/resume.md` remains only as an Engineering System adoption-compliance shim and must not redefine `/resume` as canonical.
Slash command: `/work-resume`. Do not redefine built-in `/resume` as canonical.

## Cursor persistence launcher (CLI 2026.09.18-9a7762b)

| Mechanism | Status |
| --- | --- |
| `agent persist --force --trust /work-resume` (cwd = validated worktree, PTY) | Supported unattended create path. `--force` follows `persist` and is Run Everything |
| `agent --force persist --trust /work-resume` | Unknown command; not canonical |
| `agent persist list\|attach\|stop` | Available for observe/manage |
| `agent --workspace <path> --trust persist` | Incorrect argv; do not use |
| `agent -p` / `--print` | Must **not** be used as a persistence substitute |
| PTY spawn | Transport only for unattended create; not business state |

Controller dispatch argv:

```text
agent persist --force --trust /work-resume
```

with `cwd=<validated-worktree>`. `--force` follows `persist`. `--trust` trusts
the workspace and the prompt remains `/work-resume`. Durable dispatch success
requires a new `agent persist list` session for that worktree whose id is
named by the owned spawn's process tree. Another new session in the same
worktree is not this launch. A target process is diagnostic only and must not
be reported as success. Do not stop/attach unrelated sessions. If no owned
session appears before the bounded timeout, terminate the entire owned
process group and fail closed only after every member is gone. A surviving
child keeps cleanup uncertain. If that cleanup cannot be verified, the
controller records `HUMAN_REQUIRED` with `reason=spawn_cleanup_uncertain` and
does not rewrite the canonical packet into a dispatch-blocked state.

Before spawn, the dispatcher runs the Engineering System resource preflight.
The canonical explicit script path is
`ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD` (the override named by
`/work-resume`). `ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT` is a
compatibility alias used only when `..._GUARD` is unset. When neither is set,
the script is `tools/cursor-resource-preflight.py` under
`ENGINEERING_SYSTEM_ROOT`. A set explicit path that is not an existing file
is unavailable. Exit 0 (`PASS` or `WARN`) may spawn. A nonzero, unknown, or
unavailable result is `BLOCK`: the controller finalizes `HUMAN_REQUIRED` with
`reason=resource_preflight_blocked`, creates no session, and does not stop
existing sessions. The final repo/branch/HEAD and clean-porcelain check still
runs immediately before a permitted spawn.

## Offline deterministic dogfood (no network)

```bash
export PYTHONPATH=.
export ATLAS_DATA_ROOT="$(pwd)/.atlas-data-awc"
rm -rf "$ATLAS_DATA_ROOT"

HEAD="$(git rev-parse HEAD)"
BRANCH="$(git branch --show-current)"
WT="$(git rev-parse --show-toplevel)"

python3 -m atlas work-controller register autonomous-work-controller-poc \
  --repository datarelay-labs/datarelay-atlas \
  --issue-number 12 \
  --branch "$BRANCH" \
  --worktree "$WT" \
  --expected-head "$HEAD" \
  --max-attempts 3

cat > /tmp/awc-completion.json <<EOF
{
  "event_id": "dogfood-1",
  "workstream": "autonomous-work-controller-poc",
  "issue_number": 12,
  "branch": "$BRANCH",
  "head": "$HEAD",
  "attempt": 1,
  "session_id": "local-dogfood"
}
EOF

# PASS path via inbox ingestion (Cursor hook shape)
python3 -m atlas work-controller enqueue-completion /tmp/awc-completion.json
python3 -m atlas work-controller drain-inbox \
  --audit-adapter fixed --audit-verdict PASS

# Offline fixed REWORK with the default recording adapter and no
# `--spawn-dispatch`. This records the finding locally and does not launch
# Cursor. It must not be read as `REWORK_DISPATCHED`.
rm -rf "$ATLAS_DATA_ROOT"
python3 -m atlas work-controller register autonomous-work-controller-poc \
  --repository datarelay-labs/datarelay-atlas \
  --issue-number 12 \
  --branch "$BRANCH" \
  --worktree "$WT" \
  --expected-head "$HEAD"
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-adapter fixed \
  --audit-verdict REWORK \
  --audit-findings "deterministic rework finding"

python3 -m atlas work-controller show autonomous-work-controller-poc
```

Expected outcome of that recording / no-spawn completion:

- `state` = `HUMAN_REQUIRED`
- `verdict` = `HUMAN_REQUIRED`
- `action` = `stop`
- `reason` = `dispatch_boundary_failed`
- findings say audit-only mode cannot claim Cursor dispatch
- no `dispatch_command` and no `resume_prompt`
- `show` reports `state` = `HUMAN_REQUIRED`

`--audit-adapter fixed` defaults to `--work-packet-adapter recording`, so this
example does not mutate GitHub Issue #12 and does not start a Cursor session.

Production Codex paths default to `--work-packet-adapter github`, which
updates the same canonical `[AI Work]` Issue (findings + next action) via safe
`gh` argv/`--body-file` **before** Cursor dispatch, after a body/`updatedAt`
recheck. Dispatch-blocked compensation rewrites that packet only when the
canonical body is still the controller-owned pending transition (pending
marker, workstream, head, and attempt). An intervening edit, including
`PAUSED`/`BLOCKED` or a changed head/attempt, fails closed and is not
overwritten. GitHub Issues PATCH rejects conditional headers (`If-Match` /
`If-Unmodified-Since` → HTTP 400), so a residual TOCTOU remains and is accepted
as a platform limit for the initial mutation. Mutation failure fails closed as
`HUMAN_REQUIRED` with no spawn / no `REWORK_DISPATCHED`. GitHub mutation is
paired only with real `--spawn-dispatch`.

OpenAI remains `--work-packet-adapter recording` and audit-only, without
`--spawn-dispatch`, until it has the same deterministic evidence bundle as
Codex. It does not mutate canonical GitHub state and does not spawn.

Real dispatch is a later milestone. It is not a substitute for the audit-only
example above. When that milestone is in scope, pair fixed audit with the
canonical GitHub Work Packet adapter and an explicit spawn. That command
mutates Issue #12 and creates a live Cursor persist session in this worktree:

```bash
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-adapter fixed \
  --audit-verdict REWORK \
  --audit-findings "launcher dogfood" \
  --work-packet-adapter github \
  --spawn-dispatch
```

Only that spawned path includes
`dispatch_command = ["agent", "persist", "--force", "--trust", "/work-resume"]`
and `resume_prompt = "/work-resume"`. Stop only the newly created target
session afterward via `agent persist stop <session>` if needed. Do not stop
unrelated sessions.

## Live Codex audit adapter (default production)

Codex CLI must be installed and logged in with the owner's ChatGPT account
(`codex login status` => Logged in using ChatGPT). No `OPENAI_API_KEY` is
required for the default path.

```bash
# Live Codex with real REWORK dispatch (mutates GitHub Work Packet, then spawns)
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-adapter codex \
  --spawn-dispatch

# Audit-only Codex (no GitHub mutation, no Cursor spawn)
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-adapter codex \
  --work-packet-adapter recording
```

Contract:
- Controller gathers deterministic evidence (git, Work Packet, tests, CI)
- Autonomous path requires clean `git status --porcelain --untracked-files=all` after evidence and
  before deterministic gates or Codex (dirty/drift ⇒ `HUMAN_REQUIRED`; never
  map dirty + tests/CI FAIL to autonomous `REWORK`)
- Deterministic gates from a clean snapshot only: tests FAIL / CI FAIL ⇒
  `REWORK`; tests ERROR / CI PENDING|ERROR ⇒ `HUMAN_REQUIRED`; CI ABSENT allowed
- Dispatch boundary captures session/proc baselines first, then revalidates
  exact repo/branch/HEAD + clean porcelain immediately before spawning Cursor
  (no external observation between validation and spawn); boundary
  `ValidationError` finalizes `HUMAN_REQUIRED` (no spawn)
- `codex exec -C <worktree> -s read-only --ephemeral --ignore-user-config
  --ignore-rules` with apps/browser/computer/shell/plugins/hooks/multi-agent
  disabled (`--disable …`, `web_search="disabled"`)
- Codex judges only the embedded evidence bundle (no Codex local shell required)
- structured JSON verdict `PASS|REWORK|HUMAN_REQUIRED`
- exact worktree identity validation (repo/branch/HEAD) before audit
- no edits/commits/pushes

OpenAI Responses API (`--audit-adapter openai`) is metadata-only. It may run
audit-only with `--work-packet-adapter recording` and without
`--spawn-dispatch`. It cannot mutate the canonical GitHub Work Packet or spawn
Cursor until it carries the same deterministic evidence bundle as Codex.
Offline/deterministic mode requires explicit `--audit-adapter fixed`.

## Completion hook helper

`scripts/awc-completion-hook.sh` builds an event from the current git identity
and enqueues/drains the local inbox without Telegram. Default audit adapter is
`codex`. Unknown `AWC_AUDIT_ADAPTER` values fail closed.

Codex production default is **unattended**: when draining, the hook passes
`--spawn-dispatch` so a `REWORK` verdict can launch a fresh Cursor session with
exact argv `agent persist --force --trust /work-resume`. OpenAI stays
recording/audit-only (`AWC_SPAWN_DISPATCH=0` is forced) until evidence parity.
The hook inherits `ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD`, the
`ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT` alias, or
`ENGINEERING_SYSTEM_ROOT`; without a usable preflight the spawn fails closed. Set
`AWC_SPAWN_DISPATCH=0` only for audit-only / operator opt-out (drain without
auto-dispatch).

```bash
# Production default (Codex / ChatGPT plan; auto-dispatch on REWORK)
AWC_WORKSTREAM=autonomous-work-controller-poc \
AWC_ISSUE_NUMBER=12 \
AWC_ATTEMPT=1 \
./scripts/awc-completion-hook.sh

# Audit-only opt-out: drain without spawning REWORK dispatch
AWC_WORKSTREAM=autonomous-work-controller-poc \
AWC_ISSUE_NUMBER=12 \
AWC_ATTEMPT=1 \
AWC_SPAWN_DISPATCH=0 \
./scripts/awc-completion-hook.sh

# Explicit offline/fixed only when deliberately requested
AWC_WORKSTREAM=autonomous-work-controller-poc \
AWC_ISSUE_NUMBER=12 \
AWC_ATTEMPT=1 \
AWC_AUDIT_ADAPTER=fixed \
AWC_AUDIT_VERDICT=PASS \
./scripts/awc-completion-hook.sh
```

## Queued Work Packet cycle

A successor waiting on this packet uses the Engineering System status vocabulary
plus an Atlas queue marker:

```text
STATUS=PAUSED
QUEUE_STATE=QUEUED
AFTER_ISSUE=<predecessor issue number>
```

`/work-resume` does not execute that packet. After a verified PASS, the
controller completes the predecessor (`STATUS=COMPLETE`, issue left open) and
only then activates exactly one successor on the same branch and the same
`WORKSTREAM` (`STATUS=ACTIVE`, `QUEUE_STATE=NONE`) before one
`agent persist --force --trust /work-resume` dispatch. Zero queued successors
stop as local `PASSED` without a GitHub write. Two eligible successors, a
malformed or untrusted queued packet, a branch or `WORKSTREAM` mismatch, a
stale compare-and-set, or another trusted ACTIVE packet for that same
`WORKSTREAM` (any branch) stops `HUMAN_REQUIRED` without guessing. REWORK does
not chain.

## Telegram / notify

Observational only. Workflow progress must not require Telegram delivery.

## Non-goals

Temporal, n8n, multi-tenant scheduling, public webhooks, production UI, and
Phase 2 retrieval/MCP/Web scope are out of this PoC.
