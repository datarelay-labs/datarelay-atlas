#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "root is required" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_SRC="$ROOT/scripts/prod-context-refresh.py"
SERVICE_SRC="$ROOT/deploy/systemd/atlas-prod-refresh.service"
TIMER_SRC="$ROOT/deploy/systemd/atlas-prod-refresh.timer"
SCRIPT_DST=/usr/local/lib/datarelay-atlas/prod-context-refresh.py
SERVICE_DST=/etc/systemd/system/atlas-prod-refresh.service
TIMER_DST=/etc/systemd/system/atlas-prod-refresh.timer
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP="/var/backups/datarelay-atlas-operator/prod-refresh/$STAMP"

test -f "$SCRIPT_SRC"
test -f "$SERVICE_SRC"
test -f "$TIMER_SRC"
test -x /home/aella/.local/share/datarelay-atlas/prod-refresh-venv/bin/python

install -d -o root -g root -m 0700 "$BACKUP"
for src in "$SCRIPT_DST" "$SERVICE_DST" "$TIMER_DST"; do
  if [ -e "$src" ]; then
    cp -a "$src" "$BACKUP/"
  fi
done

install -d -o root -g root -m 0755 /usr/local/lib/datarelay-atlas
install -o root -g root -m 0755 "$SCRIPT_SRC" "$SCRIPT_DST"
install -o root -g root -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
install -o root -g root -m 0644 "$TIMER_SRC" "$TIMER_DST"
systemctl daemon-reload
systemctl enable --now atlas-prod-refresh.timer

echo "PROD_REFRESH_INSTALL=PASS"
echo "PROD_REFRESH_ROLLBACK_DIR=$BACKUP"
