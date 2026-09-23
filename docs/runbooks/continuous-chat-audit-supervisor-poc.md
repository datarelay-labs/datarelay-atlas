# Continuous Chat Audit Supervisor PoC

Operator notes for ADR-0007 / Issue #20.

## Purpose

Run bounded, delta-first repository audit slices that survive Chat timeout and
conversation rollover. Durable canonical state is the GitHub-backed Audit
Control Packet (and GitHub `[AI Work]` finding handoffs). Local
`chat-audit.json` / handoff JSON under `ATLAS_DATA_ROOT` are derived cache only.

## Canonical checkpoint

- Production: GitHub Contents API blob-SHA CAS via `--checkpoint-issue` /
  `ATLAS_CHAT_AUDIT_ISSUE` (keys path
  `.atlas/chat-audit/checkpoints/issue-N.json` on branch
  `atlas/chat-audit-control`, overridable with
  `ATLAS_CHAT_AUDIT_CHECKPOINT_BRANCH`). Issue body RMW is not canonical.
- Offline/test: `--allow-local-checkpoint` with `--unit-adapter fixed` or
  `--handoff local`.

## Commands

```bash
# Initialize from authoritative worktree identity (asserted repo/branch optional)
python -m atlas chat-audit init \
  --repository datarelay-labs/datarelay-atlas \
  --branch feature/continuous-chat-audit-supervisor-poc \
  --worktree "$PWD" \
  --checkpoint-issue 20

# Show canonical checkpoint (GitHub + local cache)
python -m atlas chat-audit show \
  --repository datarelay-labs/datarelay-atlas \
  --checkpoint-issue 20

# Run one bounded slice. Default adapter requires external COMPLETE evidence.
# Supplied --repository/--branch/--head are assertions against the worktree.
python -m atlas chat-audit run-slice \
  --repository datarelay-labs/datarelay-atlas \
  --branch feature/continuous-chat-audit-supervisor-poc \
  --worktree "$PWD" \
  --checkpoint-issue 20 \
  --evidence-file ./evidence/slice.json

# Explicit offline/test synthesizer only (never the operator default):
python -m atlas chat-audit run-slice \
  --unit-adapter fixed \
  --allow-local-checkpoint \
  --allow-trusted-identity \
  --handoff local \
  --repository datarelay-labs/datarelay-atlas \
  --branch main \
  --head "$(git rev-parse HEAD)"

# Resume payload for a fresh Chat (no conversation history required)
python -m atlas chat-audit resume-payload \
  --repository datarelay-labs/datarelay-atlas \
  --checkpoint-issue 20

# Mark session + optional fake rollover
python -m atlas chat-audit mark-session ROLLOVER_REQUIRED \
  --repository datarelay-labs/datarelay-atlas \
  --checkpoint-issue 20
python -m atlas chat-audit rollover \
  --repository datarelay-labs/datarelay-atlas \
  --checkpoint-issue 20
```

Fresh Chat / scheduled Chat entrypoint: `/chat-audit-resume`.

## Guardrails

- No OpenAI API key required on the default path.
- Truncated evidence cannot PASS.
- Exact 40-char SHAs only for durable current/last-audited identities.
- Production identity comes from `--worktree` (defaults to cwd); caller values
  are assertions and mismatch fails closed.
- Finding handoff success requires GitHub `[AI Work]` create/update; local JSON
  is not canonical success.
- GitHub checkpoint writes are Contents API blob-SHA CAS; a stale/concurrent
  writer fails closed (`HUMAN_REQUIRED` / retry) instead of last-writer-wins.
- Stagehand rollover is optional and gated on Issue #19.
- Do not merge from Chat; hand findings to Cursor `[AI Work]` packets.
