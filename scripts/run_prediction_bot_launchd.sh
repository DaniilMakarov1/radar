#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

PYTHON_BIN="${PREDICTION_BOT_PYTHON:-/opt/homebrew/bin/python3}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting Prediction Radar bot"
exec "$PYTHON_BIN" -u -m smart_money_radar.prediction.bot \
  --scan-interval-seconds "${PREDICTION_BOT_SCAN_INTERVAL_SECONDS:-300}" \
  --status-report-interval-seconds "${PREDICTION_BOT_STATUS_REPORT_INTERVAL_SECONDS:-3600}" \
  --status-report-max-routes "${PREDICTION_BOT_STATUS_REPORT_MAX_ROUTES:-5}" \
  --events-per-venue "${PREDICTION_BOT_EVENTS_PER_VENUE:-75}" \
  --max-markets-per-venue "${PREDICTION_BOT_MAX_MARKETS_PER_VENUE:-1500}" \
  --kalshi-market-pages "${PREDICTION_BOT_KALSHI_MARKET_PAGES:-10}" \
  --http-timeout-seconds "${PREDICTION_BOT_HTTP_TIMEOUT_SECONDS:-8}" \
  --http-max-retries "${PREDICTION_BOT_HTTP_MAX_RETRIES:-1}" \
  --paper-size "${PREDICTION_BOT_PAPER_SIZE:-100}" \
  --paper-latency-ms "${PREDICTION_BOT_PAPER_LATENCY_MS:-750}" \
  --paper-depth-haircut "${PREDICTION_BOT_PAPER_DEPTH_HAIRCUT:-0.8}"
