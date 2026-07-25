#!/usr/bin/env bash
set -euo pipefail

# Deploys the qwen heartbeat and installs its launchd agent.
# Sends a short Telegram status every hour so the user knows
# the bot infrastructure is alive.

LABEL="com.smartmoneyradar.qwen-heartbeat"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO="$HOME/Desktop/radar"
DEPLOY="$HOME/.local/share/qwen-channel"

mkdir -p "$DEPLOY/logs"

cp "$REPO/scripts/qwen_heartbeat.py" "$DEPLOY/heartbeat.py"

# Source .env to get the bot token and chat id.
if [[ -f "$DEPLOY/.env" ]]; then
  set -a; source "$DEPLOY/.env" 2>/dev/null || true; set +a
elif [[ -f "$REPO/.env" ]]; then
  set -a; source "$REPO/.env" 2>/dev/null || true; set +a
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>${DEPLOY}/heartbeat.py</string>
  </array>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DEPLOY}/logs/heartbeat.log</string>
  <key>StandardErrorPath</key>
  <string>${DEPLOY}/logs/heartbeat.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>QWEN_CHANNEL_TELEGRAM_TOKEN</key>
    <string>${QWEN_CHANNEL_TELEGRAM_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}</string>
    <key>TELEGRAM_CHAT_ID</key>
    <string>${TELEGRAM_CHAT_ID:-}</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✅ ${LABEL} deployed (every 3600s / 1 hour)"
echo "   Script: ${DEPLOY}/heartbeat.py"
echo "   Logs:   ${DEPLOY}/logs/heartbeat.log"
