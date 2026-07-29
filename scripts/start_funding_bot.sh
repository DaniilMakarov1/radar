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
SCREEN_NAME="${FUNDING_PAPER_SCREEN_NAME:-radar-funding-bot}"
FUNDING_BOT_PROCESS_PATTERN='(^|/)(Python|python[0-9.]*)[[:space:]]+-m smart_money_radar[.]cli funding-paper-trader'

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.env"
  set +a
fi

funding_telegram_enabled="${FUNDING_PAPER_TELEGRAM_ENABLED:-}"
if [[ -z "$funding_telegram_enabled" ]] \
  && [[ -n "${FUNDING_TELEGRAM_BOT_TOKEN:-}" ]] \
  && [[ -n "${FUNDING_TELEGRAM_CHAT_ID:-}" ]]; then
  funding_telegram_enabled=1
fi
funding_telegram_arg=()
if [[ "$funding_telegram_enabled" != "1" ]]; then
  funding_telegram_arg=(--no-telegram)
fi

mkdir -p "$LOG_DIR"

funding_bot_pid() {
  ps ax -o pid=,comm=,command= \
    | awk -v pattern="$FUNDING_BOT_PROCESS_PATTERN" '$0 ~ pattern && !found {print $1; found=1}'
}

if [[ -f "$PLIST" ]]; then
  launchctl bootstrap "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  pid="$(funding_bot_pid)"
  if [[ -z "$pid" ]]; then
    launchctl kickstart "gui/$UID_VALUE/$LABEL"
    sleep 1
    pid="$(funding_bot_pid)"
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
  running_pid="$(funding_bot_pid)"
  if [[ -n "$existing_pid" ]] && [[ "$existing_pid" == "$running_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Funding paper trader is already running: PID $existing_pid"
    echo "Log: $LOG_FILE"
    exit 0
  fi
fi

existing_pid="$(funding_bot_pid)"
if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
  echo "$existing_pid" > "$PID_FILE"
  echo "Funding paper trader is already running: PID $existing_pid"
  echo "PID file refreshed: $PID_FILE"
  echo "Log: $LOG_FILE"
  exit 0
fi

cd "$ROOT_DIR"
if command -v screen >/dev/null 2>&1; then
  screen_listing="$(screen -ls 2>/dev/null || true)"
  if [[ "$screen_listing" == *".${SCREEN_NAME}"* ]]; then
    echo "Funding paper trader screen already exists: $SCREEN_NAME"
    echo "No python process is visible yet; check with: screen -r $SCREEN_NAME"
    echo "Log: $LOG_FILE"
    exit 0
  fi
  screen -dmS "$SCREEN_NAME" /bin/zsh -lc '
    cd "$1"
    telegram_arg=""
    funding_telegram_enabled="${FUNDING_PAPER_TELEGRAM_ENABLED:-}"
    if [[ -z "$funding_telegram_enabled" ]] \
      && [[ -n "${FUNDING_TELEGRAM_BOT_TOKEN:-}" ]] \
      && [[ -n "${FUNDING_TELEGRAM_CHAT_ID:-}" ]]; then
      funding_telegram_enabled=1
    fi
    if [[ "$funding_telegram_enabled" != "1" ]]; then
      telegram_arg="--no-telegram"
    fi
    exec python3 -m smart_money_radar.cli funding-paper-trader \
      --target-notional "${FUNDING_PAPER_TARGET_NOTIONAL:-500}" \
      --entry-min-lead-seconds "${FUNDING_PAPER_ENTRY_MIN_LEAD_SECONDS:-25}" \
      --entry-max-lead-seconds "${FUNDING_PAPER_ENTRY_MAX_LEAD_SECONDS:-35}" \
      --arm-window-seconds "${FUNDING_PAPER_ARM_WINDOW_SECONDS:-120}" \
      --final-recheck-freeze-seconds "${FUNDING_PAPER_FINAL_RECHECK_FREEZE_SECONDS:-0}" \
      --max-entry-snapshot-age-seconds "${FUNDING_PAPER_MAX_ENTRY_SNAPSHOT_AGE_SECONDS:-2}" \
      --max-settlement-publication-lag-seconds "${FUNDING_PAPER_MAX_SETTLEMENT_PUBLICATION_LAG_SECONDS:-300}" \
      --min-live-net-profit "${FUNDING_PAPER_MIN_LIVE_NET_PROFIT:-0}" \
      --scan-interval-seconds "${FUNDING_PAPER_SCAN_INTERVAL_SECONDS:-300}" \
      --monitor-interval-seconds "${FUNDING_PAPER_MONITOR_INTERVAL_SECONDS:-2}" \
      --hot-interval-seconds "${FUNDING_PAPER_HOT_INTERVAL_SECONDS:-1}" \
      --hot-route-recheck-workers "${FUNDING_PAPER_HOT_ROUTE_RECHECK_WORKERS:-6}" \
      --lightweight-foreground-budget-seconds "${FUNDING_PAPER_LIGHTWEIGHT_FOREGROUND_BUDGET_SECONDS:-8}" \
      --lightweight-cache-ttl-seconds "${FUNDING_PAPER_LIGHTWEIGHT_CACHE_TTL_SECONDS:-180}" \
      --lightweight-route-horizon-seconds "${FUNDING_PAPER_LIGHTWEIGHT_ROUTE_HORIZON_SECONDS:-3600}" \
      --lightweight-watch-window-seconds "${FUNDING_PAPER_LIGHTWEIGHT_WATCH_WINDOW_SECONDS:-600}" \
      --status-report-interval-seconds "${FUNDING_PAPER_STATUS_REPORT_INTERVAL_SECONDS:-900}" \
      --status-report-max-routes "${FUNDING_PAPER_STATUS_REPORT_MAX_ROUTES:-5}" \
      --strategy-set "${FUNDING_PAPER_STRATEGY_SET:-synchronized_funding_capture}" \
      --common-price-move-alert-pct "${FUNDING_PAPER_COMMON_PRICE_MOVE_ALERT_PCT:-5}" \
      --common-price-move-critical-pct "${FUNDING_PAPER_COMMON_PRICE_MOVE_CRITICAL_PCT:-10}" \
      ${telegram_arg} \
      >> "$2" 2>&1
  ' _ "$ROOT_DIR" "$LOG_FILE"
  sleep 1
  pid="$(funding_bot_pid)"
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$PID_FILE"
    echo "Funding paper trader started in screen: $SCREEN_NAME"
    echo "PID: $pid"
  else
    echo "Funding paper trader screen started: $SCREEN_NAME"
    echo "PID is not visible yet; check status again in a few seconds."
  fi
  echo "PID file: $PID_FILE"
  echo "Log: $LOG_FILE"
  exit 0
fi

nohup python3 -m smart_money_radar.cli funding-paper-trader \
  --target-notional "${FUNDING_PAPER_TARGET_NOTIONAL:-500}" \
  --entry-min-lead-seconds "${FUNDING_PAPER_ENTRY_MIN_LEAD_SECONDS:-25}" \
  --entry-max-lead-seconds "${FUNDING_PAPER_ENTRY_MAX_LEAD_SECONDS:-35}" \
  --arm-window-seconds "${FUNDING_PAPER_ARM_WINDOW_SECONDS:-120}" \
  --final-recheck-freeze-seconds "${FUNDING_PAPER_FINAL_RECHECK_FREEZE_SECONDS:-0}" \
  --max-entry-snapshot-age-seconds "${FUNDING_PAPER_MAX_ENTRY_SNAPSHOT_AGE_SECONDS:-2}" \
  --max-settlement-publication-lag-seconds "${FUNDING_PAPER_MAX_SETTLEMENT_PUBLICATION_LAG_SECONDS:-300}" \
  --min-live-net-profit "${FUNDING_PAPER_MIN_LIVE_NET_PROFIT:-0}" \
  --scan-interval-seconds "${FUNDING_PAPER_SCAN_INTERVAL_SECONDS:-300}" \
  --monitor-interval-seconds "${FUNDING_PAPER_MONITOR_INTERVAL_SECONDS:-2}" \
  --hot-interval-seconds "${FUNDING_PAPER_HOT_INTERVAL_SECONDS:-1}" \
  --hot-route-recheck-workers "${FUNDING_PAPER_HOT_ROUTE_RECHECK_WORKERS:-6}" \
  --lightweight-foreground-budget-seconds "${FUNDING_PAPER_LIGHTWEIGHT_FOREGROUND_BUDGET_SECONDS:-8}" \
  --lightweight-cache-ttl-seconds "${FUNDING_PAPER_LIGHTWEIGHT_CACHE_TTL_SECONDS:-180}" \
  --lightweight-route-horizon-seconds "${FUNDING_PAPER_LIGHTWEIGHT_ROUTE_HORIZON_SECONDS:-3600}" \
  --lightweight-watch-window-seconds "${FUNDING_PAPER_LIGHTWEIGHT_WATCH_WINDOW_SECONDS:-600}" \
  --status-report-interval-seconds "${FUNDING_PAPER_STATUS_REPORT_INTERVAL_SECONDS:-900}" \
  --status-report-max-routes "${FUNDING_PAPER_STATUS_REPORT_MAX_ROUTES:-5}" \
  --strategy-set "${FUNDING_PAPER_STRATEGY_SET:-synchronized_funding_capture}" \
  --common-price-move-alert-pct "${FUNDING_PAPER_COMMON_PRICE_MOVE_ALERT_PCT:-5}" \
  --common-price-move-critical-pct "${FUNDING_PAPER_COMMON_PRICE_MOVE_CRITICAL_PCT:-10}" \
  "${funding_telegram_arg[@]}" \
  >> "$LOG_FILE" 2>&1 < /dev/null &

pid="$!"
disown "$pid" 2>/dev/null || true
echo "$pid" > "$PID_FILE"

echo "Funding paper trader started: PID $pid"
echo "PID file: $PID_FILE"
echo "Log: $LOG_FILE"
