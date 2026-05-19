# /sweep-max-watchlist-size

Sweeps the `max_watchlist_size` cap by firing fresh `watchlist-curator`
calls at four cap values (5, 12, 16, 24) over the 21 quarter-end dates of
the standard 5y backtest window. cap=8 is the project's default and
feeds the canonical `data/curator_runs/5y-sweep-cap08/` run dir; the
sweep also includes the older cap=12 dir (`data/curator_runs/5y-quarterly/`)
as a historical reference at the previous default.

Each cap shapes the curator's decisions (not just the optimizer), so
unlike the other three sweeps (λ, lookback, max_weight) this one cannot
be a pure replay — every (cap, date) pair needs its own curator call.

**Cost / time:** 4 caps × 21 dates × ~$0.15 = ~$13 for a from-scratch
run; ~15 min wall clock at 4-parallel batching. **Idempotent:** the
skill skips dates that already have a JSON in their cap's runs dir, so
partial runs can be resumed cheaply.

## Before you start

1. The cap=8 run dir is `data/curator_runs/5y-sweep-cap08/` (the new
   default after the cap sweep showed Sharpe 1.18 there). The legacy
   cap=12 dir at `data/curator_runs/5y-quarterly/` is also populated
   (historical). Do not re-fire either.
2. Per-cap dirs `data/curator_runs/5y-sweep-cap{05,08,16,24}/` already
   contain `_starter.json` files (starter `[AAPL, MSFT, GOOGL, NVDA,
   SPY]` with the cap-specific `max_watchlist_size`).
3. Strict as-of-date discipline applies to every fresh curator call
   (persona reset, WebSearch `before:` filters, suppression list,
   self-critique). See `.claude/agents/watchlist-curator.md`.

## Orchestration

### Step 1 — figure out what's missing (Bash)

```bash
for cap in 05 08 16 24; do
  echo "=== cap=$cap ==="
  ls data/curator_runs/5y-sweep-cap${cap}/*.json 2>/dev/null | wc -l
done
```

Each cap should have 21 `<date>-curation.json` files when complete.

### Step 2 — fire curator calls for missing (cap, date) pairs (Task, batched)

The 21 quarter-end dates are:

```
2021-03-31, 2021-06-30, 2021-09-30, 2021-12-31,
2022-03-31, 2022-06-30, 2022-09-30, 2022-12-31,
2023-03-31, 2023-06-30, 2023-09-30, 2023-12-31,
2024-03-31, 2024-06-30, 2024-09-30, 2024-12-31,
2025-03-31, 2025-06-30, 2025-09-30, 2025-12-31,
2026-03-31
```

For each date in chronological order, fire 4 parallel Task calls (one
per cap = 5, 8, 16, 24), skipping the (cap, date) pair if its JSON
already exists. The dates must be processed in order because each
call's `current_watchlist` input depends on the prior dates' applied
adds and removes.

Compute `current_watchlist` for each (cap, date) via:

```bash
.venv/bin/python scripts/replay_watchlist.py \
  --runs-dir data/curator_runs/5y-sweep-cap{NN} \
  --as-of {YYYY-MM-DD}
```

The script replays prior JSONs through the validator's logic (drops
"already in" adds, "stale" removes, and trailing adds that would push
the watchlist over the cap).

Each agent prompt (use `subagent_type="general-purpose"` since
`watchlist-curator` is not directly available via the Agent tool —
embed the curator spec inline) needs:

- `as_of_date`: the date.
- `current_watchlist`: from `replay_watchlist.py`.
- `max_watchlist_size`: the cap.
- `rebalance_period`: quarterly.
- `recent_news_lookback_days`: 90.
- `profile_wave_thesis`: AI ride/trim + rockets / robotics / quantum /
  nuclear (no engineered_biology).
- `exclusions`: solar, wind.
- `post_date_events`: from
  `python -c "from scripts.post_date_events import events_after; print('\n'.join(events_after('{DATE}')))"`.

Save each Task's JSON return to
`data/curator_runs/5y-sweep-cap{NN}/{DATE}-curation.json`.

### Step 3 — run math replay for each cap (Bash)

```bash
for cap in 05 08 16 24; do
  .venv/bin/python -m src.cli backtest \
    --curator-runs-dir data/curator_runs/5y-sweep-cap${cap} \
    --out-dir data/curator_runs/5y-sweep-cap${cap}/_backtest \
    --max-weight 0.70 --risk-aversion 0.5 --benchmarks SPY
done
```

Each produces `snapshots.csv` in its respective `_backtest/` subdir
that the aggregator reads.

### Step 4 — render the sweep dashboard (Bash)

```bash
.venv/bin/python scripts/sweep_watchlist_size.py
```

Writes `docs/sweep_max_watchlist_size.html` (chart + summary table +
nav strip).

### Step 5 — commit and push (Bash)

```bash
git add data/curator_runs/5y-sweep-cap*/ \
        docs/sweep_max_watchlist_size.html
git commit -m "Refresh max_watchlist_size sweep"
git push origin main
```

## Rules

- Cap=8 (the project default) lives at `data/curator_runs/5y-sweep-cap08/`; cap=12 lives at `data/curator_runs/5y-quarterly/` as a historical reference. Do not re-fire either.
- Dates are processed in chronological order per cap; cross-cap calls
  for the same date can run in parallel (no dependency).
- Skip (cap, date) pairs whose JSON already exists; the skill is
  resumable.
- Strict as-of-date discipline applies to every fresh call regardless
  of cap.
- The curator's wave_bucket enum technically allows
  `engineered_biology` but the current profile doesn't name biology;
  the prompt should explicitly forbid ARKG/NTLA/CRSP/EDIT to avoid the
  thesis-misalignment issue documented in CLAUDE.md.
