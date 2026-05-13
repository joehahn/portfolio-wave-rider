# Curator backtest report

**Window:** 2021-09-30 to 2026-04-30 (1673 calendar days, 1150 trading days)
**Starter watchlist:** AAPL, MSFT, GOOGL, SPY, AGG
**Cadence:** quarterly
**Optimizer:** `mean_variance`, lookback 1.3y, max_weight 0.25

## Curation activity

| Metric | Value |
|---|---|
| Curation calls applied | 19 |
| Adds executed | 18 |
| Removes executed | 11 |
| Final watchlist size | 12 |
| Rebalances (optimizer calls) | 20 |
| Mean L1 weight distance rebalance-to-rebalance | 0.6579 |

## Realized performance vs baselines

| Strategy | Ending value | Total return | Active vs curator |
|---|---|---|---|
| Curator-driven | $117,730.88 | +135.46% | — |
| Fixed watchlist (same cadence, no curation) | $90,110.99 | +80.22% | +55.24pp |
| Buy-and-hold starter (day-0 optimize, then hold) | $101,843.76 | +103.69% | +31.77pp |

## Risk and benchmarks

| Metric | Value |
|---|---|
| Annualized return (curator) | +20.54% |
| Max drawdown (curator) | -39.90% |

### Benchmarks (over the same window)

| Benchmark | Return | Active vs curator |
|---|---|---|
| SPY | +78.15% | +57.32pp |

## Caveats

- No transaction costs or taxes modeled.
- Look-ahead-bias guard: each optimizer call sees prices only up to that date; the curator payloads in this run were generated with strict as-of-date discipline (see the watchlist-curator agent spec).
- Tickers added by the curator that have less than 30 trading days of history at the rebalance date are dropped from the optimizer's slice for that rebalance only.
