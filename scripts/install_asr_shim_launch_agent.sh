#!/usr/bin/env bash
set -euo pipefail

# Deploys the local Whisper ASR shim and (re)installs its launchd agent.
#
# macOS TCC prevents launchd agents from reading ~/Desktop, so the runtime
# copy of the shim, its keyterms file, the runner script, and the logs all
# live under ~/.local/share/qwen-asr. Re-run this script after editing
# scripts/asr_shim.py or .qwen/voice-keyterms.txt to redeploy.

LABEL="com.smartmoneyradar.asr-shim"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO="$HOME/Desktop/radar"
DEPLOY="$HOME/.local/share/qwen-asr"

mkdir -p "$DEPLOY/logs"

# Deploy the latest code + keyterms from the repo into the launchd-readable dir.
cp "$REPO/scripts/asr_shim.py" "$DEPLOY/asr_shim.py"
cp "$REPO/scripts/run_asr_shim_launchd.sh" "$DEPLOY/run.sh"
cp "$REPO/.qwen/voice-keyterms.txt" "$DEPLOY/voice-keyterms.txt"
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
  <string>${DEPLOY}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DEPLOY}/logs/asr-shim.log</string>
  <key>StandardErrorPath</key>
  <string>${DEPLOY}/logs/asr-shim.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/daniilmakarov/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

UID_="$(id -u)"
launchctl bootout "gui/${UID_}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/${UID_}" "$PLIST"
echo "✅ ${LABEL} deployed and started"
echo "   Code:    ${DEPLOY}/asr_shim.py"
echo "   Logs:    ${DEPLOY}/logs/asr-shim.log"
echo "   Health:  curl http://127.0.0.1:8790/health"
