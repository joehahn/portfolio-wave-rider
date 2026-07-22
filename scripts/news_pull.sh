#!/usr/bin/env bash
# Daily (7-day, incl. weekends) forward news pull into the frozen corpus (data/forward_corpus/).
# Runs every day so news is frozen NEAR publication; a weekday-only pull would capture weekend news
# 1-2 days late. Independent of the weekday snapshot job. Tolerant on failure, appends to snapshot.log.
# Installed by scripts/install_cron.sh; resolves its own location so no path interpolation is needed.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] news pull start"
  .venv/bin/python -m src.cli pull-news || echo "[$(date '+%Y-%m-%d %H:%M:%S')] pull-news failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] news pull done"
} >> data/snapshot.log 2>&1
