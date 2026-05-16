# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

This Claude Code demo uses AI to manage and optimize a long-horizon investment portfolio. You declare your goals, constraints, and an investment thesis (namely what you think will drive future returns), then initialize a starter watchlist of tickers you already want exposure to. At each periodic rebalance the curator agent reads recent news against your thesis and proposes adds and removes against the current watchlist; the mean-variance optimizer then recommends portfolio weights for whatever watchlist results. The result accumulates into a static Plotly dashboard so you can watch the watchlist composition, the recommended weights, and the realized portfolio value evolve over time.

**Who this helps.** An investor who has a thesis about where markets are going but not enough time to track news ticker by ticker. This demo helps such an investor pivot from a less-optimal static buy-and-hold portfolio to one that's lightly managed by AI. In the 5-year backtest detailed below, the AI-managed portfolio lifted realized return by about **13 percentage points per year** over a buy-and-hold of the starter watchlist (~65pp total), and by about 17pp/year over SPY. Past performance is not predictive; the curator's job is to compound a thesis you already hold, not to replace one you don't have.

Two dashboards are served from GitHub Pages:

- **[Live dashboard](https://joehahn.github.io/portfolio-wave-rider/)** — today's portfolio: realized value over time, latest recommended weights, asset-class and wave-bucket breakdowns. Regenerated daily by cron.
- **[5-year curator backtest](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html)** — equity-curve race (curator vs buy-and-hold vs SPY).

See [GLOSSARY.md](GLOSSARY.md) for finance and stats terms (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, etc.) and [REFERENCE.md](REFERENCE.md) for the CLI flags, repo layout, output files, architecture overview, and testing instructions.

## Setup

Install dependencies, edit the configuration files to your taste, bootstrap your initial portfolio, and install the daily cron job.

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

- `investor_profile.md`: here you declare your goals, constraints, exclusions, asset-class targets, the wave-thesis prose, and the optimizer's settings (risk aversion, risk-free rate, lookback window, rebalance period, max watchlist size). Each field is documented with explanatory comments in `investor_profile.example.md`. Every recommendation cites lines from this file.
- `holdings.csv`: a two-column CSV (`ticker,shares`) acting as your starter watchlist. Initialize with 0 shares; the `/initialize-portfolio` skill will then allocate dollars across the watchlist during its first run.

`news_sources.md` is pre-populated with a curated list of suggested news sources (Bloomberg, Reuters, company newsrooms, SEC filings, etc.) grouped by your profile's waves. The curator searches these domains first and falls back to open WebSearch otherwise. Tailor to your own taste: add sources you trust, drop ones that paywall heavily or go off-topic.

### 3. Bootstrap the portfolio

Run `/initialize-portfolio` in Claude Code. This converts your wave thesis and starter watchlist into a concrete day-0 dollar allocation per ticker (beliefs in dollar form, no optimizer yet) and saves it as the baseline that every future review will compare against. A narrative report of the allocation reasoning is produced alongside.

### 4. Install the daily cron job (required)

```bash
./scripts/install_cron.sh
```

Appends one crontab line (works on macOS and Linux) that fires `scripts/cron_snapshot.sh` Mon-Fri at 16:30 local. The script snapshots today's per-ticker prices into `data/snapshots.csv` and regenerates `docs/index.html`. Output goes to `data/snapshot.log` with timestamps.

cron doesn't replay missed runs, so if your desktop was off at 16:30, run `./scripts/cron_snapshot.sh` manually to fill in the missing day.

To publish a refreshed dashboard to GitHub Pages: `git add docs/index.html && git commit -m "Refresh live dashboard" && git push` since cron doesn't auto-push.

## Runs

Four activities for operating and verifying the portfolio.

### 1. initialize (once)

Run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs in `investor_profile.md`. The result is a "beliefs in dollar form" initial baseline portfolio that is written to `data/thesis_baseline.json`.

### 2. cron to monitor ticker changes (daily)

Cron captures today's per-ticker shares and close price into `data/snapshots.csv` and updates the portfolio dashboard stored at `docs/index.html`.

### 3. update watchlist and optimize portfolio (monthly, quarterly, etc.)

Run `/review-portfolio` in Claude Code. The cadence is declared in `investor_profile.md` under `financial_model.rebalance_period` (`monthly` / `quarterly` / `semi_annual` / `annual`); how often you actually invoke the skill is up to you. Each run: the curator reads recent news against your wave thesis and proposes adds and removes against the current watchlist; the optimizer then recomputes weights across the updated watchlist; the resulting report is written to `data/reports/<date>-review-portfolio.md`. Read the report to see the curator's adds and removes this period and any conflicts where the optimizer wanted something your profile forbids.

Note that recommendations do not execute trades — they only append optimizer output to `data/recommendations.csv`. To act on a recommendation, execute trades in your brokerage and then edit `holdings.csv` so the next daily snapshot picks up the new share counts.

### 4. run 5-year backtest (anytime)

Run `/run-backtest` in Claude Code. This skill collects any missing historical news, evolves the watchlist quarter-by-quarter against your wave thesis, optimizes the portfolio at each rebalance, measures the resulting lift relative to a buy-and-hold investment strategy, and regenerates the backtest dashboard at `docs/backtest_curator.html` (open it locally in a browser to see your run).

At each quarterly rebalance the curator reads news as of the rebalance date and proposes adds and removes to the watchlist; the optimizer then recomputes portfolio weights for whatever watchlist results, repeated over 5 years. Compare results of your backtest to ours at [our backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html): +162.5% total and +13pp/yr over buy-and-hold.

## How `holdings.csv` shapes outcomes

`holdings.csv` is the watchlist that the curator and the optimizer operate on.

- **Optimizer eligibility.** The optimizer cannot assign weight to a ticker that isn't in the file.
- **`shares = 0` is meaningful.** A row with zero shares puts the ticker on the watchlist, which allows the optimizer to assign nonzero weights and the dashboards to track that ticker's price without requiring ownership that position.
- **Curator-driven adds and removes.** At each `/review-portfolio`, the curator can append new rows (always at `shares=0`) and delete rows for tickers it wants to drop. The validator blocks removes for tickers with `shares > 0` — you must liquidate the live position in your brokerage first and zero out the row, then a future `/review-portfolio` can complete the remove. The full audit trail of applied changes lives in `data/curation_history.csv`.
- **Manual edits still work.** Append `<TICKER>,0` to add by hand; delete a row to remove by hand (subject to the same liquidate-first rule for live positions).

## How the optimizer works

The optimizer used here selects a portfolio that maximizes the mean-variance objective function:

```
μᵀw − λ·wᵀΣw
```

subject to ∑ᵢ wᵢ = 1 (weights sum to one) and 0 ≤ wᵢ ≤ concentration_cap. The first term `μᵀw` is the portfolio's expected return (the weighted average of per-ticker expected returns); the second term `wᵀΣw` is the portfolio's return variance, scaled by `λ` to act as a risk penalty. `μ` is the per-ticker expected-return vector, computed as the annualized mean of daily log returns over a 1.3y price-history lookback set in `investor_profile.md`. `Σ` is the ticker × ticker covariance matrix estimated over the same window. `w` is the weight vector the optimizer is solving for. `λ` (risk aversion) trades expected return against variance: at `λ → 0` the variance term drops out and weight piles into the highest-`μ` tickers up to the cap; at `λ ≫ 1` the variance penalty dominates and the solution approaches min-variance (heavy in bonds and cash). The profile default `λ = 1` sits in between. This is the standard Markowitz mean-variance formulation (Markowitz 1952, *Portfolio Selection*, Journal of Finance 7:77-91), still the textbook starting point for portfolio construction because it captures the central return-vs-risk tradeoff in a single closed-form quadratic program. See [GLOSSARY.md](GLOSSARY.md) for the full definitions.

## Things to watch

- **Sample bias.** The realized Sharpe on a 1-2 year window is usually optimistic vs the forward-looking distribution. Returns are non-stationary; vol clusters; means are noisy.
- **Estimation error in `μ`.** Mean-variance amplifies small errors in the expected-return estimate. A weight pinned at the concentration cap is often a symptom of estimation noise, not a real signal. This is the well-known Markowitz blow-up. Run `python -m src.cli backtest` to walk the optimizer forward on real historical data; if the weight-stability L1 metric is small (~0.02 means weights barely move week to week) the estimation noise isn't driving the solution.
- **Curator hindsight risk in backtests.** When the curator runs against a historical as-of date, its job is to use only information available at that date. The agent spec enforces this with a persona reset, WebSearch `before:` filters, a suppression list of post-date events, and a self-critique pass — but the discipline is best-effort, not airtight. Sample a few of the cited evidence URLs against their dates before trusting a backtest's headline number.
- **Numbers come from Python.** If a figure in a report did not come from `src.cli`, that's a bug. The LLM is allowed to write prose; it is not allowed to do arithmetic.

## Headline result

**Setup:** 5y window (Mar 2021 → Mar 2026), starter watchlist `[AAPL, MSFT, GOOGL, SPY, AGG]`, 21 quarterly curator calls.

**Total realized return over the 5 years:**

| Strategy | Return |
|---|---|
| Curator-driven | **+162.5%** |
| Buy-and-hold of day-0 starter | +97.5% |
| SPY benchmark | +75.7% |

**Curator's lift over buy-and-hold:**

| Measure | Value |
|---|---|
| Absolute (curator − buy/hold) | +65pp |
| Relative (curator − buy/hold) / (buy/hold) | +67% |

See the [curator backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html) and the full report in `data/backtest_curator_5y/report.md`. Reproduce locally with the on-demand backtest from the Runs section above.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, output files, architecture overview, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
