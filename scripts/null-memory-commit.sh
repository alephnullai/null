#!/usr/bin/env bash
# null-memory-commit: periodic git commit of ~/.null/ 
# Runs every 15 minutes via cron
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
NULL_DIR="$HOME/.null"
if [[ -d "$NULL_DIR/.git" ]]; then
  cd "$NULL_DIR"
  git add -A
  if ! git diff --cached --quiet; then
    git commit -m "auto: memory checkpoint $(date '+%Y-%m-%d %H:%M')"
    # Push if remote configured
    if git remote | grep -q .; then
      git push --quiet 2>/dev/null || true
    fi
  fi
fi
