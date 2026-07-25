#!/usr/bin/env bash
set -euo pipefail

# Deploys the qwen-channel runner and (re)installs its launchd agent.
#
# macOS TCC prevents launchd agents from reading ~/Desktop, so the runtime
# copy of the runner, the .env, and the logs all live under
# ~/.local/share/qwen-channel. Re-run this script after editing
# scripts/run_qwen_channel_launchd.sh or .env to redeploy.

LABEL="com.smartmoneyradar.qwen-channel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO="$HOME/Desktop/radar"
DEPLOY="$HOME/.local/share/qwen-channel"

mkdir -p "$DEPLOY/logs"

# Deploy the latest runner + .env from the repo into the launchd-readable dir.
cp "$REPO/scripts/run_qwen_channel_launchd.sh" "$DEPLOY/run.sh"
cp "$REPO/.env" "$DEPLOY/.env" 2>/dev/null || true
chmod +x "$DEPLOY/run.sh"

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
    <string>/bin/zsh</string>
    <string>${DEPLOY}/run.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$HOME</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DEPLOY}/logs/qwen-channel.log</string>
  <key>StandardErrorPath</key>
  <string>${DEPLOY}/logs/qwen-channel.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PATH</key>
    <string>/Users/daniilmakarov/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✅ ${LABEL} deployed and started"
echo "   Runner:  ${DEPLOY}/run.sh"
echo "   Logs:    ${DEPLOY}/logs/qwen-channel.log"
