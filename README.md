# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

This Claude Code project uses AI to manage a curated watchlist of tickers. You declare your goals, constraints, and an investment thesis (namely what you think will drive future returns), then initialize a starter watchlist of tickers that you want exposure to. At each periodic rebalance the curator agent reads recent news against your thesis and evolves the watchlist by proposing adds and removes. A standard mean-variance optimizer then recommends portfolio weights across the resulting watchlist. The result accumulates into a static Plotly dashboard so you can watch the watchlist composition, the recommended weights, and the realized portfolio value evolve over time. In our experiments, this coupling of AI-driven watchlist curation with standard portfolio optimization significantly outperforms the optimizer on its own.

**Who this helps.** An investor who has a thesis about where markets are going but not enough time to track the news, or who wants help optimizing a portfolio. This demo helps that investor move from a static buy-and-hold portfolio to one that is lightly but effectively managed by AI. In the 3-year backtest below (2023 to 2026) the AI-managed portfolio returned about **+957%** against **+217%** for a buy-and-hold of the starter watchlist, roughly **3.4x** the buy-and-hold gain. Read that as one favorable wave the curator caught and held rather than a broad edge: most of the lift sits in a single position, and the whole result is in-sample. The caveats section is honest about both. The curator's job is to compound a thesis you already hold, not to invent one you don't.

Four dashboards are served from GitHub Pages:

