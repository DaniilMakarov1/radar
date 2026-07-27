#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.smartmoneyradar.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DEPLOY="$HOME/.local/share/smartmoneyradar"
LOG_DIR="$DEPLOY/logs"
UID_VALUE="$(id -u)"
RUNNER="$DEPLOY/run_dashboard.sh"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cp "$ROOT_DIR/scripts/run_dashboard_launchd.sh" "$RUNNER"
chmod +x "$RUNNER"

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
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/dashboard.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/dashboard.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$UID_VALUE" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"
sleep 1

pid="$(ps ax -o pid=,command= | awk '/[p]ython.*smart_money_radar[.]dashboard/{print $1; exit}')"
echo "Installed and started $LABEL"
echo "Plist: $PLIST"
echo "Runner: $RUNNER"
[[ -n "${pid:-}" ]] && echo "PID: $pid"
echo "Logs:"
echo "  $LOG_DIR/dashboard.out.log"
echo "  $LOG_DIR/dashboard.err.log"
