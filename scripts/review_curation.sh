#!/usr/bin/env bash
# Biweekly curation review + report, weekdays (Mon-Fri) 19:00 local.
#
# Split out of price_snapshot.sh so it runs AFTER the 18:30 news pull (scripts/news_pull.sh) and thus
# curates on SAME-DAY news (the old embedded 16:30 review read ~day-old news). --if-due self-gates to
# investor_profile.md's rebalance_period, so firing every weekday is safe: it actually curates only once
# per period (biweekly at present), the exact day floats to period-end, and a missed run catches up on the
# next weekday wake. The cadence lives in the profile, not in cron -- change rebalance_period there and this
# job follows with no cron edit. Recommendation-only; never trades. Resolves its own location.
#
# Also re-curates the Curator Bootstrap (CBS) paper portfolio on the same cadence. run_bootstrap_curator.py
# is incremental: --if-due exits immediately unless a biweekly date is missing its curation JSON, so the
# LLM cost is one call per period, not one per firing. Without this the CBS watchlist FREEZES past its last
# manual run and only its equity curve moves (the 16:30 refresh_cbs.py is render-only, no LLM).
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review (if due) start"
  .venv/bin/python -m src.cli review --if-due || echo "[$(date '+%Y-%m-%d %H:%M:%S')] review failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review done"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation (if due) start"
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation done"
} >> data/snapshot.log 2>&1
