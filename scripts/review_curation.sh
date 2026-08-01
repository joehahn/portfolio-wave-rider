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
# Also re-curates the two PAPER portfolios on the same cadence, via the same incremental script:
#   CBS  seeded 2026-04-22 from the canonical CBT run; backtest-tail GKG news up to the handoff, live corpus after.
#   FT   seeded 2026-07-22 (the handoff) from the same CBT run; live corpus ONLY, no backtest news.
# Both use --if-due, which exits immediately unless a biweekly date is missing its curation JSON, so each
# costs one curator call per period, not one per firing. Without this their watchlists FREEZE past the last
# manual run and only the equity curves move (the 16:30 refreshes are render-only, no LLM).
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review (if due) start"
  .venv/bin/python -m src.cli review --if-due || echo "[$(date '+%Y-%m-%d %H:%M:%S')] review failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] review done"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation (if due) start"
  # --no-report on purpose: CBS is a comparison portfolio, not the recommendation of record, so it does
  # not write into data/reports/. FT (below) does, since that is the book real money follows.
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due --no-report \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation done"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] FT curation (if due) start"
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due --forward-only --since 2026-07-22 \
    --run-dir data/curator_runs/forward-ft --out docs/index.html \
    --heading Forwardtest --acronym FT --actual-csv data/snapshots.csv \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] FT curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] FT curation done"
} >> data/snapshot.log 2>&1
