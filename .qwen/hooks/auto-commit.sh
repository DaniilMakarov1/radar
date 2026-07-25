#!/usr/bin/env bash
# Auto-commit hook: stages and commits the modified file after write_file/edit.
# Receives hook JSON on stdin with tool_input.file_path.
set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
ti = data.get('tool_input') or {}
print(ti.get('file_path') or '')
" 2>/dev/null || true)

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  exit 0
fi

cd "$(git -C "$(dirname "$FILE")" rev-parse --show-toplevel 2>/dev/null)" || exit 0

# Skip if file is not tracked and not in the repo
git ls-files --error-unmatch "$FILE" >/dev/null 2>&1 || git add "$FILE"

# Skip if nothing to commit for this file
if git diff --quiet -- "$FILE" && git diff --cached --quiet -- "$FILE"; then
  exit 0
fi

git add "$FILE"

# Build a short commit message from the file path
REL=$(python3 -c "import os; print(os.path.relpath('$FILE', '$(pwd)'))" 2>/dev/null || basename "$FILE")
TOOL=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_name','edit'))" 2>/dev/null || echo "edit")

git commit -m "auto: ${TOOL} ${REL}" --no-verify >/dev/null 2>&1 || true
