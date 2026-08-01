#!/usr/bin/env bash
# Weekday (Mon-Fri 16:30 local) price snapshot + the two paper-portfolio dashboard refreshes: the Curator
# Bootstrap (CBS) and the Forwardtest (FT), which is now the site landing page (docs/index.html). Both
# refreshes live in THIS job, after the snapshot, because they replay against the same-day prices and must
# run after it (a separate cron line could fire before the snapshot finishes). The old real-holdings
# dashboard (`cli dashboard`) is RETIRED: FT is the recommendation of record and real money follows it;
# `snapshots.csv` is still written, since the report and the future actual-value line need it.
# The biweekly curation review is a SEPARATE
# job (scripts/review_curation.sh, weekdays 19:00) so it runs AFTER the 18:30 news pull and reads same-day
# news; the news pull itself is scripts/news_pull.sh (every day 18:30). Resolves its own location.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] snapshot start"
  # --force so this post-close run OVERWRITES any earlier same-day row (e.g. an intraday manual snapshot)
  # with the authoritative close, leaving exactly one row per date.
  .venv/bin/python -m src.cli snapshot --force
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS refresh start"
  # Live-extend each paper portfolio's equity curve with today's prices. Render-only: re-replays the
  # FIXED curation JSONs through the optimizer and re-renders -- no LLM call, no cost. Tolerated on failure.
  .venv/bin/python scripts/refresh_cbs.py || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS refresh failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CFT refresh start"
  .venv/bin/python scripts/refresh_cbs.py --run-dir data/curator_runs/forward-ft --out docs/index.html \
    --heading 'Curator Forwardtest' --acronym CFT --actual-csv data/snapshots.csv \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CFT refresh failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> data/snapshot.log 2>&1
