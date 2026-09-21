# Autonomous Work Controller PoC — dogfood runbook

Bounded self-dogfood loop for ADR-0006: Cursor completion event → independent
audit → `PASS` / `REWORK` / `HUMAN_REQUIRED`, with optional fresh `/resume`
dispatch on rework.

## Prerequisites

- This repository checkout (validated worktree path)
- Python 3.11+
- Cursor CLI (investigate with `agent persist --help` / `agent help persist`)
- Optional: `OPENAI_API_KEY` only when wiring a live OpenAI audit adapter
- Never place secrets in Git, Issue bodies, or `.atlas-data/`

## Data root

- Default: `./.atlas-data/` (gitignored)
- Controller state: `<data-root>/work-controller.json`
- Override: `--data-root` or `ATLAS_DATA_ROOT`
- Backup: copy the data root directory (same implication as ADR-0005)

## Canonical resume command

Engineering System / repository command file is `.cursor/commands/resume.md`.
Slash command: `/resume`. Do not invent a divergent repository-local name.

## Cursor persistence finding (CLI 2026.09.18-9a7762b)

| Mechanism | Status |
| --- | --- |
| `agent persist list\|attach\|stop` | Available |
| Interactive `agent persist` in a trusted worktree | Documented long-term native start path |
| `agent persist <prompt>` create-with-prompt | Not reliable on this build (`help persist` lists only list/attach/stop) |
| `agent -p` print mode | Must **not** be used as a silent persistence substitute |
| tmux PTY bootstrap | Packet-only bootstrap exception; not a product dependency |

Controller dispatch records fixed argv:

```text
agent --workspace <validated-worktree> --trust persist
```

Operators then attach and submit `/resume` for a fresh cycle. Do not reuse
unrelated persist sessions from other worktrees.

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

# PASS path
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-verdict PASS

# Re-register in a fresh data root to exercise REWORK argv recording:
rm -rf "$ATLAS_DATA_ROOT"
python3 -m atlas work-controller register autonomous-work-controller-poc \
  --repository datarelay-labs/datarelay-atlas \
  --issue-number 12 \
  --branch "$BRANCH" \
  --worktree "$WT" \
  --expected-head "$HEAD"
python3 -m atlas work-controller completion /tmp/awc-completion.json \
  --audit-verdict REWORK \
  --audit-findings "deterministic rework finding"

python3 -m atlas work-controller show autonomous-work-controller-poc
```

## Live audit adapter

Inject `OpenAIAuditAdapter` with an `execute` callable that performs HTTPS calls
using `OPENAI_API_KEY` from the environment. Unit tests must continue to use
`FixedAuditAdapter` only.

## Telegram / notify

Observational only. Workflow progress must not require Telegram delivery.

## Non-goals

Temporal, n8n, multi-tenant scheduling, public webhooks, production UI, and
Phase 2 retrieval/MCP/Web scope are out of this PoC.
