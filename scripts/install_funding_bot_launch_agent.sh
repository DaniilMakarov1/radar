#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.smartmoneyradar.funding-paper-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PID_FILE="$ROOT_DIR/.funding-paper-trader.pid"
PYTHON_BIN="${FUNDING_PAPER_PYTHON:-$(command -v python3)}"
LOG_DIR="$ROOT_DIR/logs"
UID_VALUE="$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>-m</string>
    <string>smart_money_radar.cli</string>
    <string>funding-paper-trader</string>
    <string>--target-notional</string>
    <string>${FUNDING_PAPER_TARGET_NOTIONAL:-500}</string>
    <string>--entry-window-seconds</string>
    <string>${FUNDING_PAPER_ENTRY_WINDOW_SECONDS:-180}</string>
    <string>--entry-min-lead-seconds</string>
    <string>${FUNDING_PAPER_ENTRY_MIN_LEAD_SECONDS:-30}</string>
    <string>--entry-max-lead-seconds</string>
    <string>${FUNDING_PAPER_ENTRY_MAX_LEAD_SECONDS:-60}</string>
    <string>--arm-window-seconds</string>
    <string>${FUNDING_PAPER_ARM_WINDOW_SECONDS:-900}</string>
    <string>--max-settlement-publication-lag-seconds</string>
    <string>${FUNDING_PAPER_MAX_SETTLEMENT_PUBLICATION_LAG_SECONDS:-300}</string>
    <string>--min-live-net-profit</string>
    <string>${FUNDING_PAPER_MIN_LIVE_NET_PROFIT:-0}</string>
    <string>--scan-interval-seconds</string>
    <string>${FUNDING_PAPER_SCAN_INTERVAL_SECONDS:-60}</string>
    <string>--hot-interval-seconds</string>
    <string>${FUNDING_PAPER_HOT_INTERVAL_SECONDS:-10}</string>
    <string>--status-report-interval-seconds</string>
    <string>${FUNDING_PAPER_STATUS_REPORT_INTERVAL_SECONDS:-1800}</string>
    <string>--status-report-max-routes</string>
    <string>${FUNDING_PAPER_STATUS_REPORT_MAX_ROUTES:-5}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/funding-paper-trader.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/funding-paper-trader.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"
sleep 1
pid="$(ps ax -o pid=,command= | awk '/[p]ython.*-m smart_money_radar[.]cli funding-paper-trader/{print $1; exit}')"
if [[ -n "$pid" ]]; then
  echo "$pid" > "$PID_FILE"
fi

echo "Installed and started $LABEL"
echo "Plist: $PLIST"
echo "Python: $PYTHON_BIN"
[[ -n "${pid:-}" ]] && echo "PID: $pid"
echo "Logs:"
echo "  $LOG_DIR/funding-paper-trader.out.log"
echo "  $LOG_DIR/funding-paper-trader.err.log"
