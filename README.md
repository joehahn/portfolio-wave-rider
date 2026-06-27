# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

This Claude Code project uses AI to manage a curated watchlist of tickers. You declare your goals, constraints, and an investment thesis (namely what you think will drive future returns), then initialize a starter watchlist of tickers that you want exposure to. At each periodic rebalance the curator agent reads recent news against your thesis and evolves the watchlist by proposing adds and removes. A standard mean-variance optimizer then recommends portfolio weights across the resulting watchlist. The result accumulates into a static Plotly dashboard so you can watch the watchlist composition, the recommended weights, and the realized portfolio value evolve over time. In our experiments, this coupling of AI-driven watchlist curation with standard portfolio optimization significantly outperforms the optimizer on its own.

**Who this helps.** An investor who has a thesis about where markets are going but not enough time to track market news, or who needs help optimizing their portfolio. This demo helps such an investor pivot from a less-optimal static buy-and-hold portfolio to one that's lightly but effectively managed by AI. In the post-COVID backtest detailed below (2022–2025), the AI-managed portfolio lifted realized return by about **14.3 percentage points per year annualized** over a buy-and-hold of the starter watchlist. The curator's job is to compound a thesis you already hold, not to replace one you don't have.

Two dashboards are served from GitHub Pages:

- **[Live dashboard](https://joehahn.github.io/portfolio-wave-rider/)** — today's portfolio: realized value over time, latest recommended weights, asset-class and wave-bucket breakdowns.
- **[Curator backtest](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html)** — tests whether the curator's quarterly watchlist decisions across a post-COVID historical window (2022–2025) yield better returns than the buy-and-hold investor.

See [GLOSSARY.md](GLOSSARY.md) for the meanings of the finance terms used below (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, etc.) and [REFERENCE.md](REFERENCE.md) for project details (repo layout, code, input and output files, architecture overview, and testing instructions).

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

- `investor_profile.md`: here you declare your goals, constraints, exclusions, the wave-thesis prose, and the optimizer's settings (risk aversion, risk-free rate, lookback window, rebalance period, max watchlist size). Each field is documented with explanatory comments in `investor_profile.example.md`. Every recommendation cites lines from this file.
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

To uninstall this project's cron entry later: run `crontab -e` and delete the line ending in `cron_snapshot.sh`.

## Runs

This project's portfolio-optimization activities.

### 1. initialize (once)

Run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs in `investor_profile.md`. The result is a "beliefs in dollar form" initial baseline portfolio that is written to `data/thesis_baseline.json`.

### 2. cron to monitor ticker changes (daily)

Cron captures today's per-ticker shares and close price into `data/snapshots.csv` and updates the portfolio dashboard stored at `docs/index.html`.

### 3. update watchlist and optimize portfolio (monthly, quarterly, etc.)

Run `/review-portfolio` in Claude Code. The cadence is declared in `investor_profile.md` under `financial_model.rebalance_period` (`monthly` / `quarterly` / `semi_annual` / `annual`); how often you actually invoke the skill is up to you. The `rebalance_period` setting also determines the curator's news-lookback window on each call. Each call's window is anchored on that day's date, so running more often than the declared cadence (e.g. running daily under `monthly`) gives you a rolling 30-day window that overlaps heavily between consecutive runs. Each run: the curator reads recent news against your wave thesis and proposes adds and removes against the current watchlist; the optimizer then recomputes weights across the updated watchlist; the resulting report is written to `data/reports/<date>-review-portfolio.md`. Read the report to see the curator's adds and removes this period and any conflicts where the optimizer wanted something your profile forbids.

Note that recommendations do not execute trades — they only append optimizer output to `data/recommendations.csv`. To act on a recommendation, execute trades in your brokerage and then edit `holdings.csv` so the next daily snapshot picks up the new share counts.

### 4. run the curator backtest (anytime)

Run `/run-backtest` in Claude Code. This skill collects any missing historical news, evolves the watchlist quarter-by-quarter against your wave thesis, optimizes the portfolio at each rebalance, measures the resulting lift relative to a buy-and-hold investment strategy, and regenerates the backtest dashboard at `docs/backtest_curator.html` (open it locally in a browser to see your run).

At each quarterly rebalance the curator reads news as of the rebalance date and proposes adds and removes to the watchlist; the optimizer then recomputes portfolio weights for whatever watchlist results, repeated across the window. Then compare results of your backtest to ours at [our backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html), +25.6pp/yr annualized when compared to the buy-and-hold investor's gains.

### 5. sweep optimizer parameters (anytime)

```bash
./scripts/run_sweeps.sh
```

Reruns the backtest under different settings for `risk_aversion`, `lookback_period`, and `concentration_cap` to determine the optimal value of each.

Three overlay pages are written and published to GitHub Pages:

- **[`risk_aversion` `λ`](https://joehahn.github.io/portfolio-wave-rider/sweep_risk_aversion.html)** — default `0.33`. Small `λ` produces a portfolio concentrated in volatile but higher-reward equities; large `λ` shifts the portfolio toward cash and bonds.
- **[`lookback_period`](https://joehahn.github.io/portfolio-wave-rider/sweep_lookback.html)** — default `1.5y`. The length of the price-history window used to estimate `μ` and `Σ`. Short lookbacks chase recent momentum and react quickly to regime changes but are noisy; long lookbacks average across more market conditions and produce steadier estimates but lag turning points.
- **[`concentration_cap`](https://joehahn.github.io/portfolio-wave-rider/sweep_concentration_cap.html)** — default `0.80`. The maximum weight any single ticker can carry. Small caps force diversification across the full watchlist, smoothing returns but diluting conviction; large caps let the optimizer pile into its top picks, raising both upside and drawdown risk.

All three defaults are set in `investor_profile.md` and can be edited there.

A fourth sweep, **[max_watchlist_size](https://joehahn.github.io/portfolio-wave-rider/sweep_max_watchlist_size.html)**, is fired separately via the `/sweep-max-watchlist-size` skill. The `max_watchlist_size` parameter caps how many tickers the curator may hold in the active watchlist at one time. Every add the curator proposes past the cap is rejected unless paired with a remove, so the cap directly shapes which themes the portfolio can pursue at each rebalance. Small values force the curator to pick a few high-conviction tickers per wave bucket and rotate aggressively (sharper but more concentrated bets); large values let the watchlist grow broad enough to cover every named wave with multiple tickers each (more diversified but slower to react, and the optimizer may dilute its top picks across redundant exposures). max_watchlist_size=8 is the project default. Unlike the earlier sweeps, this one must execute as a Claude skill because the cap shapes the curator's *decisions*, so each cap requires its own quarterly portfolio-curator calls. Runtime is dominated by curator latency rather than local compute, so the wall clock (~15 min at 4-parallel batching) is roughly the same on any modern laptop.

## Acting on a recommendation

The `/review-portfolio` report ends with recommended weights, not trades. The project never touches your brokerage. To act on a recommendation:

1. Read the **Profile conflicts** and **Recommended allocation** sections of the report. The optimizer regularly produces concentrated calls (single-stock weights at the `concentration_cap`); decide which subset you actually want to execute.
2. Execute the buys and sells in your brokerage.
3. Edit `holdings.csv` with the new share counts. The validator blocks the curator from removing tickers with `shares > 0`, so liquidate before zeroing a row.
4. The next daily cron snapshot picks up the new positions and the dashboard catches up.

You can also do nothing and let the next `/review-portfolio` produce a fresh recommendation. The split between recommendation and execution is intentional so you can review, override, or ignore each call.

## How `holdings.csv` shapes outcomes

`holdings.csv` is the watchlist that the curator and the optimizer operate on.

- **Optimizer eligibility.** The optimizer cannot assign weight to a ticker that isn't in the file.
- **`shares = 0` is meaningful.** A row with zero shares puts the ticker on the watchlist, which allows the optimizer to assign nonzero weights and the dashboards to track that ticker's price without requiring ownership that position.
- **Curator-driven adds and removes.** At each `/review-portfolio`, the curator can append new rows (always at `shares=0`) and delete rows for tickers it wants to drop. The validator blocks removes for tickers with `shares > 0` — you must liquidate the live position in your brokerage first and zero out the row, then a future `/review-portfolio` can complete the remove. The full audit trail of applied changes lives in `data/curation_history.csv`.
- **Manual edits still work.** Append `<TICKER>,0` to add by hand; delete a row to remove by hand (subject to the same liquidate-first rule for live positions).

## How this project utilizes Claude Skills and Subagents

This project uses two kinds of Claude Code primitives:

- A **Skill** is a slash command that delivers a sequence of tasks. Typing `/review-portfolio` delivers the steps described in [`.claude/skills/review-portfolio/SKILL.md`](.claude/skills/review-portfolio/SKILL.md), which: launches the curator subagent, applies the surviving adds and removes to `holdings.csv`, runs the portfolio optimizer, calls the report-writer, and refreshes the dashboard. Inspect this project's four skills, [`/initialize-portfolio`](.claude/skills/initialize-portfolio/SKILL.md), [`/review-portfolio`](.claude/skills/review-portfolio/SKILL.md), [`/run-backtest`](.claude/skills/run-backtest/SKILL.md), & [`/sweep-max-watchlist-size`](.claude/skills/sweep-max-watchlist-size/SKILL.md), to see what they do in detail.
- A **Subagent** uses an LLM with narrow lists of allowed tools. Each subagent manages its own context window so the work it does (news reading, report writing) doesn't crowd the main conversation. Calls are fire-and-forget: they spawn, run, return one message back, then the subagent's context disappears. Any state that needs to persist across calls is stored as data in files written to the `data/` directory. This project has two subagents: the [`watchlist-curator`](.claude/agents/watchlist-curator.md) that reads the news and proposes portfolio adds and removes, and the [`report-writer`](.claude/agents/report-writer.md) that writes the monthly report after synthesizing the curator's output.

Other project tasks (portfolio optimization, price fetching, validation, dashboard rendering) are deterministic and handled by Python code in [`src/portfolio.py`](src/portfolio.py) and [`src/cli.py`](src/cli.py). The judgment pieces (which news matters, investment waves are currently active, and what to write in the report) are what an LLM is good at and is challenging to encode as fixed logic. So Python is used for the deterministic work and an LLM for the judgment calls, with each part staying small and easily understood.

## How the watchlist-curator works

The curator is the AI subagent that decides which tickers belong on the watchlist, and it executes when you call `/review-portfolio`. Its job is composition only: read the news, decide what to add and what to remove against the current watchlist. It does not propose weights or generate any forecasts. Instead it manages the list of tickers that the optimizer can choose from, doing so in a way that is informed by current news and aligned with your investing thesis.

On each call the curator:

1. Reads the wave thesis from `investor_profile.md` and the current watchlist from `holdings.csv`.
2. Searches recent news against the named waves, preferring sources listed in `news_sources.md`.
3. Proposes at most 3 adds and 3 removes, each cited with 2-4 dated news items.
4. Returns one JSON payload.

Python code then validates the payload: US-listed only, listing-date check via yfinance, post-change watchlist size within `max_watchlist_size`, no double-adds, no stale removes, no removes of tickers with live share counts. Only the changes that survive validation touch `holdings.csv`.

This splitting is intentional. The mean-variance solution finds the portfolio that optimizes the objective function (which is detailed further below), while the LLM handles tasks that require a judgement call. The curator agent is detailed in [`.claude/agents/watchlist-curator.md`](.claude/agents/watchlist-curator.md).

## How the optimizer works

The optimizer used here selects a portfolio that maximizes the mean-variance objective function:

```
μᵀw − λ·wᵀΣw
```

subject to ∑ᵢ wᵢ = 1 (weights sum to one) and 0 ≤ wᵢ ≤ concentration_cap. The first term `μᵀw` is the portfolio's expected return (the weighted average of per-ticker expected returns); the second term `wᵀΣw` is the portfolio's return variance, scaled by `λ` to act as a risk penalty. `μ` is the per-ticker expected-return vector, computed as the annualized mean of daily log returns over a 1.5y price-history lookback set in `investor_profile.md`. `Σ` is the ticker × ticker covariance matrix estimated over the same window. `w` is the weight vector the optimizer is solving for. `λ` (risk aversion) trades expected return against variance:

- `λ → 0`: the solution favors high-return tickers, which also tend to have greater variability.
- `λ = 0.33`: a return-tilted setting that leans into higher-reward tickers while still penalizing variance. This is this project's default.
- `λ ≫ 1`: the variance penalty dominates, so the solution tends toward a low-variance portfolio that is heavy in cash and bonds.

This is the standard Markowitz mean-variance formulation (Markowitz 1952, *Portfolio Selection*, Journal of Finance 7:77-91), which is the textbook starting point for portfolio construction because it captures the central return-vs-risk tradeoff in a single closed-form quadratic expression. See [GLOSSARY.md](GLOSSARY.md) for the full definitions.

## Main findings

This project builds an AI assistant that reads business news against a user's stated investment thesis, derives a curated watchlist of tickers from it, and then hands that watchlist to a standard mean-variance optimizer for weighting at each rebalance. The AI's job is watchlist composition only, while a simple but effective financial model then turns the watchlist into portfolio weights. The published backtest covers a **post-COVID, normal-regime window (2022-03-31 → 2025-10-31, ~3.6 years)** — it deliberately drops the distorted 2020–2021 stimulus melt-up and ends just before the late-2025 Iran-war runup, so it reads as a credible "normal markets" window. The dashboard renders the same single optimizer config the live portfolio uses (λ=0.33, lookback 1.5y, cap=0.80). To measure the AI's lift, we compare its track record against a tech-minded investor whose initial portfolio is equal amounts of `[AAPL, MSFT, GOOGL, NVDA, SPY]`, a buy-and-hold investor too busy to monitor news and revise their portfolio. We know such investors exist because the author of this project is one.

NVDA is in the starter from day 0, so both the AI-managed portfolio and the buy-and-hold baseline ride it and the curator earns no credit there. The divergence comes from the curator's thematic adds and the optimizer's quarterly re-weighting. The clearest example is the rockets/spacecraft wave: the curator added Rocket Lab (RKLB) at the window's start in 2022 and held it through a multi-year, several-fold run; by 2025 the optimizer had concentrated heavily in RKLB, up to its 80% cap. The curator also rotated the watchlist as theses matured: it added defense (ITA) and robotics (BOTZ) early, opened the nuclear slot with single-name NuScale (SMR) and then swapped it for the diversified nuclear ETF (NUKZ) once NuScale's flagship project collapsed, played quantum first via IonQ (IONQ) and then trimmed it "before the crest" after the late-2024 run-up, and dropped the broad AAPL/MSFT/GOOGL/SPY names on dated catalysts (Apple's revenue slump, Microsoft's AI-margin compression) — finally opening the demographics slot with the senior-housing REIT Welltower (WELL). Final watchlist: BOTZ, GOOGL, ITA, NUKZ, NVDA, RKLB, WELL.

**Total realized return over the window (2022–2025, ~3.6 years):**

| Strategy | Return | Annualized |
|---|---|---|
| Curator (λ=0.33, lookback 1.5y, cap=0.80) | **+525.1%** | **+66.6%** |
| Buy-and-hold (equal-weight starter, includes NVDA) | +187.3% | +34.2% |
| SPY benchmark | +58.7% | +13.7% |

This is the single config the live portfolio uses; the dashboard renders the same one. It ends ~80% RKLB / 20% WELL, so most of the lift rests on the one RKLB position (see the robustness caveat below).

**The AI Curator's lift over buy-and-hold:**

| Measure | Value |
|---|---|
| Absolute (curator − buy/hold), total | +338pp |
| Absolute, annualized | +32.4pp/yr |
| Relative (curator − buy/hold) / (buy/hold) | 1.80 |

**A caveat on robustness.** This lift is not a steady, broad-based edge. The curator actually trailed the buy-and-hold portfolio for most of this window and pulled clear only in 2025, when the optimizer concentrated near the cap in Rocket Lab (ends ~80% RKLB) and RKLB ran hard. The outperformance therefore rests largely on a single late position rather than a repeatable advantage. Read it as one favorable wave the curator caught and held, not proof of a durable edge; a single winning bet (n=1) cannot separate skill from luck. There is also a look-ahead-bias / data-leakage caveat: the curator is an LLM whose training postdates the window, and live WebSearch ranks results by present-day fame, so the backtest is a **hindsight-tinted upper bound, not a clean out-of-sample result**. The way to test for overfitting is **forward testing** — hold this config fixed and measure realized performance on quarters that postdate the model's training cutoff. See [REFERENCE.md](REFERENCE.md#the-curator-backtest-post-covid-window) for the full bias accounting and the forward-testing plan.

See the [curator backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html) and the full report in `data/backtest_curator_postcovid/report.md`. Reproduce locally with the on-demand backtest from the Runs section above.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, output files, architecture overview, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
