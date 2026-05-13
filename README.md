# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-08 <br>
**branch:** main

A Claude Code demo for long-horizon portfolio optimization. You declare your goals, constraints, and a wave thesis (which technology waves you believe will drive future returns) in `investor_profile.md`, as well as a watchlist of tickers in `holdings.csv`. The system then pulls the last few years of price history via yfinance, runs a mean-variance optimizer (scipy.optimize) over those tickers, and recommends weights that maximize risk-adjusted return subject to your concentration cap (drift from your asset-class targets is reported in each run's Profile conflicts section, not enforced). The result accumulates into a static Plotly dashboard so you can watch the recommended weights and the realized portfolio value evolve over time.

> **Status: in-flight rebuild.** `main` is being scaffolded for a new watchlist-curator design. The previous wave-stage-tilt design (an LLM that classified each technology wave's cycle stage and tilted μ accordingly) didn't survive 5-year backtests — postmortem in FINDINGS.md on the [`5y-backtest`](https://github.com/joehahn/portfolio-wave-rider/tree/5y-backtest) branch. The working 1-year demo lives on [`1y-baseline`](https://github.com/joehahn/portfolio-wave-rider/tree/1y-baseline) and is what GitHub Pages serves. On `main` the `/review-portfolio` and `/run-backtest` slash commands are removed pending the curator rebuild; `backtest`, `analyze`, `snapshot`, `recommend`, and `dashboard` CLI subcommands still work.

**Live demo:** GitHub Pages serves from the [`1y-baseline`](https://github.com/joehahn/portfolio-wave-rider/tree/1y-baseline) branch (the last working 1-year cut of the prior design), so these links keep working while `main` is mid-rebuild:

- [Live dashboard](https://joehahn.github.io/portfolio-wave-rider/) — what the portfolio looks like today: realized value, recommended weights, asset-class and wave-bucket breakdowns.
- [12-month backtest](https://joehahn.github.io/portfolio-wave-rider/backtest.html) — walk-forward replay of the optimizer over the trailing year.
- Sweeps — same backtest with one optimizer setting varied at a time. The three pages cross-link from a small nav strip at the top of each:
  - [Lambda sweep](https://joehahn.github.io/portfolio-wave-rider/lambda_comparison.html) (risk-aversion λ)
  - [Concentration-cap sweep](https://joehahn.github.io/portfolio-wave-rider/max_weight_comparison.html) (per-ticker weight ceiling)
  - [Lookback sweep](https://joehahn.github.io/portfolio-wave-rider/lookback_comparison.html) (price-history window for μ and Σ)

See [GLOSSARY.md](GLOSSARY.md) for finance and stats terms (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, max drawdown, VaR/CVaR, etc.) and [REFERENCE.md](REFERENCE.md) for the CLI flags, repo layout, output files, architecture overview, and testing instructions.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy templates and edit:
cp investor_profile.example.md investor_profile.md
cp holdings.example.csv holdings.csv
```

The two files you maintain:

- `investor_profile.md`: here you declare your goals, constraints, exclusions, asset-class targets, the wave-thesis prose, and the optimizer's settings (objective, risk aversion, risk-free rate, lookback window). Each field is documented with explanatory comments in `investor_profile.example.md`. Every recommendation cites lines from this file.
- `holdings.csv`: a two-column CSV (`ticker,shares`) acting as your watchlist. Initialize that with 0; the `/initialize-portfolio` skill will then allocate dollars across that portfolio during its first run.

Optional: `news_sources.md`, a curated list of sources per technology wave. The next-generation watchlist-curator subagent will read this; the current state of `main` does not.

To bootstrap a fresh portfolio, run `/initialize-portfolio` in Claude Code. The recurring `/review-portfolio` cycle returns in the curator rebuild.

## Runs

On this branch the active triggers are setup, daily price refresh, and on-demand backtest. The recurring `/review-portfolio` cycle is parked for the curator rebuild.

- **Once, on a fresh repo** — you run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs described in `investor_profile.md`. This results in a "beliefs in dollar form" initial baseline portfolio.
- **Daily, Mon-Fri 16:30 local** — cron captures today's per-ticker shares and close price, and then updates the live dashboard.
- **On demand** — `python -m src.cli backtest` runs a walk-forward monthly-rebalance replay over a 12-month window. The sweep scripts (`scripts/compare_lambdas.py`, `scripts/compare_lookbacks.py`, `scripts/compare_max_weight.py`) run the same backtest with one optimizer setting varied.

Recommendations from `python -m src.cli recommend` do not execute trades — they only append optimizer output to `data/recommendations.csv`. To act on a recommendation, execute trades in your brokerage and then edit `holdings.csv` so the next daily snapshot picks up the new share counts.

## Operations

- Daily: nothing. The cron job appends a row per ticker to `data/snapshots.csv` and refreshes the local copy of `docs/index.html`.
- Whenever you want to publish the cron-refreshed dashboard: `git add docs/index.html && git commit -m "Refresh live dashboard" && git push` since cron does not auto-push. (Note that the public demo currently serves from the `1y-baseline` branch, not `main`.)
- After trading: edit `holdings.csv` to reflect new share counts. The next snapshot picks up the new positions.
- Anytime: open `docs/index.html` in a browser for the local view, or visit the public-demo URL.

### Optional: cron automation

If you want the daily snapshot to update automatically, install this cron entry. Skip if you'd rather invoke the commands by hand. Works the same on macOS and Linux:

```cron
PROJ=/path/to/portfolio-wave-rider
# Daily snapshot + dashboard refresh, Mon-Fri 16:30 local
30 16 * * 1-5  cd $PROJ && .venv/bin/python -m src.cli snapshot && .venv/bin/python -m src.cli dashboard >> data/snapshot.log 2>&1
```

Each cron call refreshes the local copy of `docs/index.html` (the dashboard CLI's default `--out`). The file is git-tracked but cron does not push — `git status` will show it modified after each run, and a manual `git add docs/index.html && git commit && git push` publishes the refresh.

Install with `crontab -e` and paste. Adjust `PROJ` to your clone path. Verify with `crontab -l`. cron only fires while the machine is awake; missed runs do not auto-replay. Use `--date YYYY-MM-DD` on `snapshot` to backfill a missed day.

## How `holdings.csv` shapes a run

`holdings.csv` is the watchlist that the optimizer operates on.

- **Optimizer eligibility.** The optimizer cannot assign weight to a ticker that isn't in the file.
- **`shares = 0` is meaningful.** A row with zero shares puts the ticker on the watchlist (the optimizer can assign weight, the dashboard tracks its price) without representing an actual position. Use this when researching a candidate before buying, or when you want price-only history for context.
- **To add a ticker:** append a row `<TICKER>,0` to `holdings.csv`. The next run picks it up automatically — no other config changes needed.
- **To remove a ticker:** delete the row. Subsequent runs skip it. The historical rows in `data/snapshots.csv` and `data/recommendations.csv` are not pruned (so old charts still render correctly), but no new rows accumulate.

## How the optimizer works

The optimizer used here selects a portfolio that maximizes the mean-variance objective function:

```
μᵀw − λ·wᵀΣw
```

subject to ∑ᵢ wᵢ = 1 (weights sum to one) and 0 ≤ wᵢ ≤ concentration_cap. The two inputs estimated from price history are `μ` (which is the per-ticker expected-return vector i.e. the annualized mean of daily log returns over the **price-history lookback**, default 1.3y, set in `investor_profile.md`) and `Σ` (the ticker × ticker covariance matrix — variances on the diagonal, pairwise covariances off-diagonal). `w` is the weight vector the optimizer is solving for, and `λ` (risk aversion) trades expected return against variance: when `λ = 0` the variance term drops out and the optimizer maximizes pure expected return, piling weight into the highest-`μ` tickers up to the concentration cap; as `λ` grows the variance penalty dominates and the solution approaches min-variance (heavy in bonds and cash). In our lambda-sweep experiments `λ = 1` gives the best realized Sharpe; that value is set in `investor_profile.md` as `risk_aversion: 1.0`. An alternative objective is max-Sharpe — `(μᵀw − r_free) / √(wᵀΣw)` — but it picks just one point on the same efficient frontier the mean-variance sweep traces out. See [GLOSSARY.md](GLOSSARY.md) for the full definitions.

## Things to watch

- **Sample bias.** The realized Sharpe on a 1-2 year window is usually optimistic vs the forward-looking distribution. Returns are non-stationary; vol clusters; means are noisy.
- **Estimation error in `μ`.** Mean-variance amplifies small errors in the expected-return estimate. A weight pinned at the concentration cap is often a symptom of estimation noise, not a real signal. This is the well-known Markowitz blow-up. Run `python -m src.cli backtest` to walk the optimizer forward on real historical data; if the weight-stability L1 metric is small (~0.02 means weights barely move week to week) the estimation noise isn't driving the solution.
- **Numbers come from Python.** If a figure in a report did not come from `src.cli`, that's a bug. The LLM is allowed to write prose; it is not allowed to do arithmetic.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, output files, architecture overview, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
