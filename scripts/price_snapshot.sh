#!/usr/bin/env bash
# Weekday (Mon-Fri 16:30 local) price snapshot + live dashboard + Curator Bootstrap (CBS) refresh.
# The biweekly curation review is a SEPARATE job (scripts/review_curation.sh, weekdays 19:00) so it
# runs AFTER the 18:30 news pull and reads same-day news; the news pull itself is scripts/news_pull.sh
# (every day 18:30). Resolves its own location.
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
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS refresh start"
  # Live-extend the Curator Bootstrap equity curve with today's prices. Render-only: re-replays the
  # FIXED bootstrap curation through the optimizer and re-renders -- no LLM call, no cost. Tolerated on failure.
  .venv/bin/python scripts/refresh_cbs.py || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS refresh failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> data/snapshot.log 2>&1
