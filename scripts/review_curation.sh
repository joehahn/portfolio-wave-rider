#!/usr/bin/env bash
# Biweekly curation review + report, weekdays (Mon-Fri) 19:00 local.
#
# Split out of price_snapshot.sh so it runs AFTER the 18:30 news pull (scripts/news_pull.sh) and thus
# curates on SAME-DAY news (the old embedded 16:30 review read ~day-old news). --if-due self-gates to
# investor_profile.md's rebalance_period, so firing every weekday is safe: it actually curates only once
# per period (biweekly at present), the exact day floats to period-end, and a missed run catches up on the
# next weekday wake. The cadence lives in the profile, not in cron -- change rebalance_period there and this
# job follows with no cron edit. Recommendation-only; never trades. Resolves its own location.
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review (if due) start"
  .venv/bin/python -m src.cli review --if-due || echo "[$(date '+%Y-%m-%d %H:%M:%S')] review failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review done"
} >> data/snapshot.log 2>&1
