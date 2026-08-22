#!/usr/bin/env bash
# Biweekly paper-portfolio curation, Sunday 19:00 local (with a Monday 08:00 catch-up firing).
#
# Runs AFTER the 18:30 news pull (scripts/news_pull.sh), which runs every day INCLUDING weekends, so the
# Sunday evening curation reads same-day news and the fresh recommendation is on the dashboard and in
# data/reports/ before the user rebalances Monday morning. Recommendation-only; never trades.
#
# --if-due self-gates to investor_profile.md's rebalance_period, so firing more often than the cadence is
# safe: each portfolio curates only once per period (biweekly at present). The cadence lives in the profile.
# GRID_ANCHOR sets the PHASE of that cadence -- a Sunday, so rebalance dates are Sundays. CBS and CFT are
# seeded on different dates (2026-04-22 Sat-phase, 2026-07-22 Wed-phase) and so used to fall on alternating
# weeks; anchoring both to the same Sunday makes them co-fire on one firing, every other week.
# Already-curated dates before the anchor keep their old slots, so re-phasing re-curates nothing.
#
# The old live `cli review --if-due` step is RETIRED (2026-08-01): it curated the root watchlist.csv in
# parallel with FT and produced a second, competing recommendation. FT is the recommendation of record.
# `cli review` still exists for a deliberate manual run.
#
# Curates the two PAPER portfolios via one incremental script:
#   CBS  seeded 2026-04-22 from the canonical CBT run; backtest-tail GKG news up to the handoff, live corpus after.
#   FT   seeded 2026-07-22 (the handoff) from the same CBT run; live corpus, plus any backtest-tail
#        article still inside the trailing news window (--blend-backtest-news). That blend weans
#        itself off automatically as the window clears the handoff, so early forward dates are not
#        starved of context while later ones are pure forward news.
# Both use --if-due, which exits immediately unless a biweekly date is missing its curation JSON, so each
# costs one curator call per period, not one per firing. Without this their watchlists FREEZE past the last
# manual run and only the equity curves move (the 16:30 refreshes are render-only, no LLM).
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"
# Any Sunday on the desired biweekly phase. Both paper portfolios share it, which is what aligns them.
GRID_ANCHOR=2026-08-30
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation (if due) start"
  # No --report: CBS is a comparison portfolio, not the recommendation of record, so it stays out of
  # data/reports/. Reporting is opt-in; only FT (below) asks for it.
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due --grid-anchor "$GRID_ANCHOR" \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CBS curation done"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CFT curation (if due) start"
  .venv/bin/python scripts/run_bootstrap_curator.py --if-due --forward-only --blend-backtest-news \
    --since 2026-07-22 --grid-anchor "$GRID_ANCHOR" \
    --run-dir data/curator_runs/forward-ft --out docs/index.html \
    --heading 'Curator Forwardtest' --acronym CFT --actual-csv data/snapshots.csv --report \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] CFT curation failed (tolerated)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CFT curation done"
} >> data/snapshot.log 2>&1
