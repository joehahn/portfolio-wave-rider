#!/usr/bin/env bash
# Daily snapshot + dashboard refresh, intended to be run by cron Mon-Fri 16:30 local.
# Resolves its own location so the user doesn't have to edit PROJ by hand:
# install with one line in crontab, no project-path interpolation required.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] snapshot start"
  .venv/bin/python -m src.cli snapshot
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] dashboard start"
  .venv/bin/python -m src.cli dashboard
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> data/snapshot.log 2>&1
