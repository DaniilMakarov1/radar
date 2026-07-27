#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.smartmoneyradar.funding-paper-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PID_FILE="$ROOT_DIR/.funding-paper-trader.pid"
PYTHON_BIN="${FUNDING_PAPER_PYTHON:-$(command -v python3)}"
DEPLOY="$HOME/.local/share/smartmoneyradar"
LOG_DIR="$DEPLOY/logs"
UID_VALUE="$(id -u)"
RUNNER="$DEPLOY/run_funding_bot.sh"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cp "$ROOT_DIR/scripts/run_funding_bot_launchd.sh" "$RUNNER"
cp "$ROOT_DIR/.env" "$DEPLOY/.env" 2>/dev/null || true
chmod +x "$RUNNER"
chmod 600 "$DEPLOY/.env" 2>/dev/null || true

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>WorkingDirectory</key>
  <string>$DEPLOY</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$RUNNER</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>RADAR_ROOT_DIR</key>
    <string>$ROOT_DIR</string>
    <key>RADAR_RUNTIME_DIR</key>
    <string>$DEPLOY</string>
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
echo "Runner: $RUNNER"
echo "Python: $PYTHON_BIN"
[[ -n "${pid:-}" ]] && echo "PID: $pid"
echo "Logs:"
echo "  $LOG_DIR/funding-paper-trader.out.log"
echo "  $LOG_DIR/funding-paper-trader.err.log"
