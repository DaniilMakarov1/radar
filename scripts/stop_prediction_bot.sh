#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.prediction-radar-bot.pid"
LABEL="com.smartmoneyradar.prediction-radar-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"
SCREEN_NAME="${PREDICTION_BOT_SCREEN_NAME:-radar-prediction-bot}"
PROCESS_RE='[p]ython.*-m smart_money_radar([.]prediction[.]bot|[.]cli prediction-bot)'

if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "Prediction Radar bot LaunchAgent stopped."
  exit 0
fi

pids=()
if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    pids+=("$pid")
  fi
fi

if [[ "${#pids[@]}" -eq 0 ]]; then
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(ps ax -o pid=,command= | awk -v re="$PROCESS_RE" '$0 ~ re {print $1}')
fi

if [[ "${#pids[@]}" -eq 0 ]]; then
  if command -v screen >/dev/null 2>&1; then
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  fi
  rm -f "$PID_FILE"
  echo "Prediction Radar bot is not running."
  exit 0
fi

for pid in "${pids[@]}"; do
  echo "Stopping Prediction Radar bot: PID $pid"
  kill -TERM "$pid" 2>/dev/null || true
done

deadline=$((SECONDS + 45))
while [[ "$SECONDS" -lt "$deadline" ]]; do
  alive=0
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      alive=1
      break
    fi
  done
  [[ "$alive" -eq 0 ]] && break
  sleep 1
done

still_alive=()
for pid in "${pids[@]}"; do
  if kill -0 "$pid" 2>/dev/null; then
    still_alive+=("$pid")
  fi
done

if [[ "${#still_alive[@]}" -gt 0 ]]; then
  echo "Still stopping or stuck: ${still_alive[*]}"
  echo "Use kill -9 only as a last resort; it cannot run graceful shutdown hooks."
  exit 1
fi

rm -f "$PID_FILE"
if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
fi
echo "Prediction Radar bot stopped."
