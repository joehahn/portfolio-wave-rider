#!/usr/bin/env bash
# Biweekly paper-portfolio curation, weekdays (Mon-Fri) 19:00 local.
#
# Runs AFTER the 18:30 news pull (scripts/news_pull.sh) so it curates on SAME-DAY news. --if-due self-gates
# to investor_profile.md's rebalance_period, so firing every weekday is safe: each portfolio curates only
# once per period (biweekly at present), the exact day floats to period-end, and a missed run catches up on
# the next weekday wake. The cadence lives in the profile, not in cron. Recommendation-only; never trades.
#
# The old live `cli review --if-due` step is RETIRED (2026-08-01): it curated the root watchlist.csv in
# parallel with FT and produced a second, competing recommendation. FT is the recommendation of record.
# `cli review` still exists for a deliberate manual run.
#
# Curates the two PAPER portfolios via one incremental script:
#   CBS  seeded 2026-04-22 from the canonical CBT run; backtest-tail GKG news up to the handoff, live corpus after.
#   FT   seeded 2026-07-22 (the handoff) from the same CBT run; live corpus ONLY, no backtest news.
# Both use --if-due, which exits immediately unless a biweekly date is missing its curation JSON, so each
# costs one curator call per period, not one per firing. Without this their watchlists FREEZE past the last
# manual run and only the equity curves move (the 16:30 refreshes are render-only, no LLM).
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation (if due) start"
  # No --report: CBS is a comparison portfolio, not the recommendation of record, so it stays out of
  # data/reports/. Reporting is opt-in; only FT (below) asks for it.
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation done"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] FT curation (if due) start"
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due --forward-only --since 2026-07-22 \
    --run-dir data/curator_runs/forward-ft --out docs/index.html \
    --heading Forwardtest --acronym FT --actual-csv data/snapshots.csv --report \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] FT curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] FT curation done"
} >> data/snapshot.log 2>&1
