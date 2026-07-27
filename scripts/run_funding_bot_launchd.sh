#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_DIR="${RADAR_ROOT_DIR:-$SCRIPT_ROOT}"
RUNTIME_DIR="${RADAR_RUNTIME_DIR:-$ROOT_DIR}"
PYTHON_BIN="${FUNDING_PAPER_PYTHON:-/opt/homebrew/bin/python3}"

if [[ -f "$RUNTIME_DIR/.env" ]]; then
  set -a
  source "$RUNTIME_DIR/.env"
  set +a
elif [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
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

cd "$ROOT_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting funding paper trader"
exec "$PYTHON_BIN" -u -m smart_money_radar.cli funding-paper-trader \
  --target-notional "${FUNDING_PAPER_TARGET_NOTIONAL:-500}" \
  --entry-min-lead-seconds "${FUNDING_PAPER_ENTRY_MIN_LEAD_SECONDS:-0}" \
  --entry-max-lead-seconds "${FUNDING_PAPER_ENTRY_MAX_LEAD_SECONDS:-15}" \
  --arm-window-seconds "${FUNDING_PAPER_ARM_WINDOW_SECONDS:-900}" \
  --final-recheck-freeze-seconds "${FUNDING_PAPER_FINAL_RECHECK_FREEZE_SECONDS:-15}" \
  --max-entry-snapshot-age-seconds "${FUNDING_PAPER_MAX_ENTRY_SNAPSHOT_AGE_SECONDS:-30}" \
  --max-settlement-publication-lag-seconds "${FUNDING_PAPER_MAX_SETTLEMENT_PUBLICATION_LAG_SECONDS:-300}" \
  --min-live-net-profit "${FUNDING_PAPER_MIN_LIVE_NET_PROFIT:-0}" \
  --scan-interval-seconds "${FUNDING_PAPER_SCAN_INTERVAL_SECONDS:-300}" \
  --monitor-interval-seconds "${FUNDING_PAPER_MONITOR_INTERVAL_SECONDS:-120}" \
  --hot-interval-seconds "${FUNDING_PAPER_HOT_INTERVAL_SECONDS:-10}" \
  --hot-route-recheck-workers "${FUNDING_PAPER_HOT_ROUTE_RECHECK_WORKERS:-6}" \
  --status-report-interval-seconds "${FUNDING_PAPER_STATUS_REPORT_INTERVAL_SECONDS:-900}" \
  --status-report-max-routes "${FUNDING_PAPER_STATUS_REPORT_MAX_ROUTES:-5}" \
  --strategy-set "${FUNDING_PAPER_STRATEGY_SET:-funding_only,combined}" \
  --price-stop-loss-pct "${FUNDING_PAPER_PRICE_STOP_LOSS_PCT:-10}" \
  "${funding_telegram_arg[@]}"
