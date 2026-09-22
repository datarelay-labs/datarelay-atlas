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
| `agent persist --trust /work-resume` (cwd = validated worktree, PTY) | Supported create-with-prompt path |
| `agent persist list\|attach\|stop` | Available for observe/manage |
| `agent --workspace <path> --trust persist` | Incorrect argv; do not use |
| `agent -p` print mode | Must **not** be used as a persistence substitute |
| PTY spawn | Transport only for unattended create; not business state |

Controller dispatch argv:

```text
agent persist --trust /work-resume
```

with `cwd=<validated-worktree>`. The PTY dispatcher observes a newly listed
session for that worktree only and must not stop/attach unrelated sessions.

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

# Re-register in a fresh data root to exercise REWORK dispatch argv:
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

Expected REWORK outcome includes:
`dispatch_command = ["agent", "persist", "--trust", "/work-resume"]`
and `resume_prompt = "/work-resume"`.

To exercise the real PTY launcher (creates a live Cursor persist session in
this worktree only):

```bash
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-adapter fixed \
  --audit-verdict REWORK \
  --audit-findings "launcher dogfood" \
  --spawn-dispatch
```

Stop only the newly created target session afterward via
`agent persist stop <session>` if needed. Do not stop unrelated sessions.

## Live Codex audit adapter (default production)

Codex CLI must be installed and logged in with the owner's ChatGPT account
(`codex login status` => Logged in using ChatGPT). No `OPENAI_API_KEY` is
required for the default path.

```bash
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-adapter codex
```

Contract:
- Controller gathers deterministic evidence (git, Work Packet, tests, CI)
- Autonomous path requires clean `git status --porcelain` after evidence and
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

OpenAI Responses API (`--audit-adapter openai`) is optional fallback only.
Offline/deterministic mode requires explicit `--audit-adapter fixed`.

## Completion hook helper

`scripts/awc-completion-hook.sh` builds an event from the current git identity
and enqueues/drains the local inbox without Telegram. Default audit adapter is
`codex`. Unknown `AWC_AUDIT_ADAPTER` values fail closed.

Production default is **unattended**: when draining, the hook passes
`--spawn-dispatch` so a `REWORK` verdict can launch a fresh Cursor session with
exact argv `agent persist --trust /work-resume`. Set
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

## Telegram / notify

Observational only. Workflow progress must not require Telegram delivery.

## Non-goals

Temporal, n8n, multi-tenant scheduling, public webhooks, production UI, and
Phase 2 retrieval/MCP/Web scope are out of this PoC.
