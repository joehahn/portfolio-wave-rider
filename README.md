# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-04 <br>
**branch:** main

A Claude Code demo: optimize a long-horizon portfolio of stocks and ETFs against a user-authored investor profile. One slash command, two LLM subagents, eight Python CLI subcommands, and a static dashboard. Stack: yfinance for prices (free Python wrapper around Yahoo Finance's historical close-price API), scipy.optimize for picking portfolio weights (maximizes risk-adjusted return subject to no-shorting and a per-asset weight cap), pandas for everything in between, Plotly for the dashboard.

**Live demo:** [joehahn.github.io/portfolio-wave-rider](https://joehahn.github.io/portfolio-wave-rider/). A snapshot of the dashboard generated against the example profile and watchlist. Refreshed manually; the timestamp inside the "Latest news" section tells you the date.

## Glossary (skip if you do this for a living)

This README leans on a handful of finance terms.

**Symbols used below:**
- `r` = return (typically a daily log return); `E[r]` = expected (mean) return.
- `σ` = standard deviation of returns (a.k.a. **volatility**).
- `μ` = vector of expected returns, one entry per asset (one of the optimizer's two inputs).
- `Σ` = covariance matrix of returns (the optimizer's other input).
- `w` = vector of portfolio weights, one entry per asset. Constrained: `Σw_i = 1` (fully invested) and each `w_i ≥ 0` (long-only).
- `V_t` = cumulative portfolio value at time `t`.
- `cummax(V)_t` = the running max of `V` through time `t`. Same semantics as pandas' `Series.cummax()`.
- `α` = quantile level (e.g., `α = 0.05` picks out the 5% tail of the return distribution).

| Term | Plain definition |
|---|---|
| **Ticker** | Symbol identifying a security: `AAPL` is Apple, `AGG` is an aggregate bond ETF, `IBIT` is a spot-Bitcoin ETF. |
| **ETF** | Exchange-traded fund. A packaged basket of underlying securities that trades like a single stock. |
| **Long-only** | All portfolio weights `w_i ≥ 0`. No short selling, no leverage. |
| **Mean-variance optimization** | Markowitz framework. Convex quadratic program: pick weights `w` that minimize `wᵀΣw` (variance) subject to `wᵀμ = target` (target return) and `Σw = 1`, `w ≥ 0`. We use `scipy.optimize.minimize(SLSQP)`. |
| **Risk-free rate (`r_free`)** | The return you can earn with effectively zero risk by parking money in short-term US Treasuries or a money-market fund. Default in the code is `0.04` (4% annualized, roughly a 1-year Treasury yield). Adjustable via `--risk-free-rate` on `analyze` and `recommend`. |
| **Sharpe ratio** | Signal-to-noise on returns: `(E[r] − r_free) / σ`. The numerator is the **excess return** (return above what's free). The denominator is the standard deviation of returns. You only get credit for the risk-bearing part of `E[r]`. Higher is better; values above 1 are good for a long-horizon portfolio. |
| **Risk aversion (`λ`)** | Scalar coefficient in the mean-variance utility `μᵀw − λ·wᵀΣw`. The `analyze` CLI exposes it as `--risk-aversion` when `--objective mean_variance`. Small `λ` (≤ 1) favors expected return → equity-heavy portfolios; large `λ` (≥ 5) favors variance reduction → bond/cash-heavy. Sliding `λ` traces the efficient frontier; `max_sharpe` picks one specific point on it (the tangent). |
| **Max drawdown** | Worst observed peak-to-trough decline of cumulative value: `min_t (V_t − cummax(V)_t) / cummax(V)_t`. A max drawdown of `-0.30` means at some point the portfolio lost 30% from a prior high. |
| **VaR_α** | Value-at-risk: the α-quantile of the daily return distribution. `VaR_0.05 = -0.02` means there's a 5% chance of losing more than 2% on a given day (under the empirical distribution). |
| **CVaR_α** | Conditional VaR: the expected return conditioned on being below `VaR_α`. Tail-loss expectation. |
| **Concentration cap** | Box constraint on the optimizer: `w_i ≤ max_weight` for every asset. Profile default 0.25. |
| **Asset class** | Coarse bucket: equities, bonds, precious metals, cash. |
| **Asset-class drift** | Deviation of recommended weights summed by class from the user's declared target percentages. Reported but not enforced. |
| **Wave-stage tilt** | Multiplicative scaling on `μ` (the expected-return vector) before optimization. `μ_tilted[i] = stage_multiplier × μ[i]`. The five stages and their multipliers are in `src/portfolio.py:WAVE_STAGE_TILT`. |
| **Rebalance** | Execute trades to move current portfolio weights back toward target weights. This project produces recommendations; the user does the trading. |
| **Wave thesis** | The user's belief that long technology waves drive returns: enter early in a wave (buildup, surge), trim near the crest (peak), avoid the hangover (digestion). The profile prose names the current wave (AI) and the next ones (rockets/spacecraft, robotics, engineered biology, quantum computing, nuclear fusion). |
| **`general_markets` bucket** | Catch-all wave bucket for tickers not tied to a specific technology wave (broad-market ETFs, bonds, cash, gold). Always classified `neutral` — no tilt applied. Acts as ballast for diversification rather than a wave bet. |
| **Watchlist universe** | The set of tickers in `holdings.csv`. Both the news-researcher and the optimizer operate on exactly this set: news is harvested only for these tickers, and the optimizer can only assign weight to these tickers. Adding a ticker with `shares=0` adds it to the universe without owning it; deleting a row removes it from future runs. |

## What it does

Three cadences:

| Cadence | Mechanism | What runs | Output |
|---|---|---|---|
| Daily, Mon-Fri 16:30 local | cron | `snapshot && news-feed && dashboard`. Fetches the latest close price for every ticker in `holdings.csv`, multiplies by `shares`, appends one row per ticker to `data/snapshots.csv`. Then pulls fresh Yahoo Finance headlines per ticker into `data/news_feed.json`. Then refreshes the dashboard. | `data/snapshots.csv`, `data/news_feed.json`, `data/dashboard.html` |
| Weekly, Fri 17:00 local | cron | `recommend && dashboard`. Re-runs the mean-variance optimizer over the holdings universe, appends new target weights to `data/recommendations.csv`, refreshes the dashboard. No news, no wave tilts. | `data/recommendations.csv`, `data/dashboard.html` |
| Monthly, you decide | You run `/review-portfolio` in Claude Code | LLM subagents gather wave-aligned news (60-day lookback on the first run, 30-day default thereafter), classify each wave's stage, pass `wave_views` to the optimizer (which scales `μ` accordingly), then write a profile-aware report and refresh the dashboard. The run also appends today's wave-stage classifications to `data/wave_history.csv` (drives the trajectory chart) and archives the full news payload to `data/news/<date>-news.json`. The first run additionally does a thesis-driven day 0 allocation before optimizing. | `data/reports/YYYY-MM-DD-review-portfolio.md`, `data/dashboard.html`, `data/wave_history.csv` (appended), `data/news/<date>-news.json` |

The weekly cron is the lightweight Python-only sibling of `/review-portfolio`: pure Python, no LLM, no wave tilts. Run the skill when you want a fresh wave-stage read and a written narrative.

## How it's built

- One skill at `.claude/skills/review-portfolio/`. Reads the profile and holdings, gathers news, runs the optimizer with wave-stage tilts, writes a report, refreshes the dashboard. On a first run (when `holdings.csv` has all-zero shares) it first does a thesis-driven dollar allocation so the user's beliefs become the day 0 baseline; the same report then shows beliefs and math side-by-side.
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

- `investor_profile.md`: `initial_investment_usd`, `concentration_cap`, `exclusions`, `asset_class_targets`, and the wave-thesis prose. Every recommendation cites lines from this file.
- `holdings.csv`: a two-column CSV (`ticker,shares`) acting as your watchlist. Pre-day-0 you can leave every `shares` at 0; that's the universe `/review-portfolio` will allocate dollars across on its first run.

Optional: `news_sources.md`, a curated list of sources per technology wave. Improves the news-researcher's signal. Missing is fine; the agent falls back to open web search.

### How `holdings.csv` shapes a run

`holdings.csv` is the watchlist universe. Both LLM subagents and the optimizer operate on exactly the set of tickers in this file:

- **News scope.** The `news-researcher` is invoked with the ticker list from `holdings.csv` and only fetches headlines for those tickers. The daily yfinance `news-feed` cron does the same.
- **Optimizer eligibility.** `analyze` and the weekly `recommend` cron pass the same ticker list to `optimize_portfolio`, which builds a covariance matrix and an expected-return vector over only that set. The optimizer cannot assign weight to a ticker that isn't in the file.
- **`shares = 0` is meaningful.** A row with zero shares puts the ticker on the watchlist (news is fetched, the optimizer can assign weight, the dashboard tracks its price) without representing an actual position. Use this when researching a candidate before buying, or when you want price-only history for context.
- **To add a ticker:** append a row `<TICKER>,0` and run `/review-portfolio` (or wait for the next cron). The next run picks it up automatically — no other config changes needed.
- **To remove a ticker:** delete the row. Subsequent runs skip it. The historical rows in `data/snapshots.csv` and `data/recommendations.csv` are not pruned (so old charts still render correctly), but no new rows accumulate.

The optimizer respects two profile-level constraints when allocating across this universe: `concentration_cap` (no single ticker exceeds that fraction) and `exclusions` (a ticker tagged with an excluded sector is flagged in "Profile conflicts" if it gets non-zero weight). Constraints come from the profile; the universe comes from `holdings.csv`.

### How wave-stage tilts enter the math

The news-researcher's only job that touches the optimizer is to assign each ticker a wave stage (`buildup`, `surge`, `neutral`, `digestion`, `peak`). The skill then passes these classifications to `analyze`, which scales each ticker's expected return by the stage's multiplier *before* running the optimizer. The math is one line:

```
μ_tilted[i] = stage_multiplier[stage(i)] × μ[i]
```

with the multipliers fixed in `src/portfolio.py:WAVE_STAGE_TILT`:

| Stage | Multiplier | Plain reading |
|---|---|---|
| `buildup` | **1.20** | quiet, cheap, under-owned: nudge μ up 20% |
| `surge` | **1.10** | adoption compounding, room to run: nudge μ up 10% |
| `neutral` | **1.00** | no view |
| `digestion` | **0.90** | post-crest hangover: nudge μ down 10% |
| `peak` | **0.80** | enthusiasm is the story, valuations stretched: nudge μ down 20% |

`μ` (mu) is the expected-return vector — historical mean of daily log returns over the lookback window, annualized. The optimizer maximizes Sharpe `(μ_tiltedᵀw − r_free) / √(wᵀΣw)` using the tilted version. Note that `Σ` (the covariance matrix) is **not** tilted — only the expected returns. So a ticker with a bullish wave view doesn't get blindly upweighted: the optimizer still discounts it for volatility and correlation with the rest of the portfolio.

This is why the optimizer often zeros tickers with bullish wave views (BOTZ, ARKG, MSFT in recent runs). The tilt isn't strong enough to override the volatility / covariance penalty for those tickers given the 3y lookback. The "Profile conflicts" section of the report flags exactly that gap — the wave-thesis prior pulled one direction; the data pulled another. A ±20% bump in μ is meaningful but deliberately modest: a single news pass plus an LLM judgment is fairly weak evidence, so the tilts nudge weights rather than dictate them.

### First run

In Claude Code, run:

```
/review-portfolio
```

On the first run (when `holdings.csv` still has all-zero shares), the skill detects the empty state, does a thesis-driven dollar allocation across the watchlist (no math, just the wave thesis plus `asset_class_targets`), converts dollars to shares using current prices, populates `holdings.csv`, records the initial state via `snapshot`, and *then* runs the mean-variance optimizer with wave-stage tilts. The resulting report shows the **day 0 baseline** (beliefs in dollar form) alongside the **day 1 recommendation** (mean-variance optimum) so you can see both at once. The gap between them is the marginal contribution of the optimizer relative to your prior.

### Subsequent runs

Same command:

```
/review-portfolio
```

Now `holdings.csv` has real positions, so the first-run branch is skipped. The skill runs news + analyze + report + dashboard against your current holdings. Run it monthly, or whenever wave-stage news has shifted materially.

## Optional: cron automation

If you want the daily snapshot and weekly recommend to run automatically, install these two cron entries. Skip if you'd rather invoke the commands by hand. The two entries work the same on macOS and Linux:

```cron
PROJ=/path/to/portfolio-wave-rider
# Daily snapshot + news-feed + dashboard refresh, Mon-Fri 16:30 local
30 16 * * 1-5  cd $PROJ && .venv/bin/python -m src.cli snapshot && .venv/bin/python -m src.cli news-feed && .venv/bin/python -m src.cli dashboard >> data/snapshot.log 2>&1
# Weekly recommend + dashboard refresh, Fri 17:00 local
0  17 * * 5    cd $PROJ && .venv/bin/python -m src.cli recommend && .venv/bin/python -m src.cli dashboard >> data/recommend.log 2>&1
```

Install with `crontab -e` and paste. Adjust `PROJ` to your clone path. Verify with `crontab -l`. cron only fires while the machine is awake; missed runs do not auto-replay. Use `--date YYYY-MM-DD` on either subcommand to backfill.

## Operations

- Daily: nothing. The cron job appends a row per ticker to `data/snapshots.csv` and refreshes `data/dashboard.html`.
- Weekly: nothing. Friday 17:00 local appends one optimization run to `data/recommendations.csv` and refreshes the dashboard.
- Monthly: run `/review-portfolio` in Claude Code. Read the report, decide on rebalances, execute trades in your brokerage, then update `holdings.csv`.
- Anytime: open `data/dashboard.html` in a browser.
- After trading: edit `holdings.csv` to reflect new share counts. The next snapshot picks up the new positions.
- Refreshing the public demo dashboard at `joehahn.github.io/portfolio-wave-rider`: run `/review-portfolio` against the **example** state (a clean clone of `holdings.example.csv` with all-zero shares; a clean copy of `investor_profile.example.md`), then `cp data/dashboard.html docs/index.html`, commit, and push. Do this when the example watchlist or profile changes, not when your personal `holdings.csv` changes (since the published demo should reflect the example state, not your real portfolio dollar values).

## Outputs to monitor

| File | What's in it | When to look |
|---|---|---|
| `data/dashboard.html` | Six Plotly charts (1: portfolio value over time **with SPY overlay**; 2: per-ticker recommended portfolio % drift; 3: latest recommended portfolio % bar chart; 4: per-wave stage trajectories accumulating across `/review-portfolio` runs; 5: $ allocated by asset class over time; 6: $ allocated by wave over time, log y-axis) plus two news sections: "Today's headlines" (refreshed daily by cron from yfinance) and "In-depth news from last `/review-portfolio`" (LLM portfolio-relevance summaries with wave-stage classification, refreshed monthly) | Open in a browser any time |
| `data/news_feed.json` | Daily Yahoo Finance headlines per ticker (refreshed by cron). Headline + first-paragraph summary + source + URL + date. ~5 bullets per ticker. Drives the dashboard's "Today's headlines" section. | If you want raw daily headline coverage |
| `data/wave_history.csv` | Long-format per-wave stage history: `date, wave, stage, evidence_tickers, rationale`. One row per wave per `/review-portfolio` run. Drives the wave-stage trajectory chart on the dashboard. | If you want raw history of how the LLM has classified each wave over time |
| `data/news/YYYY-MM-DD-news.json` | Full archived news payload from each `/review-portfolio` run (per-ticker bullets with headline + summary). About 25 KB per run; accumulates with no pruning. | When the dashboard chart shows a wave-stage shift and you want to re-read what news was driving the LLM's call on that date |
| `data/snapshots.csv` | Long-format daily snapshots: `date, ticker, shares, price, value, total_value` | Raw history; load with pandas |
| `data/recommendations.csv` | Long-format weekly optimizer output: `date, ticker, weight, expected_return, annual_volatility, sharpe_ratio, objective` | Raw history; load with pandas |
| `data/reports/*.md` | LLM-written narrative reports, one per `/review-portfolio` run | After each `/review-portfolio` |
| `data/snapshot.log`, `data/recommend.log` | cron stdout/stderr | If a scheduled run looks missing |

The "Profile conflicts" section of any report is the most important thing to read. It tells you when the optimizer wanted something the profile forbids.

## Things to watch

- **Prior vs likelihood.** The wave thesis is a prior; mean-variance over a 2-3 year price window is a likelihood. The optimizer often disagrees with the prior because the recent past favored low-volatility assets (bonds, cash, gold). The "Profile conflicts" section shows where they disagree. The user decides which to trust.
- **Sample bias.** The realized Sharpe on any 2-3 year window is usually optimistic vs the forward-looking distribution. Returns are non-stationary; vol clusters; means are noisy.
- **Estimation error in `μ`.** Mean-variance amplifies small errors in the expected-return estimate. A weight pinned at the concentration cap is often a symptom of estimation noise, not a real signal. This is the well-known Markowitz blow-up. Run `python -m src.cli backtest` to walk the optimizer forward on real historical data; if the weight-stability L1 metric is small (~0.02 means weights barely move week to week) the estimation noise isn't driving the solution.
- **Wave-stage tilts.** Multipliers are deliberately small and symmetric: 1.20 / 1.10 / 1.00 / 0.90 / 0.80. The tilt nudges the optimizer; it does not dictate. Track the realized vs tilted Sharpe gap (the "views premium") to see whether the news-researcher's classifications add information.
- **Wave-stage trajectories.** The dashboard's fourth chart plots each wave's stage rank over time as `wave_history.csv` accumulates. Organic accumulation is slow (one row per wave per `/review-portfolio` run, monthly cadence), so a fresh repo runs `python -m src.cli seed-wave-history` once to backfill 12 months of post-hoc monthly classifications grounded in the news flow over 2025-2026 (AI revenue compounding mid-2025, humanoid surge late 2025, nuclear-energy run-up Q4 2025 then digestion in Q1 2026). Seeded rows carry `seeded=True` so they're distinguishable from organic output. Watch for sustained climbs (buildup → surge → peak) as a rebalance trigger and sustained drops (peak → digestion) as a trim signal.
- **Numbers come from Python.** If a figure in a report did not come from `src.cli`, that's a bug. The LLM is allowed to write prose; it is not allowed to do arithmetic.

## CLI reference

Nine subcommands. `/review-portfolio` calls `init-holdings` (first-run branch only), `wave-history` (after each news pass), and `analyze`. The cron jobs call `snapshot`, `news-feed`, `recommend`, and `dashboard`. `backtest` is a one-off spot-check tool, not part of any cron flow. `seed-wave-history` is a one-time backfill for chart 4 trajectories. Every subcommand prints a single JSON blob to stdout.

```bash
# Convert a thesis-driven dollar allocation into shares (used internally by the
# skill's first-run branch; runnable directly if you ever want to redo a day 0
# allocation, e.g. after expanding the watchlist)
.venv/bin/python -m src.cli init-holdings --allocations '{"NVDA": 5000, "MSFT": 5000, ...}' --out holdings.csv

# Append today's per-wave stage classifications (read from data/news_latest.json)
# to data/wave_history.csv so the dashboard can plot stage trajectories
.venv/bin/python -m src.cli wave-history [--news data/news_latest.json] [--force]

# Backfill 12 months of post-hoc monthly wave-stage classifications, tagged
# seeded=True. Run once on a fresh repo so chart 4 is informative before
# /review-portfolio has had time to accumulate organic history.
.venv/bin/python -m src.cli seed-wave-history [--force]

# Pull recent Yahoo Finance headlines per ticker into data/news_feed.json (cron, no LLM)
.venv/bin/python -m src.cli news-feed [--per-ticker-limit 5]

# One-shot analysis (fetch prices + compute log-returns + optimize + risk metrics).
# Three objectives:
#   max_sharpe    - default; maximize (μᵀw - r_free) / √(wᵀΣw). Risk-adjusted optimum.
#   min_variance  - minimize wᵀΣw. Lowest-vol point on the frontier.
#   mean_variance - maximize μᵀw - λ·wᵀΣw. λ (`--risk-aversion`) slides along the
#                   frontier: small λ favors return (more equity-heavy), large λ
#                   favors variance reduction (more bond/cash-heavy).
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --period 3y --max-weight 0.25
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --objective mean_variance --risk-aversion 1.0

# Time-series logging
.venv/bin/python -m src.cli snapshot   [--date YYYY-MM-DD] [--force]
.venv/bin/python -m src.cli recommend  [--max-weight 0.25] [--force]

# Walk-forward backtest of the cron 'recommend' path over a historical window
# (math-only; no news, no LLM cost). Writes data/backtest/{snapshots,recommendations}.csv
# plus data/backtest/report.md with realized return, max drawdown, weight stability,
# and per-benchmark active-return comparison (default SPY).
.venv/bin/python -m src.cli backtest [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--initial-usd 50000] [--benchmarks SPY DIA QQQ]

# Static dashboard (reads the CSVs above plus both news files; writes data/dashboard.html;
# overlays each --benchmarks ticker on the portfolio-value chart, default SPY)
.venv/bin/python -m src.cli dashboard [--benchmarks SPY]
```

To inspect the backtest visually, point the dashboard at the backtest CSVs:

```bash
.venv/bin/python -m src.cli dashboard \
  --snapshots data/backtest/snapshots.csv \
  --recommendations data/backtest/recommendations.csv \
  --out data/backtest/dashboard.html
```

## Layout

```
portfolio-wave-rider/
├── investor_profile.md         # source of truth (you write this; gitignored)
├── investor_profile.example.md # template to copy
├── holdings.csv                # ticker,shares (you maintain this; gitignored)
├── holdings.example.csv        # template to copy
├── news_sources.md             # optional curated sources per wave
├── CLAUDE.md                   # rules for Claude operating in this repo
├── .claude/
│   ├── agents/                 # 2 subagent specs (news-researcher, report-writer)
│   ├── skills/                 # 1 skill (review-portfolio)
│   └── settings.json           # tool allowlist
├── src/
│   ├── portfolio.py            # all math
│   └── cli.py                  # one CLI, six subcommands
├── tests/
└── data/
    ├── snapshots.csv           # daily, appended (your history)
    ├── recommendations.csv     # weekly, appended (your history)
    ├── wave_history.csv        # per-/review-portfolio run, appended (gitignored)
    ├── dashboard.html          # static Plotly + news dashboard (gitignored, regenerated)
    ├── news_feed.json          # daily yfinance headlines (gitignored)
    ├── news_latest.json        # latest news payload from /review-portfolio (gitignored)
    ├── news/                   # archived news payloads, one per run (gitignored)
    ├── reports/                # LLM-written reports (gitignored)
    ├── backtest/               # output of `cli backtest` runs (gitignored)
    └── *.log                   # cron output (gitignored)
```

## Testing

```bash
.venv/bin/pytest tests/    # offline; no network calls, no API keys needed
```

Tests are pure-Python: synthetic price series → returns → optimizer → risk metrics. Network-dependent code paths (yfinance) are not exercised in CI.

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo.

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
