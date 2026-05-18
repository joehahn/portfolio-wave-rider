# Curator backtest report

**Window:** 2021-03-31 to 2026-03-31 (1826 calendar days, 1256 trading days)
**Starter watchlist:** AAPL, MSFT, GOOGL, NVDA, SPY
**Cadence:** quarterly
**Optimizer:** `mean_variance`, lookback 1.5y, max_weight 0.50

## Curation activity

| Metric | Value |
|---|---|
| Curation calls applied | 20 |
| Adds executed | 12 |
| Removes executed | 9 |
| Final watchlist size | 8 |
| Rebalances (optimizer calls) | 21 |
| Mean L1 weight distance rebalance-to-rebalance | 0.3316 |

## Realized performance vs baselines

| Strategy | Ending value | Total return | Active vs curator |
|---|---|---|---|
| Curator-driven | $440,019.31 | +780.04% | — |
| Buy-and-hold starter (equal-weight, then hold) | $214,360.62 | +328.72% | +451.32pp |

## Risk and benchmarks

| Metric | Value |
|---|---|
| Annualized return (curator) | +54.45% |
| Max drawdown (curator) | -47.55% |

### Benchmarks (over the same window)

| Benchmark | Return | Active vs curator |
|---|---|---|
| SPY | +75.69% | +704.35pp |

## Caveats

- No transaction costs or taxes modeled.
- Look-ahead-bias guard: each optimizer call sees prices only up to that date; the curator payloads in this run were generated with strict as-of-date discipline (see the watchlist-curator agent spec).
- Tickers added by the curator that have less than 30 trading days of history at the rebalance date are dropped from the optimizer's slice for that rebalance only.
