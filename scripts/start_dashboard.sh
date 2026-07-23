#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/dashboard.manual.log"
LABEL="com.smartmoneyradar.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"
PORT="${RADAR_DASHBOARD_PORT:-8787}"
HOST="${RADAR_DASHBOARD_HOST:-127.0.0.1}"
SCREEN_NAME="${RADAR_DASHBOARD_SCREEN_NAME:-radar-dashboard}"
USE_LAUNCHD="${RADAR_DASHBOARD_USE_LAUNCHD:-0}"

mkdir -p "$LOG_DIR"

dashboard_pid() {
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

pid="$(dashboard_pid)"
if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  echo "Dashboard is already running on $HOST:$PORT: PID $pid"
  exit 0
fi

if [[ "$USE_LAUNCHD" == "1" && -f "$PLIST" ]]; then
  launchctl bootstrap "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$UID_VALUE/$LABEL" >/dev/null 2>&1 || true
  sleep 2
  pid="$(dashboard_pid)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Dashboard started by launchd on $HOST:$PORT: PID $pid"
    echo "LaunchAgent: $PLIST"
    exit 0
  fi
  echo "LaunchAgent did not expose a listener on $HOST:$PORT; starting screen fallback."
  launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
else
  launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
fi

if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  screen -dmS "$SCREEN_NAME" /bin/zsh -lc '
    cd "$1"
    exec python3 -u -c "from smart_money_radar.dashboard import run_dashboard; run_dashboard(host='\''$2'\'', port=int('\''$3'\''))" >> "$4" 2>&1
  ' _ "$ROOT_DIR" "$HOST" "$PORT" "$LOG_FILE"
  sleep 1
  pid="$(dashboard_pid)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Dashboard started in screen $SCREEN_NAME on $HOST:$PORT: PID $pid"
    echo "Log: $LOG_FILE"
    exit 0
  fi
fi

cd "$ROOT_DIR"
nohup python3 -u -c "from smart_money_radar.dashboard import run_dashboard; run_dashboard(host='$HOST', port=int('$PORT'))" \
  >> "$LOG_FILE" 2>&1 < /dev/null &
pid="$!"
disown "$pid" 2>/dev/null || true
sleep 1

listener_pid="$(dashboard_pid)"
if [[ -n "$listener_pid" ]] && kill -0 "$listener_pid" 2>/dev/null; then
  echo "Dashboard started on $HOST:$PORT: PID $listener_pid"
else
  echo "Dashboard start requested: PID $pid"
fi
echo "Log: $LOG_FILE"
