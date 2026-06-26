# Curator backtest report

**Window:** 2022-03-31 to 2025-10-31 (1310 calendar days, 901 trading days)
**Starter watchlist:** AAPL, MSFT, GOOGL, NVDA, SPY
**Cadence:** quarterly
**Optimizer:** `mean_variance`, lookback 1.5y, max_weight 0.70

## Curation activity

| Metric | Value |
|---|---|
| Curation calls applied | 15 |
| Adds executed | 7 |
| Removes executed | 4 |
| Final watchlist size | 8 |
| Rebalances (optimizer calls) | 16 |
| Mean L1 weight distance rebalance-to-rebalance | 0.7560 |

## Realized performance vs baselines

| Strategy | Ending value | Total return | Active vs curator |
|---|---|---|---|
| Curator-driven | $193,128.08 | +286.26% | — |
| Buy-and-hold starter (equal-weight, then hold) | $143,628.48 | +187.26% | +99.00pp |

## Risk and benchmarks

| Metric | Value |
|---|---|
| Annualized return (curator) | +45.72% |
| Max drawdown (curator) | -42.97% |

### Benchmarks (over the same window)

| Benchmark | Return | Active vs curator |
|---|---|---|
| SPY | +58.69% | +227.57pp |

## Caveats

- No transaction costs or taxes modeled.
- Execution lag: t_update_days=1. Each rebalance is decided on the rebalance date's close but executed 1 trading day(s) later at that day's close (the one-time initial deployment is not lagged). This models the gap between running a review and placing the trade. 0 = the optimistic same-close run. Over this window the result is insensitive to the lag (within noise across 0-3 days), so the curator's edge is not a fast-execution artifact.
- Look-ahead-bias guard: each optimizer call sees prices only up to that date; the curator payloads in this run were generated with strict as-of-date discipline (see the watchlist-curator agent spec).
- Tickers added by the curator that have less than 30 trading days of history at the rebalance date are dropped from the optimizer's slice for that rebalance only.
