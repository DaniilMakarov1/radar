#!/usr/bin/env zsh
set -euo pipefail

# Local Whisper ASR shim for the Qwen Code Telegram channel voice messages.
# Transcribes inbound voice offline (free) and serves the qwen3-asr-flash
# contract on a localhost OpenAI-compatible endpoint.
#
# NOTE: launchd agents cannot read ~/Desktop (macOS TCC), so this service runs
# entirely from ~/.local/share/qwen-asr. The install script deploys the latest
# asr_shim.py + keyterms there from the repo.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

DEPLOY="$HOME/.local/share/qwen-asr"
cd "$DEPLOY"

export ASR_HOST="${ASR_HOST:-127.0.0.1}"
export ASR_PORT="${ASR_PORT:-8790}"
export ASR_MODEL="${ASR_MODEL:-mlx-community/whisper-large-v3-turbo}"
export ASR_KEYTERMS_FILE="${ASR_KEYTERMS_FILE:-$DEPLOY/voice-keyterms.txt}"

exec "$DEPLOY/venv/bin/python" "$DEPLOY/asr_shim.py"
