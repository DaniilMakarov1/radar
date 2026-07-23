#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.smartmoneyradar.prediction-radar-bot"
PID_FILE="$ROOT_DIR/.prediction-radar-bot.pid"
UID_VALUE="$(id -u)"
SCREEN_NAME="${PREDICTION_BOT_SCREEN_NAME:-radar-prediction-bot}"
PYTHON_BIN="${PREDICTION_BOT_PYTHON:-$(command -v python3)}"
PROCESS_RE='[p]ython.*-m smart_money_radar([.]prediction[.]bot|[.]cli prediction-bot)'

echo "Processes:"
ps ax -o pid=,ppid=,etime=,command= | awk -v re="$PROCESS_RE" '$0 ~ re {print}'

if command -v screen >/dev/null 2>&1; then
  echo
  echo "screen:"
  screen -ls 2>/dev/null | awk -v name="$SCREEN_NAME" '$0 ~ name {print}' || true
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
echo "Latest prediction scans:"
cd "$ROOT_DIR"
"$PYTHON_BIN" - <<'PY'
import sqlite3
from pathlib import Path

from smart_money_radar.storage import SQLiteStore

SQLiteStore(Path("data/radar.sqlite")).init_db()

conn = sqlite3.connect("data/radar.sqlite")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
for row in cur.execute(
    """
    SELECT
        prediction_scan_id,
        status,
        started_at,
        finished_at,
        polymarket_event_count,
        kalshi_event_count,
        hyperliquid_event_count,
        hyperliquid_market_count,
        hyperliquid_orderbook_count,
        market_count,
        orderbook_count,
        route_count,
        executable_route_count,
        error
    FROM prediction_scans
    ORDER BY prediction_scan_id DESC
    LIMIT 5
    """
):
    scan_id = int(row["prediction_scan_id"])
    candidates = cur.execute(
        """
        SELECT COUNT(*)
        FROM prediction_route_candidates candidate
        LEFT JOIN prediction_routes route
          ON route.prediction_scan_id = candidate.prediction_scan_id
         AND route.route_key = candidate.route_key
        WHERE candidate.prediction_scan_id = ?
          AND COALESCE(candidate.expected_net_profit, 0) > 0
          AND (
              route.prediction_route_id IS NULL
              OR route.capital_lock_days IS NULL
              OR route.capital_lock_days > 0
          )
        """,
        (scan_id,),
    ).fetchone()[0]
    print(
        scan_id,
        row["status"],
        row["finished_at"] or row["started_at"],
        (
            f"events=poly:{row['polymarket_event_count']} "
            f"kalshi:{row['kalshi_event_count']} "
            f"hl:{row['hyperliquid_event_count']}"
        ),
        f"hl_markets={row['hyperliquid_market_count']}",
        f"hl_books={row['hyperliquid_orderbook_count']}",
        f"markets={row['market_count']}",
        f"books={row['orderbook_count']}",
        f"routes={row['route_count']}",
        f"exec={row['executable_route_count']}",
        f"candidates={candidates}",
        f"error={row['error'] or '-'}",
    )
PY
