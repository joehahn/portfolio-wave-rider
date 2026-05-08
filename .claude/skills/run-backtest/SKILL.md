---
name: run-backtest
description: Run a 12-month walk-forward backtest of the optimizer using mean_variance λ=1, time-varying wave tilts from data/wave_history.csv, and the current 12-ticker watchlist. Auto-renders both data/backtest/dashboard.html and docs/backtest.html, and updates the public-demo backtest page in one shot.
---

# /run-backtest

The "what would have happened" page. Replays the optimizer over the last 12 months at weekly cadence, applying the wave-stage tilts that were known at each historical Friday (from `data/wave_history.csv`), so we can spot-check the optimizer's recommendations against real out-of-sample price data.

This skill is a one-step wrapper around the underlying CLI. There is no LLM cost; everything is pure Python.

## Before you start

1. Read `holdings.csv`. The watchlist defines the backtest universe.
2. Verify `data/wave_history.csv` exists. If it doesn't, the backtest will run with no wave tilts (effectively the cron `recommend` path); warn the user that they can run `/initialize-portfolio` and `/review-portfolio` to populate wave_history before backtesting, or accept the no-tilt baseline.
3. The default backtest configuration matches the public-demo headline numbers: `--objective mean_variance --risk-aversion 1.0`, 12-month window, max_weight 0.25, SPY benchmark. Optional overrides: `--objective`, `--risk-aversion`, `--start-date`, `--end-date`, `--max-weight`. Pass these through if the user requested specific values.

## Step 1 — run the backtest (Bash)

```
python -m src.cli backtest \
  --objective mean_variance \
  --risk-aversion 1.0 \
  --wave-history data/wave_history.csv \
  --benchmarks SPY
```

The CLI writes to `data/backtest/` (snapshots.csv, recommendations.csv, report.md) and **auto-renders** both the local `data/backtest/dashboard.html` and the public `docs/backtest.html`. Capture the realized return, max drawdown, vs-SPY active return, and the rendered paths from the JSON return.

## Step 2 — final output to the user

One short message:

- Backtest summary: realized return, max drawdown, vs SPY (one line each).
- Rendered paths: `data/backtest/dashboard.html` (local) and `docs/backtest.html` (public, ready to push).
- Suggest committing `docs/backtest.html` to publish the refresh: `git add docs/backtest.html && git commit -m "Refresh backtest dashboard" && git push`.

## Rules

- Pure-Python path. No LLM agents are invoked. The wave tilts come from `data/wave_history.csv` (already populated by past /review-portfolio runs and any historical news pulls), not from a fresh news-researcher call.
- Don't write to `data/snapshots.csv` or `data/recommendations.csv` (those are the live time-series). The backtest writes to its own `data/backtest/` directory.
- Don't override `--thesis-baseline` or pass any thesis-related flag to the backtest dashboard. The backtest deliberately predates any thesis allocation; its dashboard renders the full yearlong window.
