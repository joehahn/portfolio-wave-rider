# Curator backtest report

**Window:** 2021-03-31 to 2026-03-31 (1826 calendar days, 1256 trading days)
**Starter watchlist:** AAPL, MSFT, GOOGL, SPY, AGG
**Cadence:** quarterly
**Optimizer:** `mean_variance`, lookback 3.0y, max_weight 0.50

## Curation activity

| Metric | Value |
|---|---|
| Curation calls applied | 20 |
| Adds executed | 17 |
| Removes executed | 10 |
| Final watchlist size | 11 |
| Rebalances (optimizer calls) | 21 |
| Mean L1 weight distance rebalance-to-rebalance | 0.3828 |

## Realized performance vs baselines

| Strategy | Ending value | Total return | Active vs curator |
|---|---|---|---|
| Curator-driven | $349,726.24 | +599.45% | — |
| Buy-and-hold starter (day-0 optimize, then hold) | $94,234.47 | +88.47% | +510.98pp |

## Risk and benchmarks

| Metric | Value |
|---|---|
| Annualized return (curator) | +47.52% |
| Max drawdown (curator) | -49.45% |

### Benchmarks (over the same window)

| Benchmark | Return | Active vs curator |
|---|---|---|
| SPY | +75.69% | +523.76pp |

## Caveats

- No transaction costs or taxes modeled.
- Look-ahead-bias guard: each optimizer call sees prices only up to that date; the curator payloads in this run were generated with strict as-of-date discipline (see the watchlist-curator agent spec).
- Tickers added by the curator that have less than 30 trading days of history at the rebalance date are dropped from the optimizer's slice for that rebalance only.
