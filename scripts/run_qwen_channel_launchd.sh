#!/usr/bin/env zsh
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PROJECT_DIR="$HOME/Desktop/radar"

# macOS TCC may block launchd agents from accessing ~/Desktop.
# Try to cd there, but fall back to $HOME so the process at least starts.
if cd "$PROJECT_DIR" 2>/dev/null; then
  :
else
  cd "$HOME"
fi

# Source .env — prefer the local TCC-safe copy, fall back to the project dir.
# TCC may block reading from ~/Desktop — tolerate the failure.
DEPLOY_DIR="$HOME/.local/share/qwen-channel"
for env_file in "$DEPLOY_DIR/.env" "$PROJECT_DIR/.env"; do
  if [[ -f "$env_file" ]]; then
    set -a
    source "$env_file" 2>/dev/null || true
    set +a
    break
  fi
done

export QWEN_CHANNEL_TELEGRAM_TOKEN="${QWEN_CHANNEL_TELEGRAM_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"

exec qwen channel start radar
