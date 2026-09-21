#!/usr/bin/env bash
# Local Cursor completion-hook helper for Autonomous Work Controller (ADR-0006).
# Writes a completion event into ATLAS_DATA_ROOT/completion-inbox and optionally drains it.
# Telegram/notify remains observational only and is intentionally not required here.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
DATA_ROOT="${ATLAS_DATA_ROOT:-$ROOT/.atlas-data}"

: "${AWC_WORKSTREAM:?AWC_WORKSTREAM is required}"
: "${AWC_ISSUE_NUMBER:?AWC_ISSUE_NUMBER is required}"
: "${AWC_ATTEMPT:?AWC_ATTEMPT is required}"

export EVENT_ID="${AWC_EVENT_ID:-hook-$(date -u +%Y%m%dT%H%M%SZ)}"
export BRANCH="$(git branch --show-current)"
export HEAD="$(git rev-parse HEAD)"
export SESSION_ID="${AWC_SESSION_ID:-}"

TMP_JSON="$(mktemp)"
trap 'rm -f "$TMP_JSON"' EXIT

python3 - "$TMP_JSON" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
event = {
    "event_id": os.environ["EVENT_ID"],
    "workstream": os.environ["AWC_WORKSTREAM"],
    "issue_number": int(os.environ["AWC_ISSUE_NUMBER"]),
    "branch": os.environ["BRANCH"],
    "head": os.environ["HEAD"],
    "attempt": int(os.environ["AWC_ATTEMPT"]),
}
session_id = os.environ.get("SESSION_ID", "").strip()
if session_id:
    event["session_id"] = session_id
path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 -m atlas --data-root "$DATA_ROOT" work-controller enqueue-completion "$TMP_JSON"

if [[ "${AWC_DRAIN:-1}" == "1" ]]; then
  EXTRA=()
  case "${AWC_AUDIT_ADAPTER:-fixed}" in
    openai)
      EXTRA+=(--audit-adapter openai)
      ;;
    codex)
      EXTRA+=(--audit-adapter codex)
      ;;
    fixed|*)
      EXTRA+=(--audit-adapter fixed --audit-verdict "${AWC_AUDIT_VERDICT:-PASS}")
      if [[ -n "${AWC_AUDIT_FINDINGS:-}" ]]; then
        EXTRA+=(--audit-findings "$AWC_AUDIT_FINDINGS")
      fi
      ;;
  esac
  if [[ "${AWC_SPAWN_DISPATCH:-0}" == "1" ]]; then
    EXTRA+=(--spawn-dispatch)
  fi
  python3 -m atlas --data-root "$DATA_ROOT" work-controller drain-inbox "${EXTRA[@]}"
fi
