#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.funding-paper-trader.pid"
PATTERN="smart_money_radar.cli funding-paper-trader"
LABEL="com.smartmoneyradar.funding-paper-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VALUE="$(id -u)"

if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "Funding paper trader LaunchAgent stopped."
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
  done < <(ps ax -o pid=,command= | awk '/[p]ython.*-m smart_money_radar[.]cli funding-paper-trader/{print $1}')
fi

if [[ "${#pids[@]}" -eq 0 ]]; then
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
echo "Funding paper trader stopped."