- **[Live dashboard](https://joehahn.github.io/portfolio-wave-rider/)**: today's portfolio, its value over time, the latest recommended weights, and the asset-class and wave-bucket breakdowns.
- **[News retriever](https://joehahn.github.io/portfolio-wave-rider/retrieval_pwr.html)**: the date-clean GDELT-GKG plus Wayback news corpus that the backtest curator reads, with coverage, sources, and per-window article counts. Mechanical, no LLM.
- **[Curator backtest](https://joehahn.github.io/portfolio-wave-rider/backtest_gkg_3yr_kimi.html)**: the curator's weekly watchlist decisions replayed over 2023 to 2026, portfolio value against buy-and-hold and SPY, the watchlist Gantt, and per-wave profit and loss.
- **[Parameter sweep](https://joehahn.github.io/portfolio-wave-rider/sweep_pwr.html)**: a zero-cost sweep of the optimizer knobs (cap, `λ`, lookback), a curator-LLM comparison, and a blind, leak-free judge that scores each curator's reasoning with the market outcome hidden.

See [GLOSSARY.md](GLOSSARY.md) for the meanings of the finance terms used below (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, etc.) and [REFERENCE.md](REFERENCE.md) for project details (repo layout, code, input and output files, architecture overview, and testing instructions).

## Recent revisions

The project has evolved since its first release, and the dashboards above reflect the current design.

- **Clean news retrieval.** The backtest curator no longer reads news via live WebSearch, which leaks present-day knowledge into historical queries (a 2023 rebalance would surface 2026 "best stocks to buy" lists). It now reads a date-honest **GDELT-GKG plus Wayback** corpus: server-enforced date bounds and archived same-date article ledes, so each rebalance only sees news that existed at that time. This removes the *retrieval* leak. It does not remove the model's training-memorization leak, so only forward testing can settle that (more in the caveats).
- **A cheap, disciplined default curator.** Every backtest return is in-sample and cannot rank one curator LLM against another, so a **blind, leak-free rationale judge** scores the reasoning instead: an independent Opus grader rates each add and remove with the market outcome hidden. It found **kimi-k2.5** ties Sonnet on reasoning quality while running about 8x cheaper and 3x faster, so kimi is now the default backtest curator.
- **One unified sweep.** The four separate sweep pages are retired in favor of a single [parameter-sweep dashboard](https://joehahn.github.io/portfolio-wave-rider/sweep_pwr.html): the zero-cost optimizer-knob frontier, the curator-LLM comparison, and the blind judge, side by side.
- **Coming next, forward usage.** The Claude-Code *skills* are moving to plain Python that calls the Anthropic and OpenRouter SDKs directly, so routine runs use an API key and Claude-Code tokens are spent only during development. The aim is a single curator prompt, retriever, validator, and optimizer shared by both the backtest and the forward path, so a lesson learned on either side lands on the other.

The granular numbers (exact config, per-wave attribution, the full bias accounting) live in [REFERENCE.md](REFERENCE.md); this page keeps the visitor-level tour.

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

### 4. Install the cron jobs (required)

First make the two job scripts executable:

```bash
chmod +x scripts/cron_pull.sh scripts/cron_snapshot.sh
```

Then open your crontab:

```bash
crontab -e
```

and add these two lines, substituting your actual repository path for `/path/to/portfolio-wave-rider`:

```
# PWR: Daily (7-day) forward news pull into the corpus, 16:00 local
0 16 * * *  /path/to/portfolio-wave-rider/scripts/cron_pull.sh
# PWR: Weekday price snapshot + dashboard + review (if due), Mon-Fri 16:30 local
30 16 * * 1-5  /path/to/portfolio-wave-rider/scripts/cron_snapshot.sh
```

Verify with `crontab -l`. Works the same on macOS and Linux, and leaves any other crontab entries untouched.

- **Daily at 16:00 local** (`cron_pull.sh`, every day including weekends): the forward news pull. It runs your wave queries through Anthropic web_search and appends the raw articles to the frozen corpus under `data/forward_corpus/`. Running every day freezes each article near its publication date, which keeps the corpus clean for later forward testing.
- **Weekday at 16:30 local** (`cron_snapshot.sh`, Mon to Fri): the price snapshot, dashboard refresh, and a self-gating rebalance review. It snapshots per-ticker prices into `data/snapshots.csv`, regenerates `docs/index.html`, then runs `review --if-due`, which curates the watchlist and writes a fresh recommendation report to `data/reports/` only when a full `rebalance_period` has elapsed since the last review. The 30-minute gap after the pull means the review always reads a freshly-pulled corpus.

Both jobs append timestamped output to `data/snapshot.log`, and both tolerate failures so a web_search hiccup never blocks the price snapshot.

cron only fires while the machine is awake and does not replay missed runs. Backfill a missed price snapshot with `.venv/bin/python -m src.cli snapshot --date YYYY-MM-DD`. A missed news pull cannot be cleanly backfilled, because re-querying web_search about a past day reintroduces hindsight, so a laptop that sleeps through 16:00 leaves a gap in the corpus (recorded in the manifest at `data/forward_corpus/pulls.jsonl`).

To publish a refreshed dashboard to GitHub Pages: `git add docs/index.html && git commit -m "Refresh live dashboard" && git push`, since cron does not auto-push.

To uninstall: run `crontab -e` and delete the two lines ending in `cron_pull.sh` and `cron_snapshot.sh`.

## Runs

This project's portfolio-optimization activities.

### 1. initialize (once)

Run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs in `investor_profile.md`. The result is a "beliefs in dollar form" initial baseline portfolio that is written to `data/thesis_baseline.json`.

### 2. cron to monitor ticker changes (daily)

Cron captures today's per-ticker shares and close price into `data/snapshots.csv` and updates the portfolio dashboard stored at `docs/index.html`.

### 3. update watchlist and optimize portfolio (monthly, quarterly, etc.)

Run `/review-portfolio` in Claude Code. The cadence is declared in `investor_profile.md` under `financial_model.rebalance_period` (`monthly` / `quarterly` / `semi_annual` / `annual`); how often you actually invoke the skill is up to you. The `rebalance_period` setting also determines the curator's news-lookback window on each call. Each call's window is anchored on that day's date, so running more often than the declared cadence (e.g. running daily under `monthly`) gives you a rolling 30-day window that overlaps heavily between consecutive runs. Each run: the curator reads recent news against your wave thesis and proposes adds and removes against the current watchlist; the optimizer then recomputes weights across the updated watchlist; the resulting report is written to `data/reports/<date>-review-portfolio.md`. Read the report to see the curator's adds and removes this period and any conflicts where the optimizer wanted something your profile forbids.

Note that recommendations do not execute trades, they only append optimizer output to `data/recommendations.csv`. To act on a recommendation, execute trades in your brokerage and then edit `holdings.csv` so the next daily snapshot picks up the new share counts.

### 4. run the curator backtest (anytime)

Run the curator backtest (`scripts/backtest_sdk.py`, invoked today by the `/run-backtest` skill and being migrated to a plain CLI call). It builds the date-clean GKG plus Wayback news pool for each missing rebalance, evolves the watchlist week by week against your wave thesis via the curator LLM, optimizes the portfolio at each rebalance, measures the lift over a buy-and-hold strategy, and regenerates the dashboard at `docs/backtest_gkg_3yr_kimi.html`.

At each weekly rebalance the curator reads the date-bounded news pool as of that date and proposes adds and removes, then the optimizer recomputes weights for whatever watchlist results, repeated across the window. Compare your run to ours at [our curator backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_gkg_3yr_kimi.html): about **+957%** over the 3-year window against **+217%** buy-and-hold, roughly **3.4x**, with the honest caveats spelled out below.

### 5. sweep the settings (anytime)

The four old per-parameter sweep pages are retired. Everything now lives on one [parameter-sweep dashboard](https://joehahn.github.io/portfolio-wave-rider/sweep_pwr.html), built by `scripts/build_sweep_dashboard.py`. It has three parts:

- **Optimizer-knob frontier.** The optimizer settings (`concentration_cap`, risk aversion `λ`, and the price lookback) only touch the mean-variance replay, not the curator, so the entire grid is a **zero-cost** local re-solve on a fixed set of curations. No LLM tokens, no news re-fetch. The dashboard ranks every config by Information Ratio and flags the current one.
- **Curator-LLM comparison.** Each candidate model reads the same news pools at the same config, so the only variable is the curator. This is where the cheap-workhorse choice (kimi) was made.
- **Blind rationale judge.** The leak-free scoring described in Recent revisions.

`max_watchlist_size` is swept separately (via the `/sweep-max-watchlist-size` skill) because it shapes the curator's *decisions*, so each value needs its own set of curator calls rather than a free re-solve. See [REFERENCE.md](REFERENCE.md) for what each knob does and how the sweep is run.

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
- **Curator-driven adds and removes.** At each `/review-portfolio`, the curator can append new rows (always at `shares=0`) and delete rows for tickers it wants to drop. The validator blocks removes for tickers with `shares > 0`, so you must liquidate the live position in your brokerage first and zero out the row, then a future `/review-portfolio` can complete the remove. The full audit trail of applied changes lives in `data/curation_history.csv`.
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

subject to ∑ᵢ wᵢ = 1 (weights sum to one) and 0 ≤ wᵢ ≤ concentration_cap. The first term `μᵀw` is the portfolio's expected return (the weighted average of per-ticker expected returns); the second term `wᵀΣw` is the portfolio's return variance, scaled by `λ` to act as a risk penalty. `μ` is the per-ticker expected-return vector, computed as the annualized mean of daily log returns over a 0.5y price-history lookback set in `investor_profile.md`. `Σ` is the ticker × ticker covariance matrix estimated over the same window. `w` is the weight vector the optimizer is solving for. `λ` (risk aversion) trades expected return against variance:

- `λ → 0`: the solution favors high-return tickers, which also tend to have greater variability.
- `λ = 0.5`: a moderate setting that leans toward higher-reward tickers while keeping a real variance penalty. This is this project's default.
- `λ ≫ 1`: the variance penalty dominates, so the solution tends toward a low-variance portfolio that is heavy in cash and bonds.

This is the standard Markowitz mean-variance formulation (Markowitz 1952, *Portfolio Selection*, Journal of Finance 7:77-91), which is the textbook starting point for portfolio construction because it captures the central return-vs-risk tradeoff in a single closed-form quadratic expression. See [GLOSSARY.md](GLOSSARY.md) for the full definitions.

## Main findings

This project reads business news against a user's stated investment thesis, derives a curated watchlist from it, and hands that watchlist to a standard mean-variance optimizer for weighting at each rebalance. The AI's job is watchlist composition only, and the financial model turns the watchlist into weights. The published backtest runs weekly from May 2023 to May 2026 (about 3 years, 157 rebalances), starting from an equal-weight `[AAPL, MSFT, GOOGL, NVDA, SPY]` buy-and-hold investor who is too busy to track the news and revise the portfolio. We know such investors exist because the author is one.

The curator here is kimi-k2.5 reading the date-clean GKG pool, and it is disciplined. Over 157 weeks it made just four swaps, every ticker real and US-listed, and it held the line the rest of the time. It kept NVDA through the AI boom, added Rocket Lab (RKLB) on a mid-2024 Space Force catalyst and rode it, opened a nuclear slot with Constellation Energy (CEG) on the Microsoft / Three Mile Island restart, played defense with Lockheed (LMT), and re-added Google as a quantum name on the Willow-chip breakthrough. The optimizer then concentrated into whatever was running, and the book ends **RKLB 60% / NVDA 40%**.

**Total return over the window (May 2023 to May 2026, about 3 years):**

| Strategy | Total return |
|---|---|
| Curator (kimi; cap 0.8, `λ` 2.0, 30-day lookback, weekly) | **+957%** (about +120%/yr, 29% max drawdown) |
| Buy-and-hold (equal-weight starter, includes NVDA) | +217% |
| SPY benchmark | +85% |

The curator beat the buy-and-hold investor by about **+740 percentage points**, or **3.4x** its gain.

**Where the return comes from, and why to distrust it.** Rocket Lab alone is roughly 71% of the gain. That is the headline and the caveat at once: this is one favorable wave the curator caught and held, not a broad-based edge, and a single winning bet (n=1) cannot separate skill from luck. Worse, the whole result is in-sample. The clean GKG plus Wayback retriever removes the *retrieval* leak, so the curator only ever saw period-correct news, but the curator is an LLM whose training postdates the window, so it may simply remember which 2023-to-2026 names won. The backtest is therefore a hindsight-tinted upper bound, not a clean out-of-sample result. The only honest test is **forward testing**: hold the config fixed and measure realized performance on quarters that postdate the model's training cutoff. That is the next phase of this project.

See the [curator backtest dashboard](https://joehahn.github.io/portfolio-wave-rider/backtest_gkg_3yr_kimi.html) for the full picture, and [REFERENCE.md](REFERENCE.md) for the exact config, the per-wave attribution, the safe-haven-anchor accounting, and the full bias discussion.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, output files, architecture overview, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
