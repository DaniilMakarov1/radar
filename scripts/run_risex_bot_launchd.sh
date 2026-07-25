#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="/Users/daniilmakarov/Desktop/radar"
PYTHON_BIN="${RISEX_PYTHON:-/opt/homebrew/bin/python3}"

ENV_FILE="/Users/daniilmakarov/.local/share/smartmoneyradar/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$ROOT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

risex_telegram_enabled="${RISEX_TELEGRAM_ENABLED:-}"
if [[ -z "$risex_telegram_enabled" ]] \
  && [[ -n "${RISEX_TELEGRAM_BOT_TOKEN:-}" ]] \
  && [[ -n "${RISEX_TELEGRAM_CHAT_ID:-}" ]]; then
  risex_telegram_enabled=1
fi
risex_telegram_arg=()
if [[ "$risex_telegram_enabled" != "1" ]]; then
  risex_telegram_arg=(--no-telegram)
fi

cd "$ROOT_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting RiseX paper trader"
exec "$PYTHON_BIN" -u -m smart_money_radar.cli risex-bot \
  --balance "${RISEX_VENUE_BALANCE:-2000}" \
  --notional "${RISEX_TARGET_NOTIONAL:-500}" \
  --scan-interval "${RISEX_SCAN_INTERVAL:-180}" \
  --report-interval "${RISEX_REPORT_INTERVAL:-900}" \
  "${risex_telegram_arg[@]}"
