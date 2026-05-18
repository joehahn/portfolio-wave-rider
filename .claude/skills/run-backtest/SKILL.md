# /run-backtest

Refreshes the 5-year curator backtest against a rolling 5-year window
ending today. Diffs the target quarter-end dates against the committed
curator JSONs, fires fresh `watchlist-curator` agent calls for any
missing quarter-ends (with strict as-of-date discipline), archives
JSONs that have rolled out of the window, regenerates the backtest
output and the public dashboard, and pushes the result to GitHub.

First-time invocation by a fresh clone: ~$3 in LLM calls (20 fresh
quarter-ends). Subsequent invocations once new quarters have elapsed:
~$0.15 per new quarter. If nothing new is needed (helper reports
missing=[] and stale=[]), the skill skips the LLM step and just re-runs
the math replay.

## Before you start

1. The runs dir is `data/curator_runs/5y-quarterly/`. Commit this dir
   (and the regenerated `data/backtest_curator_5y/` outputs +
   `docs/backtest_curator.html`) at the end of the run so the public
   demo always reflects the latest 5y window.
2. The starter watchlist is fixed at `[AAPL, MSFT, GOOGL, NVDA, SPY]` —
   a 2021-tech-savvy investor's portfolio before the AI surge. The
   fixed starter keeps the backtest's day-0 conditions stable across
   refreshes; only the trailing edge moves.
3. The strict as-of-date discipline (persona reset, WebSearch
   `before:` filters, suppression list, self-critique) applies to
   every fresh curator call. See `.claude/agents/watchlist-curator.md`
   for the full spec.

## Orchestration

### Step 1 — diff the rolling window against committed JSONs (Bash)

```bash
.venv/bin/python scripts/compute_backtest_dates.py
```

Returns JSON with `target_dates` (the 20 most recent quarter-ends),
`existing_dates`, `missing_dates`, `stale_dates`. Capture this output.

If `missing_dates` is empty, skip to step 4.

### Step 2 — fire curator calls for missing dates (Task, batched)

For each date in `missing_dates`, spawn the `watchlist-curator` subagent
via `Task(subagent_type="watchlist-curator")` with a prompt templated
from these inputs:

- `as_of_date`: the missing date.
- `current_watchlist`: replay all `existing_dates` JSONs in
  chronological order to determine the watchlist state at that point.
  Use `portfolio.reconstruct_watchlist_at(date, starter, history_path)`
  if a sandbox history file is available, or compute by hand from the
  payloads' `adds`/`removes` if not. Note that one missing date may be
  inside the existing range (a "gap fill") OR newer than any existing
  date (a "forward extension"); the replay logic handles both.
- `max_watchlist_size`: 12.
- `rebalance_period`: quarterly.
- `recent_news_lookback_days`: 90.
- `profile_wave_thesis`: prose from `investor_profile.md`'s "Strategy &
  beliefs" section.
- `exclusions`: from the profile's `exclusions` YAML field.
- `post_date_events`: from
  `python -c "from scripts.post_date_events import events_after; print('\n'.join(events_after('<DATE>')))"`.

Fire in batches of 4 parallel Task calls per message (matches the
rate-limit pattern from the original 5y experiment). Between batches,
update the `current_watchlist` for subsequent batches by replaying the
just-completed dates' applied adds/removes.

Save each agent return to `data/curator_runs/5y-quarterly/<date>-curation.json`.

### Step 3 — archive stale JSONs (Bash)

For each date in `stale_dates`:

```bash
mkdir -p data/curator_runs/5y-quarterly/_archive
mv data/curator_runs/5y-quarterly/<date>-curation.json \
   data/curator_runs/5y-quarterly/_archive/<date>-curation.json
```

Archive rather than delete so the historical decisions stay
recoverable from the working tree, but they're out of the runs dir
the replay picks up.

### Step 4 — regenerate `_starter.json` (Bash)

```bash
.venv/bin/python - <<'PY'
import json, pandas as pd, glob
from pathlib import Path
runs = Path("data/curator_runs/5y-quarterly")
dates = sorted(pd.Timestamp(p.stem.replace("-curation","")) for p in runs.glob("*-curation.json"))
starter = {
    "starter_watchlist": ["AAPL", "MSFT", "GOOGL", "NVDA", "SPY"],
    "as_of_dates": [d.strftime("%Y-%m-%d") for d in dates],
    "rebalance_period": "quarterly",
    "initial_usd": 50000.0,
    "lookback_years": 1.3,
    "max_watchlist_size": 12,
    "start_date": dates[0].strftime("%Y-%m-%d"),
    "end_date": dates[-1].strftime("%Y-%m-%d"),
}
(runs / "_starter.json").write_text(json.dumps(starter, indent=2))
PY
```

### Step 5 — run the math replay (Bash)

```bash
.venv/bin/python -m src.cli backtest \
  --curator-runs-dir data/curator_runs/5y-quarterly \
  --out-dir data/backtest_curator_5y \
  --max-weight 0.25 --risk-aversion 1.0 \
  --benchmarks SPY
```

### Step 6 — render the public dashboard (Bash)

```bash
.venv/bin/python -m src.cli dashboard \
  --curator-backtest-dir data/backtest_curator_5y \
  --curator-runs-dir data/curator_runs/5y-quarterly \
  --benchmarks SPY
```

### Step 7 — commit and push (Bash)

```bash
git add data/curator_runs/5y-quarterly/*.json \
        data/backtest_curator_5y/ \
        docs/backtest_curator.html
git commit -m "Refresh 5y curator backtest (rolling window ending $(date +%Y-%m-%d))"
git push origin main
```

If `data/curator_runs/5y-quarterly/_archive/` got new entries this run,
stage them too. Skip the commit if nothing changed (replay produced
identical output and no JSONs were added/archived).

## Final output to the user

One short message:

- Count of new curator calls fired (and rough $ cost: count × $0.15).
- Count of archived stale JSONs.
- New headline: realized return, vs-fixed-baseline lift, vs-SPY lift.
- "Pushed to origin/main; public dashboard refreshed at
  https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html"

## Rules

- The starter watchlist is fixed at `[AAPL, MSFT, GOOGL, NVDA, SPY]`.
  Don't reinterpret it based on the current date; that would change
  day-0 conditions and make refreshes non-comparable to prior ones.
- Strict as-of-date discipline applies to every fresh curator call,
  even for very recent quarter-ends. Pass the suppression list from
  `events_after(date)` even if it's empty for the most-recent date.
- Don't delete stale JSONs; archive them. The committed
  `data/curator_runs/5y-quarterly/_archive/` directory is the
  historical record of what the backtest used to include.
- Commit + push is part of the skill, not a follow-up step the user
  has to remember. The public dashboard must always reflect the
  latest run.
