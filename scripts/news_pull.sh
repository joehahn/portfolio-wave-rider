#!/usr/bin/env bash
# Forward news pull into the frozen corpus (data/forward_corpus/). Runs EVERY day (incl. weekends).
# Each run fetches recent news via WebSearch (not a fixed window) and dedups; only genuinely new articles
# are stored. Running every day so news is frozen NEAR publication; a weekday-only pull would capture weekend news
# 1-2 days late. Independent of the weekday snapshot job. Tolerant on failure, appends to snapshot.log.
# Installed by scripts/install_cron.sh; resolves its own location so no path interpolation is needed.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] news pull start"
  .venv/bin/python -m src.cli pull-news || echo "[$(date '+%Y-%m-%d %H:%M:%S')] pull-news failed (tolerated)"
  # Refresh the Retriever Bootstrap (RBS) dashboard off the just-updated forward corpus (render-only, no LLM).
  # Lives here (not the 16:30 snapshot job) because RBS visualizes the news corpus this job just wrote.
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] RBS refresh start"
  .venv/bin/python scripts/build_bootstrap_dashboard.py || echo "[$(date '+%Y-%m-%d %H:%M:%S')] RBS refresh failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] news pull done"
} >> data/snapshot.log 2>&1
