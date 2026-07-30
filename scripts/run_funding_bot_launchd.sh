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
  --entry-min-lead-seconds "${FUNDING_PAPER_ENTRY_MIN_LEAD_SECONDS:-25}" \
  --entry-max-lead-seconds "${FUNDING_PAPER_ENTRY_MAX_LEAD_SECONDS:-35}" \
  --arm-window-seconds "${FUNDING_PAPER_ARM_WINDOW_SECONDS:-120}" \
  --final-recheck-freeze-seconds "${FUNDING_PAPER_FINAL_RECHECK_FREEZE_SECONDS:-0}" \
  --max-entry-snapshot-age-seconds "${FUNDING_PAPER_MAX_ENTRY_SNAPSHOT_AGE_SECONDS:-5}" \
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
  --account-fee-evidence-max-age-seconds "${FUNDING_PAPER_ACCOUNT_FEE_EVIDENCE_MAX_AGE_SECONDS:-86400}" \
  --public-fee-endpoint-max-age-seconds "${FUNDING_PAPER_PUBLIC_FEE_ENDPOINT_MAX_AGE_SECONDS:-604800}" \
  --reviewed-static-fee-max-age-seconds "${FUNDING_PAPER_REVIEWED_STATIC_FEE_MAX_AGE_SECONDS:-2592000}" \
  "${funding_telegram_arg[@]}"
