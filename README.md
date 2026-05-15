# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

A Claude Code demo that uses AI to manage and optimize a long-horizon investment portfolio. You declare your goals, constraints, and a wave thesis (namely which investment waves you think will drive future returns), then initialize a starter watchlist of tickers you already want exposure to. At each periodic rebalance the curator agent reads recent news against your thesis and proposes adds and removes against the current watchlist; the mean-variance optimizer then recommends portfolio weights for whatever watchlist results. The result accumulates into a static Plotly dashboard so you can watch the watchlist composition, the recommended weights, and the realized portfolio value evolve over time.

**Who this helps.** An investor who has a thesis about where markets are going but not enough time to track news ticker by ticker. This demo helps such an investor pivot from a less-optimal static buy-and-hold portfolio to one that's lightly managed by AI. In the 5-year backtest summarized near the bottom of this README, the AI-managed portfolio lifted realized return by about **6 percentage points per year** over the same optimizer running on an unchanging starter watchlist (~55pp total over 4.6 years), and by about 7pp/year over SPY. Past performance is not predictive; the curator's job is to compound a thesis you already hold, not to replace one you don't have.

Two dashboards are served from GitHub Pages:

- **[Live dashboard](https://joehahn.github.io/portfolio-wave-rider/)** — today's portfolio: realized value over time, latest recommended weights, asset-class and wave-bucket breakdowns. Regenerated daily by cron.
- **[5-year curator backtest](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html)** — equity-curve race (curator vs buy-and-hold vs fixed-watchlist rebalance vs SPY) plus a Gantt timeline of watchlist composition over 4.6 years.

See [GLOSSARY.md](GLOSSARY.md) for finance and stats terms (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, max drawdown, VaR/CVaR, etc.) and [REFERENCE.md](REFERENCE.md) for the CLI flags, repo layout, output files, architecture overview, and testing instructions.

## Setup

Four steps: install dependencies, edit the configuration files to your taste, bootstrap your initial portfolio, and install the daily cron job.

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

`news_sources.md` is pre-populated with a curated list of preferred news sources (Bloomberg, Reuters, company newsrooms, SEC filings, etc.) grouped by your profile's waves. The curator searches these domains first and falls back to open WebSearch otherwise. Tailor to your own taste — add sources you trust, drop ones that paywall heavily or go off-topic.

### 3. Bootstrap the portfolio

Run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist using only the qualitative inputs in `investor_profile.md`, persists the result to `data/thesis_baseline.json`, and writes a thesis-only report under `data/reports/`.

### 4. Install the daily cron job (required)

```bash
./scripts/install_cron.sh
```

Appends one crontab line (works on macOS and Linux) so Mon-Fri at 16:30 local, `scripts/cron_snapshot.sh` fires: snapshots today's per-ticker prices into `data/snapshots.csv` and regenerates `docs/index.html`. Output goes to `data/snapshot.log` with timestamps.

cron doesn't replay missed runs, so if your laptop was asleep at 16:30, run `./scripts/cron_snapshot.sh` manually to fill in the missing day.

To publish a refreshed dashboard to GitHub Pages: `git add docs/index.html && git commit && git push` (cron doesn't auto-push).

## Runs

Four triggers cover the portfolio's lifecycle: setup, daily price refresh, monthly review, and on-demand backtest.

- **Once, on a fresh repo** — you run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs in `investor_profile.md`. The result is a "beliefs in dollar form" initial baseline portfolio, persisted to `data/thesis_baseline.json`.
- **Daily, Mon-Fri 16:30 local** — cron captures today's per-ticker shares and close price into `data/snapshots.csv` and refreshes `docs/index.html`.
- **Monthly, quarterly, or whatever cadence you set in your profile** — you run `/review-portfolio` in Claude Code. The cadence is declared in `investor_profile.md` under `financial_model.rebalance_period` (`monthly` / `quarterly` / `semi_annual` / `annual`); how often you actually invoke the skill is up to you. Each run: the curator reads recent news against your wave thesis, proposes adds and removes against the current watchlist; the `curate` CLI applies validated changes to `holdings.csv` and appends an audit row to `data/curation_history.csv`; mean-variance runs on the post-change watchlist; and a profile-aware report is written under `data/reports/`. Every report has a **Profile conflicts** section that flags when the optimizer wanted something the profile forbids and a **Watchlist changes** section that lists what the curator added, removed, or had rejected by the validator. Those are the two sections to read first.
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
- **Curator-driven adds and removes.** At each `/review-portfolio`, the curator can append new rows (always at `shares=0`) and delete rows for tickers it wants to drop. The validator blocks removes for tickers with `shares > 0` — you must liquidate the live position in your brokerage first and zero out the row, then a future `/review-portfolio` can complete the remove. The full audit trail of applied changes lives in `data/curation_history.csv`.
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

Over 4.6 years (Sept 2021 → Apr 2026) starting from a 2021-tech-savvy portfolio (AAPL, MSFT, GOOGL, SPY, AGG), the curator (20 quarterly LLM calls) lifted realized return to **+135.5%** vs. **+103.7%** for buy-and-hold of the day-0 starter, **+80.2%** for a fixed-watchlist same-cadence rebalance, and **+78.2%** for SPY. See the [curator backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html) and the full report in `data/backtest_curator_5y/report.md`.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, output files, architecture overview, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
