---
name: run-backtest
description: Run a 12-month walk-forward backtest of the optimizer using mean_variance λ=1, time-varying wave tilts from data/wave_history.csv, and the current 12-ticker watchlist. Auto-renders both data/backtest/dashboard.html and docs/backtest.html, then re-runs the lambda and lookback sweeps so docs/lambda_comparison.html and docs/lookback_comparison.html refresh in the same invocation.
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

## Step 2 — refresh the lambda sweep (Bash)

```
python scripts/compare_lambdas.py
```

Runs the same 12-month walk-forward six times (one per λ in `[0.0, 0.33, 1.0, 3.3, 10.0, 33.3]`) and writes `data/backtest/lambda_comparison.html` and `docs/lambda_comparison.html`.

## Step 3 — refresh the lookback sweep (Bash)

```
python scripts/compare_lookbacks.py
```

Runs the same 12-month walk-forward nine times (one per lookback in `[0.25, 0.5, 0.75, 1.0, 1.3, 2.0, 3.0, 4.0, 5.0]` years) and writes `data/backtest/lookback_comparison.html` and `docs/lookback_comparison.html`. A ticker is included in the universe only if its history extends back at least LB years before the backtest start, so NUKZ drops for LB ≥ 2y (launched 2024-01-24) and RKLB also drops at LB = 5y (started 2020-11-24). Universes are not identical across the sweep, which is flagged in the chart caption.

## Step 4 — final output to the user

One short message:

- Backtest summary: realized return, max drawdown, vs SPY (one line each).
- Rendered paths: `data/backtest/dashboard.html` (local), plus the three public pages (`docs/backtest.html`, `docs/lambda_comparison.html`, `docs/lookback_comparison.html`).
- Suggest committing all three to publish the refresh: `git add docs/backtest.html docs/lambda_comparison.html docs/lookback_comparison.html && git commit -m "Refresh backtest + sweeps" && git push`.

## Rules

- Pure-Python path. No LLM agents are invoked. The wave tilts come from `data/wave_history.csv` (already populated by past /review-portfolio runs and any historical news pulls), not from a fresh news-researcher call.
- Don't write to `data/snapshots.csv` or `data/recommendations.csv` (those are the live time-series). The backtest writes to its own `data/backtest/` directory.
- Don't override `--thesis-baseline` or pass any thesis-related flag to the backtest dashboard. The backtest deliberately predates any thesis allocation; its dashboard renders the full yearlong window.
