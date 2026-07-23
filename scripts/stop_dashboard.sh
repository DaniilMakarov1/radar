#!/usr/bin/env bash
set -euo pipefail

LABEL="com.smartmoneyradar.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"
PORT="${RADAR_DASHBOARD_PORT:-8787}"
SCREEN_NAME="${RADAR_DASHBOARD_SCREEN_NAME:-radar-dashboard}"

if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  echo "Dashboard LaunchAgent stopped."
fi

if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
fi

pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [[ -n "$pid" ]]; then
  kill -TERM "$pid" 2>/dev/null || true
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  echo "Dashboard stopped on port $PORT: PID $pid"
else
  echo "Dashboard is not running."
fi
