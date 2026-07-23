#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.prediction-radar-bot.pid"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/prediction-radar-bot.log"
LABEL="com.smartmoneyradar.prediction-radar-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

SCREEN_NAME="${PREDICTION_BOT_SCREEN_NAME:-radar-prediction-bot}"
PYTHON_BIN="${PREDICTION_BOT_PYTHON:-$(command -v python3)}"
PROCESS_RE='[p]ython.*-m smart_money_radar([.]prediction[.]bot|[.]cli prediction-bot)'

mkdir -p "$LOG_DIR"

if [[ -f "$PLIST" ]]; then
  launchctl bootstrap "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  pid="$(ps ax -o pid=,command= | awk -v re="$PROCESS_RE" '$0 ~ re {print $1; exit}')"
  if [[ -z "$pid" ]]; then
    launchctl kickstart "gui/$UID_VALUE/$LABEL"
    sleep 1
    pid="$(ps ax -o pid=,command= | awk -v re="$PROCESS_RE" '$0 ~ re {print $1; exit}')"
  fi
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$PID_FILE"
    echo "Prediction Radar bot is running by launchd: PID $pid"
  else
    echo "Prediction Radar bot launchd job was kicked, but PID is not visible yet."
  fi
  echo "LaunchAgent: $PLIST"
  echo "Logs:"
  echo "  $LOG_DIR/prediction-radar-bot.out.log"
  echo "  $LOG_DIR/prediction-radar-bot.err.log"
  exit 0
fi

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Prediction Radar bot is already running: PID $existing_pid"
    echo "Log: $LOG_FILE"
    exit 0
  fi
fi

existing_pid="$(ps ax -o pid=,command= | awk -v re="$PROCESS_RE" '$0 ~ re {print $1; exit}')"
if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
  echo "$existing_pid" > "$PID_FILE"
  echo "Prediction Radar bot is already running: PID $existing_pid"
  echo "PID file refreshed: $PID_FILE"
  echo "Log: $LOG_FILE"
  exit 0
fi

cd "$ROOT_DIR"
if command -v screen >/dev/null 2>&1; then
  screen -dmS "$SCREEN_NAME" bash -lc '
    cd "$1"
    exec "$3" -m smart_money_radar.prediction.bot \
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
      --paper-depth-haircut "${PREDICTION_BOT_PAPER_DEPTH_HAIRCUT:-0.8}" \
      >> "$2" 2>&1
  ' _ "$ROOT_DIR" "$LOG_FILE" "$PYTHON_BIN"
  sleep 1
  pid="$(ps ax -o pid=,command= | awk -v re="$PROCESS_RE" '$0 ~ re {print $1; exit}')"
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$PID_FILE"
    echo "Prediction Radar bot started in screen: PID $pid"
  else
    echo "Prediction Radar bot screen session started, but PID is not visible yet."
  fi
  echo "Screen: $SCREEN_NAME"
  echo "Python: $PYTHON_BIN"
  echo "PID file: $PID_FILE"
  echo "Log: $LOG_FILE"
  exit 0
fi

nohup "$PYTHON_BIN" -m smart_money_radar.prediction.bot \
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
  --paper-depth-haircut "${PREDICTION_BOT_PAPER_DEPTH_HAIRCUT:-0.8}" \
  >> "$LOG_FILE" 2>&1 < /dev/null &

pid="$!"
disown "$pid" 2>/dev/null || true
echo "$pid" > "$PID_FILE"

echo "Prediction Radar bot started: PID $pid"
echo "Python: $PYTHON_BIN"
echo "PID file: $PID_FILE"
echo "Log: $LOG_FILE"
