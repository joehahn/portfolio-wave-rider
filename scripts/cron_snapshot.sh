#!/usr/bin/env bash
# Weekday (Mon-Fri 16:30 local) price snapshot + dashboard + self-gating rebalance review.
# The daily 7-day news pull is a SEPARATE job (scripts/cron_pull.sh at 16:00) so the corpus is fresh
# by the time the review runs here. Resolves its own location; no path interpolation required.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] snapshot start"
  .venv/bin/python -m src.cli snapshot
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] dashboard start"
  .venv/bin/python -m src.cli dashboard
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review (if due) start"
  # Self-gating rebalance: --if-due only curates once per rebalance_period, so daily is safe and it
  # catches up a missed period on the next wake. Recommendation-only; never trades. Tolerated on failure.
  .venv/bin/python -m src.cli review --if-due || echo "[$(date '+%Y-%m-%d %H:%M:%S')] review failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> data/snapshot.log 2>&1
