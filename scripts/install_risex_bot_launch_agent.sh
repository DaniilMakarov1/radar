#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.smartmoneyradar.risex-paper-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_BIN="${RISEX_PYTHON:-$(command -v python3)}"
DEPLOY="$HOME/.local/share/smartmoneyradar"
LOG_DIR="$DEPLOY/logs"
UID_VALUE="$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Deploy runner + .env to TCC-safe location
cp "$ROOT_DIR/scripts/run_risex_bot_launchd.sh" "$DEPLOY/run_risex_bot.sh"
chmod +x "$DEPLOY/run_risex_bot.sh"

if [[ -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env" "$DEPLOY/.env"
fi

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
    <string>$DEPLOY/run_risex_bot.sh</string>
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
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/risex-paper-trader.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/risex-paper-trader.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$UID_VALUE/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"

echo "Installed and started $LABEL"
echo "Plist: $PLIST"
echo "Runner: $DEPLOY/run_risex_bot.sh"
echo "Python: $PYTHON_BIN"
echo "Logs:"
echo "  $LOG_DIR/risex-paper-trader.out.log"
echo "  $LOG_DIR/risex-paper-trader.err.log"
