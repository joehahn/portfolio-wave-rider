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
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] news pull start"
  # Forward corpus ingest. Tolerated on failure so a WebSearch hiccup never blocks the snapshot/dashboard.
  .venv/bin/python -m src.cli pull-news || echo "[$(date '+%Y-%m-%d %H:%M:%S')] pull-news failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review (if due) start"
  # Self-gating rebalance: --if-due only curates once per rebalance_period, so daily is safe and it
  # catches up a missed period on the next wake. Recommendation-only; never trades. Tolerated on failure.
  .venv/bin/python -m src.cli review --if-due || echo "[$(date '+%Y-%m-%d %H:%M:%S')] review failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> data/snapshot.log 2>&1
