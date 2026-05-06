# Glossary

Finance and stats terms used in the README, reports, and dashboard.

## Symbols

- `r` = return (typically a daily log return); `E[r]` = expected (mean) return.
- `σ` = standard deviation of returns (a.k.a. **volatility**).
- `μ` = vector of expected returns, one entry per asset (one of the optimizer's two inputs).
- `Σ` = covariance matrix of returns (the optimizer's other input).
- `w` = vector of portfolio weights, one entry per asset. Constrained: `Σw_i = 1` (fully invested) and each `w_i ≥ 0` (long-only).
- `V_t` = cumulative portfolio value at time `t`.
- `cummax(V)_t` = the running max of `V` through time `t`. Same semantics as pandas' `Series.cummax()`.
- `α` = quantile level (e.g., `α = 0.05` picks out the 5% tail of the return distribution).

## Wave-stage cycle

The four wave stages map onto a typical investment cycle. Each stage has a multiplier on `μ` (the expected-return vector) that the optimizer applies before solving:

```
buildup (×1.20)  →  surge (×1.10)  →  peak (×0.80)  →  digestion (×0.90)
early, cheap        adoption           enthusiasm        post-crest
under-owned         compounding        priced in         hangover

neutral (×1.00)  —  no wave attachment (general_markets), no tilt
```

The multipliers above are the defaults; they can be overridden per-investor in `investor_profile.md`'s `financial_model.wave_stage_tilts`. See the **Wave-stage tilt** entry below for the math.

## Terms

| Term | Plain definition |
|---|---|
| **Ticker** | Symbol identifying a security: `AAPL` is Apple, `AGG` is an aggregate bond ETF, `IBIT` is a spot-Bitcoin ETF. |
| **ETF** | Exchange-traded fund. A packaged basket of underlying securities that trades like a single stock. |
| **Long-only** | All portfolio weights `w_i ≥ 0`. No short selling, no leverage. |
| **Mean-variance optimization** | Markowitz framework. Convex quadratic program: pick weights `w` that minimize `wᵀΣw` (variance) subject to `wᵀμ = target` (target return) and `Σw = 1`, `w ≥ 0`. We use `scipy.optimize.minimize(SLSQP)`. |
| **Risk-free rate (`r_free`)** | The return you can earn with effectively zero risk by parking money in short-term US Treasuries or a money-market fund. Default `0.04` (4% annualized, roughly a 1-year Treasury yield). Set in `investor_profile.md` under `financial_model.risk_free_rate`; overridable per invocation via `--risk-free-rate` on `analyze`, `recommend`, and `backtest`. |
| **Sharpe ratio** | Signal-to-noise on returns: `(E[r] − r_free) / σ`. The numerator is the **excess return** (return above what's free). The denominator is the standard deviation of returns. You only get credit for the risk-bearing part of `E[r]`. Higher is better; values above 1 are good for a long-horizon portfolio. |
| **Risk aversion (`λ`)** | Scalar coefficient in the mean-variance utility `μᵀw − λ·wᵀΣw`. Set in `investor_profile.md` under `financial_model.risk_aversion`; overridable via `--risk-aversion` on `analyze`, `recommend`, and `backtest` (only matters when `--objective mean_variance`). Small `λ` (≤ 1) favors expected return → equity-heavy portfolios; large `λ` (≥ 5) favors variance reduction → bond/cash-heavy. Sliding `λ` traces the efficient frontier; `max_sharpe` picks one specific point on it (the tangent). |
| **Max drawdown** | Worst observed peak-to-trough decline of cumulative value: `min_t (V_t − cummax(V)_t) / cummax(V)_t`. A max drawdown of `-0.30` means at some point the portfolio lost 30% from a prior high. |
| **VaR_α** | Value-at-risk: the α-quantile of the daily return distribution. `VaR_0.05 = -0.02` means there's a 5% chance of losing more than 2% on a given day (under the empirical distribution). |
| **CVaR_α** | Conditional VaR: the expected return conditioned on being below `VaR_α`. Tail-loss expectation. |
| **Concentration cap** | Box constraint on the optimizer: `w_i ≤ max_weight` for every asset. Profile default 0.25. |
| **Asset class** | Coarse bucket: equities, bonds, precious metals, cash. |
| **Asset-class drift** | Deviation of recommended weights summed by class from the user's declared target percentages. Reported but not enforced. |
| **Wave-stage tilt** | Multiplicative scaling on `μ` (the expected-return vector) before optimization. `μ_tilted[i] = stage_multiplier × μ[i]`. The five stages and their multipliers are loaded from `investor_profile.md`'s `financial_model.wave_stage_tilts` (defaults in `src/portfolio.py:WAVE_STAGE_TILT`). |
| **Rebalance** | Execute trades to move current portfolio weights back toward target weights. This project produces recommendations; the user does the trading. |
| **Wave thesis** | The user's belief that long technology waves drive returns: enter early in a wave (buildup, surge), trim near the crest (peak), avoid the hangover (digestion). The profile prose names the current wave (AI) and the next ones (rockets/spacecraft, robotics, engineered biology, quantum computing, nuclear fusion). |
| **`general_markets` bucket** | Catch-all wave bucket for tickers not tied to a specific technology wave (broad-market ETFs, bonds, cash, gold). Always classified `neutral` — no tilt applied. Acts as ballast for diversification rather than a wave bet. |
| **Watchlist universe** | The set of tickers in `holdings.csv`. Both the news-researcher and the optimizer operate on exactly this set: news is harvested only for these tickers, and the optimizer can only assign weight to these tickers. Adding a ticker with `shares=0` adds it to the universe without owning it; deleting a row removes it from future runs. |
