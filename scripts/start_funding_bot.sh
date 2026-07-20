#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.funding-paper-trader.pid"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/funding-paper-trader.log"
PATTERN="smart_money_radar.cli funding-paper-trader"
LABEL="com.smartmoneyradar.funding-paper-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"

mkdir -p "$LOG_DIR"

if [[ -f "$PLIST" ]]; then
  launchctl bootstrap "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  pid="$(ps ax -o pid=,command= | awk '/[p]ython.*-m smart_money_radar[.]cli funding-paper-trader/{print $1; exit}')"
  if [[ -z "$pid" ]]; then
    launchctl kickstart "gui/$UID_VALUE/$LABEL"
    sleep 1
    pid="$(ps ax -o pid=,command= | awk '/[p]ython.*-m smart_money_radar[.]cli funding-paper-trader/{print $1; exit}')"
  fi
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$PID_FILE"
    echo "Funding paper trader is running by launchd: PID $pid"
  else
    echo "Funding paper trader launchd job was kicked, but PID is not visible yet."
  fi
  echo "LaunchAgent: $PLIST"
  echo "Logs:"
  echo "  $LOG_DIR/funding-paper-trader.out.log"
  echo "  $LOG_DIR/funding-paper-trader.err.log"
  exit 0
fi

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Funding paper trader is already running: PID $existing_pid"
    echo "Log: $LOG_FILE"
    exit 0
  fi
fi

existing_pid="$(ps ax -o pid=,command= | awk '/[p]ython.*-m smart_money_radar[.]cli funding-paper-trader/{print $1; exit}')"
if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
  echo "$existing_pid" > "$PID_FILE"
  echo "Funding paper trader is already running: PID $existing_pid"
  echo "PID file refreshed: $PID_FILE"
  echo "Log: $LOG_FILE"
  exit 0
fi

cd "$ROOT_DIR"
nohup python3 -m smart_money_radar.cli funding-paper-trader \
  --target-notional "${FUNDING_PAPER_TARGET_NOTIONAL:-500}" \
  --entry-window-seconds "${FUNDING_PAPER_ENTRY_WINDOW_SECONDS:-180}" \
  --entry-min-lead-seconds "${FUNDING_PAPER_ENTRY_MIN_LEAD_SECONDS:-30}" \
  --entry-max-lead-seconds "${FUNDING_PAPER_ENTRY_MAX_LEAD_SECONDS:-60}" \
  --arm-window-seconds "${FUNDING_PAPER_ARM_WINDOW_SECONDS:-900}" \
  --max-settlement-publication-lag-seconds "${FUNDING_PAPER_MAX_SETTLEMENT_PUBLICATION_LAG_SECONDS:-300}" \
  --min-live-net-profit "${FUNDING_PAPER_MIN_LIVE_NET_PROFIT:-0}" \
  --scan-interval-seconds "${FUNDING_PAPER_SCAN_INTERVAL_SECONDS:-60}" \
  --hot-interval-seconds "${FUNDING_PAPER_HOT_INTERVAL_SECONDS:-10}" \
  --status-report-interval-seconds "${FUNDING_PAPER_STATUS_REPORT_INTERVAL_SECONDS:-1800}" \
  --status-report-max-routes "${FUNDING_PAPER_STATUS_REPORT_MAX_ROUTES:-5}" \
  >> "$LOG_FILE" 2>&1 < /dev/null &

pid="$!"
disown "$pid" 2>/dev/null || true
echo "$pid" > "$PID_FILE"

echo "Funding paper trader started: PID $pid"
echo "PID file: $PID_FILE"
echo "Log: $LOG_FILE"
