# Curator backtest report

**Window:** 2021-03-31 to 2026-03-31 (1826 calendar days, 1256 trading days)
**Starter watchlist:** AAPL, MSFT, GOOGL, NVDA, SPY
**Cadence:** quarterly
**Optimizer:** `mean_variance`, lookback 1.5y, max_weight 0.50

## Curation activity

| Metric | Value |
|---|---|
| Curation calls applied | 20 |
| Adds executed | 14 |
| Removes executed | 7 |
| Final watchlist size | 12 |
| Rebalances (optimizer calls) | 21 |
| Mean L1 weight distance rebalance-to-rebalance | 0.7407 |

## Realized performance vs baselines

| Strategy | Ending value | Total return | Active vs curator |
|---|---|---|---|
| Curator-driven | $87,187.45 | +74.37% | — |
| Buy-and-hold starter (equal-weight, then hold) | $214,360.64 | +328.72% | -254.35pp |

## Risk and benchmarks

| Metric | Value |
|---|---|
| Annualized return (curator) | +11.76% |
| Max drawdown (curator) | -69.72% |

### Benchmarks (over the same window)

| Benchmark | Return | Active vs curator |
|---|---|---|
| SPY | +75.69% | -1.31pp |

## Caveats

- No transaction costs or taxes modeled.
- Look-ahead-bias guard: each optimizer call sees prices only up to that date; the curator payloads in this run were generated with strict as-of-date discipline (see the watchlist-curator agent spec).
- Tickers added by the curator that have less than 30 trading days of history at the rebalance date are dropped from the optimizer's slice for that rebalance only.
