# Continuous Chat Audit Supervisor PoC

Operator notes for ADR-0007 / Issue #20.

## Purpose

Run bounded, delta-first repository audit slices that survive Chat timeout and
conversation rollover. Durable state is the Audit Control Packet, not chat
history.

## Local checkpoint

Default path: `.atlas-data/chat-audit.json` (override with `ATLAS_DATA_ROOT`).

## Commands

```bash
# Initialize from exact HEAD
python -m atlas chat-audit init \
  --repository datarelay-labs/datarelay-atlas \
  --branch main \
  --head "$(git rev-parse HEAD)"

# Show checkpoint
python -m atlas chat-audit show

# Run one bounded slice (also auto-inits when missing).
# Default adapter requires external COMPLETE evidence and will not auto-PASS.
python -m atlas chat-audit run-slice \
  --repository datarelay-labs/datarelay-atlas \
  --branch main \
  --head "$(git rev-parse HEAD)" \
  --evidence-file ./evidence/slice.json

# Explicit offline/test synthesizer only (never the operator default):
python -m atlas chat-audit run-slice --unit-adapter fixed ...

# Resume payload for a fresh Chat (no conversation history required)
python -m atlas chat-audit resume-payload

# Mark session + optional fake rollover
python -m atlas chat-audit mark-session ROLLOVER_REQUIRED
python -m atlas chat-audit rollover
```

Fresh Chat / scheduled Chat entrypoint: `/chat-audit-resume`.

## Guardrails

- No OpenAI API key required on the default path.
- Truncated evidence cannot PASS.
- Stagehand rollover is optional and gated on Issue #19.
- Do not merge from Chat; hand findings to Cursor `[AI Work]` packets.
