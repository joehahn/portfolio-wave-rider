# /run-backtest

Refreshes the curator backtest over the window declared in
`investor_profile.md`'s `backtest` section (`start_date` / `end_date`).
Diffs the target quarter-end dates against the committed curator JSONs,
fires fresh `watchlist-curator` agent calls for any missing quarter-ends
(with strict as-of-date discipline), archives JSONs that fall outside
the window, regenerates the backtest output and the public dashboard,
and pushes the result to GitHub.

The published window is post-COVID, normal-regime (2022-03-31 →
2025-10-31, 15 quarter-ends). First-time / from-scratch run: ~$3 in LLM
calls. If the profile window is unchanged and all JSONs already exist
(helper reports missing=[] and stale=[]), the skill skips the LLM step
and just re-runs the math replay.

IMPORTANT: changing the window's *start* date invalidates every saved
JSON, because the curator's add/remove decisions are path-dependent on
the watchlist state that has accumulated since the start. A start-date
change means re-firing all quarter-ends from scratch (use a fresh run
dir or archive the old JSONs first). Changing only the *end* date later
is a cheap forward-extension / truncation.

## Before you start

1. The runs dir is `data/curator_runs/postcovid/`. Commit this dir
   (and the regenerated `data/backtest_curator_postcovid/` outputs +
   `docs/backtest_curator.html`) at the end of the run so the public
   demo always reflects the profile-defined window.
2. The starter watchlist is fixed at `[AAPL, MSFT, GOOGL, NVDA, SPY]` —
   a 2021-tech-savvy investor's portfolio before the AI surge. The
   fixed starter keeps the backtest's day-0 conditions stable across
   refreshes; only the trailing edge moves.
3. The strict as-of-date discipline (persona reset, WebSearch
   `before:` filters, suppression list, self-critique) applies to
   every fresh curator call. See `.claude/agents/watchlist-curator.md`
   for the full spec.

## Orchestration

### Step 1 — diff the profile window against committed JSONs (Bash)

```bash
.venv/bin/python scripts/compute_backtest_dates.py
```

Reads `start_date` / `end_date` from `investor_profile.md`'s `backtest`
section and returns JSON with `target_dates` (quarter-ends in the window),
`existing_dates`, `missing_dates`, `stale_dates`. Capture this output.
(If the profile pins no window, it falls back to a rolling window.)

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
- `max_watchlist_size`: 8.
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

Save each agent return to `data/curator_runs/postcovid/<date>-curation.json`.

Then preserve that agent's **real** WebSearch queries. Each `Task` result includes an `output_file:` path (the agent transcript); per-agent attribution works even though the agents run in parallel, because each has its own transcript. For each completed date run:

```bash
.venv/bin/python scripts/extract_search_terms.py <that agent's output_file> \
  --into data/curator_runs/postcovid/<date>-curation.json
```

This writes the actual `WebSearch` tool calls (verbatim, including the `before:` filters) into that date's `search_terms`, falling back to the agent's self-reported `search_terms` if the transcript can't be parsed. This is how the backtest preserves its query terms per rebalance date.

### Step 3 — archive stale JSONs (Bash)

For each date in `stale_dates`:

```bash
mkdir -p data/curator_runs/postcovid/_archive
mv data/curator_runs/postcovid/<date>-curation.json \
   data/curator_runs/postcovid/_archive/<date>-curation.json
```

Archive rather than delete so the historical decisions stay
recoverable from the working tree, but they're out of the runs dir
the replay picks up.

### Step 4 — regenerate `_starter.json` (Bash)

```bash
.venv/bin/python - <<'PY'
import json, pandas as pd
from pathlib import Path
from src.portfolio import load_backtest_config
bc = load_backtest_config()                     # window from investor_profile.md
runs = Path("data/curator_runs/postcovid")
dates = sorted(pd.Timestamp(p.stem.replace("-curation","")) for p in runs.glob("*-curation.json"))
starter = {
    "starter_watchlist": ["AAPL", "MSFT", "GOOGL", "NVDA", "SPY"],
    "as_of_dates": [d.strftime("%Y-%m-%d") for d in dates],
    "rebalance_period": "quarterly",
    "initial_usd": 50000.0,
    "lookback_years": 1.5,
    "max_watchlist_size": 8,
    # Window edges come from the profile, not the JSON glob: end_date is
    # typically a month past the last quarter-end so the final rebalance holds.
    "start_date": bc["start_date"] or dates[0].strftime("%Y-%m-%d"),
    "end_date": bc["end_date"] or dates[-1].strftime("%Y-%m-%d"),
}
(runs / "_starter.json").write_text(json.dumps(starter, indent=2))
PY
```

### Step 5 — run the math replay (Bash)

```bash
.venv/bin/python -m src.cli backtest \
  --curator-runs-dir data/curator_runs/postcovid \
  --out-dir data/backtest_curator_postcovid \
  --benchmarks SPY
```

The optimizer knobs come from `investor_profile.md`'s `backtest` section: if `risk_aversion` / `lookback_years` / `concentration_cap` are set there (backtest-only overrides) they win; otherwise the live `financial_model` + top-level `concentration_cap` values are used. The published dashboard uses the aggressive overrides (λ=0.33 / lookback 0.5y / cap=1.0), which is why it renders +910% (a ~100%-RKLB, overfit ceiling) rather than the live/robust +313.6%. Do NOT pass `--max-weight` / `--risk-aversion` here — that would override the profile-driven config.

Execution lag defaults to `--t-update-days 1` (rebalance decided on the close, trade lands the next session — the realistic case). Pass `--t-update-days 0` for the optimistic same-close upper bound; on this short window the lag is material.

### Step 6 — render the public dashboard (Bash)

```bash
.venv/bin/python -m src.cli dashboard \
  --curator-backtest-dir data/backtest_curator_postcovid \
  --curator-runs-dir data/curator_runs/postcovid \
  --benchmarks SPY
```

### Step 7 — commit and push (Bash)

```bash
git add data/curator_runs/postcovid/*.json \
        data/backtest_curator_postcovid/ \
        docs/backtest_curator.html
git commit -m "Refresh curator backtest (profile window, regenerated $(date +%Y-%m-%d))"
git push origin main
```

If `data/curator_runs/postcovid/_archive/` got new entries this run,
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
  `data/curator_runs/postcovid/_archive/` directory is the
  historical record of what the backtest used to include.
- Commit + push is part of the skill, not a follow-up step the user
  has to remember. The public dashboard must always reflect the
  latest run.
