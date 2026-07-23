#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.funding-paper-trader.pid"
PATTERN="smart_money_radar.cli funding-paper-trader"
LABEL="com.smartmoneyradar.funding-paper-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"
SCREEN_NAME="${FUNDING_PAPER_SCREEN_NAME:-radar-funding-bot}"
FUNDING_BOT_PROCESS_PATTERN='(^|/)(Python|python[0-9.]*)[[:space:]]+-m smart_money_radar[.]cli funding-paper-trader'

funding_bot_pids() {
  ps ax -o pid=,comm=,command= \
    | awk -v pattern="$FUNDING_BOT_PROCESS_PATTERN" '$0 ~ pattern {print $1}'
}

stop_funding_screen() {
  command -v screen >/dev/null 2>&1 || return 0
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  while IFS= read -r session; do
    [[ -n "$session" ]] && screen -S "$session" -X quit >/dev/null 2>&1 || true
  done < <(
    screen -ls 2>/dev/null \
      | awk -v suffix=".$SCREEN_NAME" '$1 ~ suffix {print $1}'
  )
}

if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  stop_funding_screen
  rm -f "$PID_FILE"
  echo "Funding paper trader LaunchAgent stopped."
  exit 0
fi

pids=()
while IFS= read -r pid; do
  [[ -n "$pid" ]] && pids+=("$pid")
done < <(funding_bot_pids)

if [[ "${#pids[@]}" -eq 0 ]]; then
  stop_funding_screen
  rm -f "$PID_FILE"
  echo "Funding paper trader is not running."
  exit 0
fi

for pid in "${pids[@]}"; do
  echo "Stopping Funding paper trader: PID $pid"
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
  echo "Use kill -9 only as a last resort; it cannot send Telegram shutdown notices."
  exit 1
fi

rm -f "$PID_FILE"
stop_funding_screen
echo "Funding paper trader stopped."
