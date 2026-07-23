#!/usr/bin/env bash
set -euo pipefail

LABEL="com.smartmoneyradar.dashboard"
UID_VALUE="$(id -u)"
PORT="${RADAR_DASHBOARD_PORT:-8787}"
SCREEN_NAME="${RADAR_DASHBOARD_SCREEN_NAME:-radar-dashboard}"

echo "Processes:"
ps ax -o pid=,ppid=,etime=,command= | awk '/[p]ython.*smart_money_radar[.]dashboard|[p]ython.*run_dashboard/{print}' || true

echo
echo "screen:"
if command -v screen >/dev/null 2>&1; then
  screen -ls 2>/dev/null | awk -v name="$SCREEN_NAME" '$0 ~ name {print}' || true
else
  echo "screen unavailable"
fi

echo
echo "Port:"
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true

echo
echo "launchd:"
launchctl print "gui/$UID_VALUE/$LABEL" 2>&1 | sed -n '1,120p' || true
