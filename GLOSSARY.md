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

## Terms

| Term | Plain definition |
|---|---|
| **Ticker** | Symbol identifying a security: `AAPL` is Apple, `AGG` is an aggregate bond ETF, `IBIT` is a spot-Bitcoin ETF. |
| **ETF** | Exchange-traded fund. A packaged basket of underlying securities that trades like a single stock. |
| **Long-only** | All portfolio weights `w_i ≥ 0`. No short selling, no leverage. |
| **Mean-variance optimization** | Markowitz framework. Convex quadratic program: pick weights `w` that minimize `wᵀΣw` (variance) subject to `wᵀμ = target` (target return) and `Σw = 1`, `w ≥ 0`. We use `scipy.optimize.minimize(SLSQP)`. |
| **Risk-free rate (`r_free`)** | The return you can earn with effectively zero risk by parking money in short-term US Treasuries or a money-market fund. Default `0.04` (4% annualized, roughly a 1-year Treasury yield). Set in `investor_profile.md` under `financial_model.risk_free_rate`; overridable per invocation via `--risk-free-rate` on `analyze`, `recommend`, and `backtest`. |
| **Sharpe ratio** | Signal-to-noise on returns: `(E[r] − r_free) / σ`. The numerator is the **excess return** (return above what's free). The denominator is the standard deviation of returns. You only get credit for the risk-bearing part of `E[r]`. Higher is better; values above 1 are good for a long-horizon portfolio. |
| **Risk aversion (`λ`)** | Scalar coefficient in the mean-variance utility `μᵀw − λ·wᵀΣw`. The single investor-facing knob on the return/variance tradeoff. Set in `investor_profile.md` under `financial_model.risk_aversion`; overridable via `--risk-aversion` on `analyze`, `recommend`, and `backtest`. Small `λ` (≤ 1) favors expected return → equity-heavy portfolios; large `λ` (≥ 5) favors variance reduction → bond/cash-heavy. Sliding `λ` traces the entire efficient frontier — special cases like `min_variance` (λ → ∞) and the max-Sharpe tangent point (some specific finite λ depending on `r_free`) are points on the same curve, so a single dial covers them all. |
| **Max drawdown** | Worst observed peak-to-trough decline of cumulative value: `min_t (V_t − cummax(V)_t) / cummax(V)_t`. A max drawdown of `-0.30` means at some point the portfolio lost 30% from a prior high. |
| **VaR_α** | Value-at-risk: the α-quantile of the daily return distribution. `VaR_0.05 = -0.02` means there's a 5% chance of losing more than 2% on a given day (under the empirical distribution). |
| **CVaR_α** | Conditional VaR: the expected return conditioned on being below `VaR_α`. Tail-loss expectation. |
| **Concentration cap** | Box constraint on the optimizer: `w_i ≤ max_weight` for every asset. Profile default 0.25. |
| **Asset class** | Coarse bucket: equities, bonds, precious metals, cash. |
| **Asset-class drift** | Deviation of recommended weights summed by class from the user's declared target percentages. Reported but not enforced. |
| **Rebalance** | Execute trades to move current portfolio weights back toward target weights. This project produces recommendations; the user does the trading. |
| **Wave thesis** | The user's belief that long, structurally-driven shifts ("waves") drive returns. Most named waves are technology-driven (the AI wave currently, and next ones like rockets/spacecraft, robotics, engineered biology, quantum computing, nuclear), but the framing also covers non-technology waves: geopolitical realignments (e.g., regional wars driving energy/shipping repricings), demographic shifts (aging populations driving healthcare/automation demand), commodity cycles, and regulatory inflections. The profile prose is where the user names whichever waves they want exposure to. |
| **Thesis allocation** | The user's wave thesis expressed as concrete dollar amounts per ticker, with no math involved (no optimizer, no Sharpe). Set once by `/initialize-portfolio`, persisted to `data/thesis_baseline.json`. |
| **`general_markets` bucket** | Catch-all wave bucket for tickers not tied to a specific wave thesis (broad-market ETFs, bonds, cash, gold). Acts as ballast for diversification rather than a wave bet. |
| **Watchlist universe** | The set of tickers in `holdings.csv`. The optimizer operates on exactly this set and can only assign weight to these tickers. Adding a ticker with `shares=0` adds it to the universe without owning it; deleting a row removes it from future runs. |
| **Watchlist curator** | LLM subagent (`watchlist-curator`) that at each rebalance reads recent news, proposes adds and removes to the watchlist universe, and emits one JSON object with rationale + dated evidence per decision. Replaces the previously-attempted "wave-stage tilt" design (which adjusted μ by per-wave cycle-stage multipliers and didn't beat baselines in backtest). |
| **Curation** | The verb form of the curator's job — proposing adds and removes against the current watchlist. One curation call per rebalance period. The audit trail of applied changes lives in `data/curation_history.csv`. |
| **Rebalance period** | Cadence at which the curator runs and the optimizer rebalances: `monthly`, `quarterly`, `semi_annual`, or `annual`. Set in `investor_profile.md` under `financial_model.rebalance_period`. The live `/review-portfolio` skill is designed for monthly; the 5y curator backtest used quarterly. |
| **As-of-date discipline** | Lookahead-bias guard for the watchlist-curator when run with a historical as-of date (backtest mode). Seven mitigations: persona reset, WebSearch `before:` filters, suppression list of post-date events, grounding rule (every non-neutral call needs a dated bullet), forbidden-phrase blocklist, self-critique pass, calibration probe. Live runs skip all of this — the agent should use current information. |
