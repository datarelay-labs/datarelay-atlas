#!/usr/bin/env bash
# Cron-safe single-instance wrapper for host-worker supervise-once.
# Does not register a scheduled job. The descriptor file is host-local and
# must stay outside Git. Chat IDs, worktrees, and credentials are not arguments.

atlas_py_bin() {
  local py="${ATLAS_PYTHON:-${1}/.venv/bin/python}"
  [[ -f $py && -x $py ]] && printf '%s\n' "$py" && return 0
  [[ -n ${ATLAS_PYTHON:-} ]] && echo "not executable: $py" >&2 && return 1
  printf 'no project Python\n' >&2; return 1
}
cron_bin_path() { printf '%s\n' "${HOME:+$HOME/.local/bin:}${PATH:-/usr/bin:/bin}"; }
if [[ ${BASH_SOURCE[0]} != "$0" ]]; then return 0; fi
set -euo pipefail
DESCRIPTORS="${1:?host-local descriptor file is required}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${ATLAS_SUPERVISE_LOCK:-${XDG_STATE_HOME:-$HOME/.local/state}/datarelay-atlas/supervise-once.lock}"
mkdir -p "$(dirname "$LOCK")"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}" PATH="$(cron_bin_path)"
for c in gh agent; do command -v "$c" >/dev/null || { echo "$c not found" >&2; exit 1; }; done
exec flock -n "$LOCK" "$(atlas_py_bin "$ROOT")" -m atlas host-worker supervise-once --descriptors "$DESCRIPTORS"
