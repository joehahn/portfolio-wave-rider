# Backtest report

**Window:** 2021-09-30 to 2026-04-30 (1673 calendar days, 1150 trading days)
**Tickers:** AAPL, SPY, AGG, BIL, IAU, GOOGL, NVDA, MSFT, BOTZ, QTUM, VIG
**Benchmarks:** SPY
**Optimizer:** `mean_variance`, lookback 1.3y, max_weight 0.25
**Rebalance cadence:** monthly (first trading day of each month)
**Transaction costs:** none modeled

## Realized performance

| Metric | Value |
|---|---|
| Starting value | $50,000.00 |
| Ending value | $115,516.40 |
| Realized return | +131.03% |
| Annualized return | +20.04% |
| Max drawdown | -37.80% |
| Buy-and-hold return (start-date weights) | +91.22% |
| Active return vs buy-and-hold | +39.81pp |
| SPY (over the same window) | +78.15% |
| Active return vs SPY | +52.89pp |

## Weight stability

**Rebalance count:** 20
**Mean week-over-week L1 distance between weight vectors:** 0.6066
(Lower is more stable. 0 = same weights every week. 2 = full portfolio flipped between two disjoint sets every week.)

## Caveats

- No transaction costs, taxes, or market-impact slippage.
- No news, no wave-stage tilts. This is the cron `recommend` path's behavior, not `/review-portfolio`'s.
- Look-ahead-bias-free: each Friday's optimizer sees only prices up to that Friday.
- The 3-year lookback is the same window the live system uses, so this backtest reflects how the live system would have decided.
