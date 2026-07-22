#!/usr/bin/env bash
# Weekday (Mon-Fri 16:30 local) price snapshot + dashboard + self-gating rebalance review.
# The news pull is a SEPARATE job (scripts/news_pull.sh, every day 18:30). The review here
# reads a trailing news window, so it does not depend on today's pull. Resolves its own location.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] snapshot start"
  # --force so this post-close run OVERWRITES any earlier same-day row (e.g. an intraday manual snapshot)
  # with the authoritative close, leaving exactly one row per date.
  .venv/bin/python -m src.cli snapshot --force
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] dashboard start"
  .venv/bin/python -m src.cli dashboard
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review (if due) start"
  # Self-gating rebalance: --if-due only curates once per rebalance_period, so daily is safe and it
  # catches up a missed period on the next wake. Recommendation-only; never trades. Tolerated on failure.
  .venv/bin/python -m src.cli review --if-due || echo "[$(date '+%Y-%m-%d %H:%M:%S')] review failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> data/snapshot.log 2>&1
