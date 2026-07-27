#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_DIR="${RADAR_ROOT_DIR:-$SCRIPT_ROOT}"
PYTHON_BIN="${RADAR_DASHBOARD_PYTHON:-/opt/homebrew/bin/python3}"
HOST="${RADAR_DASHBOARD_HOST:-127.0.0.1}"
PORT="${RADAR_DASHBOARD_PORT:-8787}"

cd "$ROOT_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting dashboard on ${HOST}:${PORT}"
exec "$PYTHON_BIN" -u -c "from smart_money_radar.dashboard import run_dashboard; run_dashboard(host='${HOST}', port=int('${PORT}'))"
