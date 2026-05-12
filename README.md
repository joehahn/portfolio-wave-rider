# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-08 <br>
**branch:** main

A Claude Code demo for long-horizon portfolio construction. You declare your goals, constraints, and a wave thesis (which technology waves you believe will drive future returns) in `investor_profile.md`, as well as a watchlist of tickers in `holdings.csv`. The system then pulls the last few years of price history via yfinance, runs a mean-variance optimizer (scipy.optimize) over those tickers, and recommends weights that maximize risk-adjusted return subject to your concentration cap (drift from your asset-class targets is reported in each run's Profile conflicts section, not enforced). A monthly `/review-portfolio` slash command sends two Claude subagents out for fresh news per ticker, classifies each wave's stage (buildup → surge → peak → digestion), tilts the optimizer's expected-return vector accordingly, and writes a profile-aware report. The result accumulates into a static Plotly dashboard so you can watch the recommended weights, the wave classifications, and the realized portfolio value evolve over time.

**Live demo:** [joehahn.github.io/portfolio-wave-rider](https://joehahn.github.io/portfolio-wave-rider/) (live dashboard), [/backtest.html](https://joehahn.github.io/portfolio-wave-rider/backtest.html) (12-month walk-forward backtest), and [/news.html](https://joehahn.github.io/portfolio-wave-rider/news.html) (the news bullets that drove the latest wave-stage classifications).

See [GLOSSARY.md](GLOSSARY.md) for finance and stats terms (`σ`, `μ`, `Σ`, Sharpe ratio, risk aversion `λ`, mean-variance optimization, max drawdown, VaR/CVaR, etc.) and [REFERENCE.md](REFERENCE.md) for the CLI flags, repo layout, and testing instructions.

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

- `investor_profile.md`: here you declare your goals, constraints, exclusions, asset-class targets, the wave-thesis prose, and the optimizer's mathematical model (objective, risk aversion, risk-free rate, lookback window, wave-stage tilt multipliers). Each field is documented with explanatory comments in `investor_profile.example.md`. Every recommendation cites lines from this file.
- `holdings.csv`: a two-column CSV (`ticker,shares`) acting as your watchlist. Initialize that with 0; the `/initialize-portfolio` skill will then allocate dollars across that portfolio during its first run.

Optional: `news_sources.md`, a curated list of sources per technology wave. Improves the news-researcher's signal. Missing is fine; the agent falls back to open web search.

To bootstrap a fresh portfolio, run `/initialize-portfolio` in Claude Code. Then execute `/review-portfolio` to update the portfolio recommendations. See **Runs** below for the full cadence.

## Runs

Four triggers cover the portfolio's lifecycle: setup, daily price refresh, monthly review, and on-demand backtest.

- **Once, on a fresh repo** — you run `/initialize-portfolio` in Claude Code. This distributes your starting dollars across the watchlist noted in `holdings.csv` using only the qualitative inputs described in `investor_profile.md`. The result is a "beliefs in dollar form" initial baseline portfolio.
- **Daily, Mon-Fri 16:30 local** — cron captures today's per-ticker shares and close price, and then updates the live dashboard.
- **Monthly or quarterly, you decide** — you run `/review-portfolio` in Claude Code. LLM subagents gather wave-aligned news for each ticker with 30-day lookback, classify each wave's stage, and pass those classifications to the optimizer as a tilt on expected returns. Then write a profile-aware report and refresh the live dashboard. The run also appends today's wave-stage classifications to the wave-history file (which drives the trajectory chart) and archives the full news payload for forensic re-reading. Every report includes a side-by-side table of your original `/initialize-portfolio` thesis allocation and the latest optimizer recommendation.
- **On demand, you decide** — you run `/run-backtest` in Claude Code. Walk-forward monthly-rebalance backtest of the optimizer over a 12-month window, applying the wave-stage tilts that were known at the start of each historical month. Pure Python, no LLM. Auto-renders both the local backtest dashboard and the public-demo backtest page.

**Important: `/review-portfolio` produces recommendations; it does not execute trades.** After each `/review-portfolio` run, the report lays out the recommended weights and the gap from your current holdings. To act on the recommendation you must (1) execute the trades in your brokerage, then (2) edit `holdings.csv` to reflect the new share counts so the next daily snapshot picks them up. The dashboard plots both pictures simultaneously so the gap stays visible — chart 4 is the *recommended* portfolio (prescriptive: what the optimizer wants you to hold), chart 10 is the *actual* portfolio (descriptive: what you currently hold). If you choose not to trade, your `holdings.csv` and chart 10 keep showing the old allocation; the recommended chart 4 and the report's Profile-conflicts section will keep highlighting the gap. That divergence is by design — the system surfaces the disagreement and leaves the decision to you.

## Operations

- Daily: nothing. The cron job appends a row per ticker to `data/snapshots.csv` and refreshes `docs/index.html`.
- Whenever you want to publish the cron-refreshed dashboard: `git add docs/index.html && git commit -m "Refresh live dashboard" && git push`. cron does not auto-push.
- Monthly: run `/review-portfolio` in Claude Code. Read the report (especially the **Profile conflicts** section), decide on rebalances, execute trades in your brokerage, then update `holdings.csv`.
- After trading: edit `holdings.csv` to reflect new share counts. The next snapshot picks up the new positions.
- Anytime: open `docs/index.html` in a browser for the local view, or visit the public-demo URL.

### Optional: cron automation

If you want the daily snapshot to update automatically, install this cron entry. Skip if you'd rather invoke the commands by hand. Works the same on macOS and Linux:

```cron
PROJ=/path/to/portfolio-wave-rider
# Daily snapshot + dashboard refresh, Mon-Fri 16:30 local
30 16 * * 1-5  cd $PROJ && .venv/bin/python -m src.cli snapshot && .venv/bin/python -m src.cli dashboard --nav-current live >> data/snapshot.log 2>&1
```

Each cron call refreshes `docs/index.html` (the dashboard CLI's default `--out`). The file is git-tracked but cron does not push — `git status` will show it modified after each run, and a manual `git add docs/index.html && git commit && git push` publishes the refresh.

Install with `crontab -e` and paste. Adjust `PROJ` to your clone path. Verify with `crontab -l`. cron only fires while the machine is awake; missed runs do not auto-replay. Use `--date YYYY-MM-DD` on either subcommand to backfill.

## Outputs

| File | What's in it | When to look |
|---|---|---|
| `docs/index.html` | Eight Plotly charts of the live portfolio. Same file GitHub Pages serves. | Open in a browser any time |
| `docs/news.html` | Wave-stage news bullets from the latest `/review-portfolio` run, grouped by wave bucket — the evidence the news-researcher used to classify each wave's stage. | After each `/review-portfolio` |
| `data/wave_history.csv` | Per-wave stage classifications over time. Drives chart 5 (wave-stage trajectory). | Raw wave-classification history |
| `data/news/YYYY-MM-DD-news.json` | Full archived news payload per `/review-portfolio` run (~25 KB each). | Forensic re-read after a stage shift |
| `data/snapshots.csv` | Long-format daily snapshots (date, ticker, shares, price, value, total_value). | Raw price/share history |
| `data/recommendations.csv` | Long-format optimizer output (date, ticker, weight, return, vol, Sharpe, objective). One row block per `/review-portfolio` run. | Raw weight history |
| `data/reports/*.md` | LLM-written narrative reports, one per `/review-portfolio` run. | After each `/review-portfolio` |
| `data/snapshot.log` | cron stdout/stderr. | If a scheduled run looks missing |

The "Profile conflicts" section of any report is the most important thing to read. It tells you when the optimizer wanted something the profile forbids.

## What it does

The headline pieces are spelled out below: how `holdings.csv` shapes a run, and how the news-researcher's wave classifications affect the optimizer's expected returns.

### How `holdings.csv` shapes a run

`holdings.csv` is the watchlist universe. Both LLM subagents and the optimizer operate on exactly the set of tickers in this file:

- **News scope.** The `news-researcher` is invoked with the ticker list from `holdings.csv` and only fetches headlines for those tickers.
- **Optimizer eligibility.** `analyze` (called by `/review-portfolio`) passes the ticker list to `optimize_portfolio`, which builds a covariance matrix and an expected-return vector over only that set. The optimizer cannot assign weight to a ticker that isn't in the file.
- **`shares = 0` is meaningful.** A row with zero shares puts the ticker on the watchlist (news is fetched, the optimizer can assign weight, the dashboard tracks its price) without representing an actual position. Use this when researching a candidate before buying, or when you want price-only history for context.
- **To add a ticker:** append a row `<TICKER>,0` and run `/review-portfolio` (or wait for the next cron). The next run picks it up automatically — no other config changes needed.
- **To remove a ticker:** delete the row. Subsequent runs skip it. The historical rows in `data/snapshots.csv` and `data/recommendations.csv` are not pruned (so old charts still render correctly), but no new rows accumulate.

The optimizer respects two profile-level constraints when allocating across this universe: `concentration_cap` (no single ticker exceeds that fraction) and `exclusions` (a ticker tagged with an excluded sector is flagged in "Profile conflicts" if it gets non-zero weight). Constraints come from the profile; the list of tickers the optimizer is allowed to weight comes from `holdings.csv`.

### How wave-stage tilts enter the math

The news-researcher's only job that touches the optimizer is to assign each ticker a wave stage (`buildup`, `surge`, `neutral`, `digestion`, `peak`). The skill then passes these classifications to `analyze`, which scales each ticker's expected return by the stage's multiplier *before* running the optimizer. The math is one line:

```
μ_tilted[i] = stage_multiplier[stage(i)] × μ[i]
```

with the multipliers loaded from the profile's `financial_model.wave_stage_tilts` (defaults to `src/portfolio.py:WAVE_STAGE_TILT`):

| Stage | Multiplier | Plain reading |
|---|---|---|
| `buildup` | **1.20** | quiet, cheap, under-owned: nudge μ up 20% |
| `surge` | **1.10** | adoption compounding, room to run: nudge μ up 10% |
| `neutral` | **1.00** | no view |
| `digestion` | **0.90** | post-crest hangover: nudge μ down 10% |
| `peak` | **0.80** | enthusiasm is the story, valuations stretched: nudge μ down 20% |

`μ` (mu) is the expected-return vector — historical mean of daily log returns over the lookback window, annualized. The optimizer maximizes Sharpe `(μ_tiltedᵀw − r_free) / √(wᵀΣw)` using the tilted version. Note that `Σ` (the covariance matrix) is **not** tilted — only the expected returns. So a ticker with a bullish wave view doesn't get blindly upweighted: the optimizer still discounts it for volatility and correlation with the rest of the portfolio.

This is why the optimizer often zeros tickers with bullish wave views (BOTZ, ARKG, MSFT in recent runs). The tilt isn't strong enough to override the volatility / covariance penalty for those tickers given the 3y lookback. The "Profile conflicts" section of the report flags exactly that gap — the wave-thesis prior pulled one direction; the data pulled another. A ±20% bump in μ is meaningful but deliberately modest: a single news pass plus an LLM judgment is fairly weak evidence, so the tilts nudge weights rather than dictate them.

For a single-page consolidation of the entire wave-stage pipeline (LLM judgment process, math, history-storage, as-of-date lookup, look-ahead-bias caveat) see [docs/wave-stage-classification.md](docs/wave-stage-classification.md).

## How it's built

- Three skills at `.claude/skills/`:
  - `initialize-portfolio` (one-shot): reads the profile and an empty holdings.csv, produces a thesis-driven dollar allocation, persists it to `data/thesis_baseline.json`, and writes a thesis-only report.
  - `review-portfolio` (recurring): reads the profile, holdings, and (if present) the thesis baseline; gathers news, runs the optimizer with wave-stage tilts, writes a profile-aware report, refreshes the live dashboard. Renders the thesis-vs-recommended comparison on every run when the baseline exists.
  - `run-backtest` (on demand): walk-forward 12-month backtest, auto-rendering both the local and public backtest dashboards.
- Two subagents at `.claude/agents/`:
  - `news-researcher`: picks wave-aligned news per ticker (web search scoped to `news_sources.md` first, open search as fallback), classifies each wave's stage, returns a `wave_views` mapping `{ticker: stage}`.
  - `report-writer`: synthesizes the analysis and news into the final markdown report.
- All Python in two files: `src/portfolio.py` (math) and `src/cli.py` (one entry point with eight subcommands).
- The user-authored `investor_profile.md` is the source of truth. Every recommendation cites lines from it. When the optimal numerical answer violates a profile constraint, the report flags the conflict; it does not silently clamp.

```mermaid
flowchart TD
    user([User]) -->|/review-portfolio| skill[Skill: review-portfolio]
    profile[(investor_profile.md)] -.read.-> skill
    skill --> news[news-researcher]
    sources[(news_sources.md)] -.read.-> news
    skill -->|src.cli analyze --wave-views| analyze[Python: fetch + optimize + risk]
    news --> writer[report-writer]
    analyze --> writer
    writer --> out[/report.md + dashboard.html/]

    classDef agent fill:#e1f0ff,stroke:#3b82f6
    classDef cli fill:#fef3c7,stroke:#d97706
    classDef file fill:#f3f4f6,stroke:#6b7280
    class news,writer agent
    class analyze cli
    class out file
```

Two LLM specialists (blue) bracket one Python call (yellow). The profile and `news_sources.md` are read-only inputs.

## Things to watch

- **Prior vs likelihood.** The wave thesis is a prior; mean-variance over a 2-3 year price window is a likelihood. The optimizer often disagrees with the prior because the recent past favored low-volatility assets (bonds, cash, gold). The "Profile conflicts" section shows where they disagree. The user decides which to trust.
- **Sample bias.** The realized Sharpe on any 2-3 year window is usually optimistic vs the forward-looking distribution. Returns are non-stationary; vol clusters; means are noisy.
- **Estimation error in `μ`.** Mean-variance amplifies small errors in the expected-return estimate. A weight pinned at the concentration cap is often a symptom of estimation noise, not a real signal. This is the well-known Markowitz blow-up. Run `python -m src.cli backtest` to walk the optimizer forward on real historical data; if the weight-stability L1 metric is small (~0.02 means weights barely move week to week) the estimation noise isn't driving the solution.
- **Wave-stage tilts.** Multipliers are deliberately small and symmetric: 1.20 / 1.10 / 1.00 / 0.90 / 0.80. The tilt nudges the optimizer; it does not dictate. Track the realized vs tilted Sharpe gap (the "views premium") to see whether the news-researcher's classifications add information.
- **Wave-stage trajectories.** The dashboard's fifth chart plots each wave's stage rank over time as `wave_history.csv` accumulates. Organic accumulation is slow (one row per wave per `/review-portfolio` run, monthly cadence), so a fresh repo can backfill 12 months of trajectories two ways: `python -m src.cli seed-wave-history` writes post-hoc judgments tagged `seeded=True` (fast, free, but rows stamped with past dates were authored using post-date events — what quant finance calls **look-ahead bias**); or invoking the news-researcher in parallel with strict as-of-date instructions (12 agents, ~$5 of Sonnet usage, the honest path — each agent only sees news dated ≤ its target date) and merging the resulting wave_stages into the same CSV with `seeded=False`. The agent-based path is what the public demo uses for the headline backtest: the 12 archived payloads live at `data/news_asof/`, and `scripts/rebuild_wave_history.py` reconstructs `wave_history.csv` from them. Switching from the seed path to the as-of-date path dropped headline backtest return from roughly +159% to +110% at the time of the rebuild, while halving max drawdown (-40% to -20%) and lifting Sharpe from 1.6 to 2.2 — the seed's "extra" return was foresight-inflated. The published backtest and sweep pages use a rolling 12-month window ending today, so the headline numbers shift each time the scripts are re-run; whatever is on the live demo pages is the latest. Watch for sustained climbs (buildup → surge → peak) as a rebalance trigger and sustained drops (peak → digestion) as a trim signal.
- **Numbers come from Python.** If a figure in a report did not come from `src.cli`, that's a bug. The LLM is allowed to write prose; it is not allowed to do arithmetic.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo. CLI flags, repo layout, and testing instructions live in [REFERENCE.md](REFERENCE.md). Finance and stats terms are defined in [GLOSSARY.md](GLOSSARY.md).

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
