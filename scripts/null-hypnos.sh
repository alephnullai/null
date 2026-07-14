#!/usr/bin/env bash
# null-hypnos: nightly memory maintenance (sleep cycle)
# Runs at 2am via cron
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/petecopeland/anaconda3/bin:$PATH"
cd /Users/petecopeland/Repos/null
python -m null_memory.cli hypnos run >> /tmp/null-hypnos.log 2>&1
