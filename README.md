# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

A Claude Code demo for long-horizon portfolio optimization with an LLM-driven watchlist curator. You declare your goals, constraints, and a wave thesis (which technology waves you believe will drive future returns) in `investor_profile.md` and a starter watchlist of tickers in `holdings.csv`. At each monthly rebalance the watchlist-curator subagent reads recent news, proposes adds and removes against the current watchlist (validated by the Python harness against listing dates, a max-watchlist-size cap, and the profile's exclusions), and the mean-variance optimizer (`scipy.optimize`) runs on the post-change watchlist. The result accumulates into a static Plotly dashboard so you can watch the watchlist composition, the recommended weights, and the realized portfolio value evolve over time.

Two dashboards are served from GitHub Pages:

- **[Live dashboard](https://joehahn.github.io/portfolio-wave-rider/)** — today's portfolio: realized value over time, latest recommended weights, asset-class and wave-bucket breakdowns. Regenerated daily by cron.
- **[5-year curator backtest](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html)** — equity-curve race (curator vs buy-and-hold vs fixed-watchlist rebalance vs SPY) plus a Gantt timeline of watchlist composition over 4.6 years.

See [GLOSSARY.md](GLOSSARY.md) for finance and stats terms (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, max drawdown, VaR/CVaR, etc.) and [REFERENCE.md](REFERENCE.md) for the CLI flags, repo layout, output files, architecture overview, and testing instructions.

## Setup

Four steps: install Python, fill in your profile + watchlist, bootstrap the portfolio, then install cron so the dashboard accumulates history.

### 1. Install dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy templates:
cp investor_profile.example.md investor_profile.md
cp holdings.example.csv holdings.csv
```

### 2. Edit `investor_profile.md` and `holdings.csv`

- `investor_profile.md`: here you declare your goals, constraints, exclusions, asset-class targets, the wave-thesis prose, and the optimizer's settings (risk aversion `λ`, risk-free rate, lookback window, rebalance period, max watchlist size). Each field is documented with explanatory comments in `investor_profile.example.md`. Every recommendation cites lines from this file.
- `holdings.csv`: a two-column CSV (`ticker,shares`) acting as your starter watchlist. Initialize with 0 shares; the `/initialize-portfolio` skill will allocate dollars across the watchlist during its first run.

Optional: `news_sources.md`, a curated list of preferred sources grouped by technology wave. The watchlist-curator reads it if present and falls back to general WebSearch otherwise; missing is fine.

### 3. Bootstrap the portfolio

Run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist using only the qualitative inputs in `investor_profile.md`, persists the result to `data/thesis_baseline.json`, and writes a thesis-only report under `data/reports/`.

### 4. Install the daily cron job (required)

Without cron the dashboard has no daily price history to plot, so the time-series charts stay empty after step 3.

```bash
./scripts/install_cron.sh
```

That's it. The helper script appends one line to your crontab (preserving any other entries you already have) that fires `scripts/cron_snapshot.sh` Mon-Fri at 16:30 local. The script resolves its own location, so there's no `PROJ` variable to mis-edit. Re-running `install_cron.sh` is idempotent. To uninstall: `crontab -e` and delete the line containing `cron_snapshot.sh`.

Each fire of `cron_snapshot.sh` runs `snapshot` (appending a row per ticker to `data/snapshots.csv`) and then `dashboard` (regenerating `docs/index.html`). Both outputs append to `data/snapshot.log` with a timestamp so you can grep for failures. cron only fires while the machine is awake; missed runs do not auto-replay — use `--date YYYY-MM-DD` on `snapshot` to backfill a missed day. The dashboard file is git-tracked but cron does not push: `git status` will show it modified after each run, and a manual `git add docs/index.html && git commit && git push` publishes the refresh to GitHub Pages.

## Runs

Four triggers cover the portfolio's lifecycle: setup, daily price refresh, monthly review, and on-demand backtest.

- **Once, on a fresh repo** — you run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs in `investor_profile.md`. The result is a "beliefs in dollar form" initial baseline portfolio, persisted to `data/thesis_baseline.json`.
- **Daily, Mon-Fri 16:30 local** — cron captures today's per-ticker shares and close price into `data/snapshots.csv` and refreshes `docs/index.html`.
- **Monthly, you decide** — you run `/review-portfolio` in Claude Code. The watchlist-curator agent reads recent news against your wave thesis, proposes adds and removes against the current watchlist; the `curate` CLI applies validated changes to `holdings.csv` and appends an audit row to `data/curation_history.csv`; mean-variance runs on the post-change watchlist; and a profile-aware report is written under `data/reports/`. Every report has a **Profile conflicts** section that flags when the optimizer wanted something the profile forbids and a **Watchlist changes** section that lists what the curator added, removed, or had rejected by the validator. Those are the two sections to read first.
- **On demand** — `python -m src.cli backtest` runs a math-only walk-forward replay over a 12-month window of the fixed watchlist. `python -m src.cli backtest --curator-runs-dir <dir>` replays a pre-collected directory of curator JSON payloads through the same optimizer and emits two extra baselines (fixed-watchlist same cadence, buy-and-hold of starter) on the same dashboard. The curator-driven 5-year run under `data/curator_runs/5y-quarterly/` is committed and reproducible.

Recommendations (from `recommend` and `/review-portfolio`) do not execute trades — they only append optimizer output to `data/recommendations.csv`. To act on a recommendation, execute trades in your brokerage and then edit `holdings.csv` so the next daily snapshot picks up the new share counts.

## Operations

- Daily: nothing. The cron job appends a row per ticker to `data/snapshots.csv` and refreshes the local copy of `docs/index.html`.
- Whenever you want to publish the cron-refreshed dashboard: `git add docs/index.html && git commit -m "Refresh live dashboard" && git push`. GitHub Pages serves `docs/` from `main`, so the push goes live within a minute.
- Monthly: run `/review-portfolio` in Claude Code. Read the report (especially **Profile conflicts** and **Watchlist changes**), decide on rebalances, execute trades in your brokerage, then update `holdings.csv`.
- After trading: edit `holdings.csv` to reflect new share counts. The next snapshot picks up the new positions.
- Anytime: open `docs/index.html` in a browser for the local view, or visit the public-demo URL.

## How `holdings.csv` shapes a run

`holdings.csv` is the watchlist that the curator and the optimizer operate on.

- **Optimizer eligibility.** The optimizer cannot assign weight to a ticker that isn't in the file.
- **`shares = 0` is meaningful.** A row with zero shares puts the ticker on the watchlist (the optimizer can assign weight, the dashboard tracks its price) without representing an actual position.
- **Curator-driven adds and removes.** At each `/review-portfolio`, the watchlist-curator can append new rows (always at `shares=0`) and delete rows for tickers it wants to drop. The validator blocks removes for tickers with `shares > 0` — you must liquidate the live position in your brokerage first and zero out the row, then a future `/review-portfolio` can complete the remove. The full audit trail of applied changes lives in `data/curation_history.csv`.
- **Manual edits still work.** Append `<TICKER>,0` to add by hand; delete a row to remove by hand (subject to the same liquidate-first rule for live positions). Historical rows in `data/snapshots.csv` and `data/recommendations.csv` are not pruned (so old charts still render correctly), but no new rows accumulate for a removed ticker.

## How the optimizer works

The optimizer used here selects a portfolio that maximizes the mean-variance objective function:

```
μᵀw − λ·wᵀΣw
```

subject to ∑ᵢ wᵢ = 1 (weights sum to one) and 0 ≤ wᵢ ≤ concentration_cap. The two inputs estimated from price history are `μ` (the per-ticker expected-return vector — annualized mean of daily log returns over the **price-history lookback**, default 1.3y, set in `investor_profile.md`) and `Σ` (the ticker × ticker covariance matrix — variances on the diagonal, pairwise covariances off-diagonal). `w` is the weight vector the optimizer is solving for. `λ` (risk aversion) trades expected return against variance and is the **only** investor-facing knob on that tradeoff: when `λ = 0` the variance term drops out and the optimizer maximizes pure expected return, piling weight into the highest-`μ` tickers up to the concentration cap; as `λ` grows the variance penalty dominates and the solution approaches min-variance (heavy in bonds and cash). Sliding `λ` traces the whole efficient frontier — well-known special cases like the max-Sharpe tangent point or min-variance are just specific λ values on the same curve. The profile default `λ = 1` is a middle-of-the-frontier point. See [GLOSSARY.md](GLOSSARY.md) for the full definitions.

## Things to watch

- **Sample bias.** The realized Sharpe on a 1-2 year window is usually optimistic vs the forward-looking distribution. Returns are non-stationary; vol clusters; means are noisy.
- **Estimation error in `μ`.** Mean-variance amplifies small errors in the expected-return estimate. A weight pinned at the concentration cap is often a symptom of estimation noise, not a real signal. This is the well-known Markowitz blow-up. Run `python -m src.cli backtest` to walk the optimizer forward on real historical data; if the weight-stability L1 metric is small (~0.02 means weights barely move week to week) the estimation noise isn't driving the solution.
- **Curator hindsight risk in backtests.** When the curator runs against a historical as-of date, its job is to use only information available at that date. The agent spec enforces this with a persona reset, WebSearch `before:` filters, a suppression list of post-date events, and a self-critique pass — but the discipline is best-effort, not airtight. Sample a few of the cited evidence URLs against their dates before trusting a backtest's headline number.
- **Numbers come from Python.** If a figure in a report did not come from `src.cli`, that's a bug. The LLM is allowed to write prose; it is not allowed to do arithmetic.

## Headline result

Over 4.6 years (Sept 2021 → Apr 2026) starting from a 2021-tech-savvy portfolio (AAPL, MSFT, GOOGL, SPY, AGG), the watchlist-curator agent (20 quarterly LLM calls) lifted realized return to **+135.5%** vs. **+103.7%** for buy-and-hold of the day-0 starter, **+80.2%** for a fixed-watchlist same-cadence rebalance, and **+78.2%** for SPY. See the [curator backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html) and the full report in `data/backtest_curator_5y/report.md`.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, output files, architecture overview, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
