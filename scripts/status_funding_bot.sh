#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.smartmoneyradar.funding-paper-trader"
PID_FILE="$ROOT_DIR/.funding-paper-trader.pid"
UID_VALUE="$(id -u)"
FUNDING_BOT_PROCESS_PATTERN='(^|/)(Python|python[0-9.]*)[[:space:]]+-m smart_money_radar[.]cli funding-paper-trader'

echo "Processes:"
ps ax -o pid=,ppid=,etime=,comm=,command= \
  | awk -v pattern="$FUNDING_BOT_PROCESS_PATTERN" '$0 ~ pattern {print}'

echo
echo "screen:"
screen_listing="$(screen -ls 2>/dev/null || true)"
if [[ -n "$screen_listing" ]]; then
  printf "%s\n" "$screen_listing" | sed -n '1,80p'
else
  echo "screen unavailable"
fi

if [[ -f "$PID_FILE" ]]; then
  echo
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "PID file: $pid"
  else
    echo "PID file: ${pid:-empty} (stale)"
  fi
fi

echo
echo "launchd:"
launchctl print "gui/$UID_VALUE/$LABEL" 2>/dev/null | sed -n '1,80p' || echo "not installed"

echo
echo "Latest paper events:"
cd "$ROOT_DIR"
python3 - <<'PY'
import json
import sqlite3

conn = sqlite3.connect("data/radar.sqlite")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
for row in cur.execute(
    """
    SELECT funding_paper_event_id, event_type, severity, created_at, message, payload_json
    FROM funding_paper_events
    ORDER BY funding_paper_event_id DESC
    LIMIT 6
    """
):
    payload = json.loads(row["payload_json"] or "{}")
    compact = {
        key: payload.get(key)
        for key in (
            "mode",
            "funding_scan_id",
            "candidate_count",
            "watch_count",
            "opened_count",
            "closed_count",
            "hot_route_count",
            "reason",
            "error_type",
        )
        if key in payload
    }
    print(
        row["funding_paper_event_id"],
        row["event_type"],
        row["severity"],
        row["created_at"],
        row["message"],
        compact,
    )
PY
