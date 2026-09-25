#!/usr/bin/env bash
# Cron-safe single-instance wrapper for host-worker supervise-once.
# Does not register a scheduled job. The descriptor file is host-local and
# must stay outside Git. Chat IDs, worktrees, and credentials are not arguments.

atlas_py_bin() {
  local py="${ATLAS_PYTHON:-}" candidate="${1}/.venv/bin/python"
  if [[ -n "$py" ]]; then
    [[ -f "$py" && -x "$py" ]] && printf '%s\n' "$py" && return 0
    printf 'not an executable file: %s\n' "$py" >&2
    return 1
  fi
  [[ -f "$candidate" && -x "$candidate" ]] && printf '%s\n' "$candidate" && return 0
  printf 'no project Python\n' >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

set -euo pipefail
DESCRIPTORS="${1:?host-local descriptor file is required}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${ATLAS_SUPERVISE_LOCK:-${XDG_STATE_HOME:-$HOME/.local/state}/datarelay-atlas/supervise-once.lock}"
mkdir -p "$(dirname "$LOCK")"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
ATLAS_PY="$(atlas_py_bin "$ROOT")"
exec flock -n "$LOCK" "$ATLAS_PY" -m atlas host-worker supervise-once --descriptors "$DESCRIPTORS"
