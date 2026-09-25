#!/usr/bin/env bash
# Cron-safe single-instance wrapper for host-worker supervise-once.
# Does not register a scheduled job. The descriptor file is host-local and
# must stay outside Git. Chat IDs, worktrees, and credentials are not arguments.
set -euo pipefail

DESCRIPTORS="${1:?host-local descriptor file is required}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${ATLAS_SUPERVISE_LOCK:-${XDG_STATE_HOME:-$HOME/.local/state}/datarelay-atlas/supervise-once.lock}"
mkdir -p "$(dirname "$LOCK")"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
exec flock -n "$LOCK" python3 -m atlas host-worker supervise-once --descriptors "$DESCRIPTORS"
