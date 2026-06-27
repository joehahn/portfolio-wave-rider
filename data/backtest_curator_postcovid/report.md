# Curator backtest report

**Window:** 2022-03-31 to 2025-10-31 (1310 calendar days, 901 trading days)
**Starter watchlist:** AAPL, MSFT, GOOGL, NVDA, SPY
**Cadence:** quarterly
**Optimizer:** `mean_variance`, lookback 1.5y, max_weight 0.70

## Curation activity

| Metric | Value |
|---|---|
| Curation calls applied | 15 |
| Adds executed | 9 |
| Removes executed | 6 |
| Final watchlist size | 8 |
| Rebalances (optimizer calls) | 16 |
| Mean L1 weight distance rebalance-to-rebalance | 0.7363 |

## Realized performance vs baselines

| Strategy | Ending value | Total return | Active vs curator |
|---|---|---|---|
| Curator-driven | $206,795.53 | +313.59% | — |
| Buy-and-hold starter (equal-weight, then hold) | $143,628.48 | +187.26% | +126.33pp |

## Risk and benchmarks

| Metric | Value |
|---|---|
| Annualized return (curator) | +48.52% |
| Max drawdown (curator) | -42.97% |

### Benchmarks (over the same window)

| Benchmark | Return | Active vs curator |
|---|---|---|
| SPY | +58.69% | +254.90pp |

## Caveats

- No transaction costs or taxes modeled.
- Execution lag: t_update_days=1. Each rebalance is decided on the rebalance date's close but executed 1 trading day(s) later at that day's close (the one-time initial deployment is not lagged). This models the gap between running a review and placing the trade. 0 = the optimistic same-close run. How much the lag matters is window-dependent (near-noise over long windows, material over short ones); compare against the t=0 run.
- Look-ahead bias is only partly controlled. The optimizer math is clean: it sees prices only up to the rebalance date. The curator's news/selection path is NOT fully clean: the agent's strict as-of-date discipline (persona reset, WebSearch `before:` filters, suppression list, self-critique) suppresses explicit citation of post-date facts, but it cannot remove (a) the LLM's own training-cutoff foreknowledge of which tickers later won, (b) selection bias from fame-weighted search ranking that surfaces eventual winners' early coverage, or (c) survivorship/revision bias in today's edited/deleted web record. This run uses plain WebSearch + `before:` filters only (no Wayback as-of snapshots or date-honored corpus), so treat the result as a hindsight-tinted upper bound, not a clean out-of-sample backtest.
- Tickers added by the curator that have less than 30 trading days of history at the rebalance date are dropped from the optimizer's slice for that rebalance only.
