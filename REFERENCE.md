# Reference

CLI flags, repo layout, and testing instructions for Portfolio Wave Rider. Narrative tour and operational guide live in [README.md](README.md); finance terms in [GLOSSARY.md](GLOSSARY.md).

## CLI reference

Six subcommands. The daily cron calls `snapshot` and `dashboard`. `backtest` is a one-off spot-check tool. Every subcommand prints a single JSON blob to stdout.

```bash
# Convert a thesis-driven dollar allocation into shares (used internally by the
# initialize-portfolio skill; runnable directly if you ever want to redo a thesis
# allocation, e.g. after expanding the watchlist)
.venv/bin/python -m src.cli init-holdings --allocations '{"NVDA": 5000, "MSFT": 5000, ...}' --out holdings.csv

# One-shot analysis (fetch prices + compute log-returns + optimize + risk metrics).
# Three objectives:
#   max_sharpe    - default; maximize (μᵀw - r_free) / √(wᵀΣw). Risk-adjusted optimum.
#   min_variance  - minimize wᵀΣw. Lowest-vol point on the frontier.
#   mean_variance - maximize μᵀw - λ·wᵀΣw. λ (`--risk-aversion`) slides along the
#                   frontier: small λ favors return (more equity-heavy), large λ
#                   favors variance reduction (more bond/cash-heavy).
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --period 1.3y --max-weight 0.25
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --objective mean_variance --risk-aversion 1.0

# Time-series logging
.venv/bin/python -m src.cli snapshot   [--date YYYY-MM-DD] [--force]
.venv/bin/python -m src.cli recommend  [--max-weight 0.25] [--force]

# Walk-forward backtest of the 'recommend' path over a historical window. Writes
# data/backtest/{snapshots, recommendations}.csv plus data/backtest/report.md
# with realized return, max drawdown, weight stability, and per-benchmark
# active-return comparison (default SPY). Default window is a rolling 12 months
# ending today (yfinance silently clips to whatever trading day has data, so a
# mid-session run just stops at yesterday's close). Auto-renders both
# data/backtest/dashboard.html and docs/backtest.html.
.venv/bin/python -m src.cli backtest [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--initial-usd 50000] [--benchmarks SPY DIA QQQ]

# Same backtest with the mean_variance objective at lambda=1.
.venv/bin/python -m src.cli backtest --objective mean_variance --risk-aversion 1.0

# Static dashboard (reads the CSVs above; writes docs/index.html by default;
# overlays each --benchmarks ticker on the portfolio-value chart, default SPY).
# --nav-current is only meaningful for the three sweep pages (lambda, max_weight,
# lookback), which cross-link to each other.
.venv/bin/python -m src.cli dashboard [--benchmarks SPY] [--out docs/index.html] [--nav-current lambda|max_weight|lookback]
```

To inspect the backtest visually without auto-render, point the dashboard at the backtest CSVs:

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
├── README.md                   # narrative tour + operations
├── REFERENCE.md                # this file: CLI, layout, testing
├── GLOSSARY.md                 # finance and stats terms
├── CLAUDE.md                   # rules for Claude operating in this repo
├── .claude/
│   ├── agents/                 # 1 subagent spec (report-writer)
│   ├── skills/                 # 1 skill (initialize-portfolio); curator rebuild pending
│   └── settings.json           # tool allowlist
├── src/
│   ├── portfolio.py            # all math
│   └── cli.py                  # one CLI, six subcommands
├── tests/
├── data/
│   ├── snapshots.csv           # daily, appended (your history; gitignored)
│   ├── recommendations.csv     # appended on each recommend run (your history; gitignored)
│   ├── thesis_baseline.json    # one-time artifact from /initialize-portfolio (gitignored)
│   ├── news_feed.json          # daily yfinance headlines (gitignored)
│   ├── reports/                # LLM-written reports (gitignored)
│   ├── backtest/               # output of `cli backtest` runs (gitignored)
│   └── *.log                   # cron output (gitignored)
└── docs/                       # GitHub Pages publishing root
    ├── index.html              # live dashboard
    ├── backtest.html           # 12-month backtest dashboard
    ├── lambda_comparison.html  # mean_variance λ sweep
    └── max_weight_comparison.html  # concentration_cap sweep
```

## Outputs

| File | What's in it | When to look |
|---|---|---|
| `docs/index.html` | Plotly charts of the live portfolio. Same file GitHub Pages serves (when published from this branch). | Open in a browser any time |
| `data/snapshots.csv` | Long-format daily snapshots (date, ticker, shares, price, value, total_value). | Raw price/share history |
| `data/recommendations.csv` | Long-format optimizer output (date, ticker, weight, return, vol, Sharpe, objective). One row block per `recommend` run. | Raw weight history |
| `data/reports/*.md` | LLM-written narrative reports. | After each report-producing skill run |
| `data/snapshot.log` | cron stdout/stderr. | If a scheduled run looks missing |

## How it's built

- Skills at `.claude/skills/`:
  - `initialize-portfolio` (one-shot): reads the profile and an empty holdings.csv, produces a thesis-driven dollar allocation, persists it to `data/thesis_baseline.json`, and writes a thesis-only report.
  - `/review-portfolio` and `/run-backtest` are not present on this branch; they return in the curator rebuild.
- Subagents at `.claude/agents/`:
  - `report-writer`: synthesizes the analysis and news into the final markdown report.
- All Python in two files: `src/portfolio.py` (math) and `src/cli.py` (one entry point with six subcommands).
- The user-authored `investor_profile.md` is the source of truth. Every recommendation cites lines from it. When the optimal numerical answer violates a profile constraint, the report flags the conflict; it does not silently clamp.

## 5-year backtest experiment (research artifact)

A separate branch `5y-backtest` contains a longer-horizon, multi-regime
test of whether the news-researcher's wave-stage tilts add value to the
optimizer. Headline numbers and methodology:

- **Window:** 2021-09-30 → 2026-04-30 (~4.6 years, 20 quarterly rebalances)
- **Universe:** 11 tickers — AAPL, MSFT, GOOGL, NVDA, BOTZ, QTUM, SPY, VIG, AGG, BIL, IAU. ARKG / NUKZ / RKLB excluded (insufficient pre-window history)
- **Starting portfolio (fixed, not optimizer-picked):** AAPL 25%, MSFT 20%, GOOGL 15%, SPY 15%, AGG 10%, IAU 10%, BIL 5% — a realistic "tech-savvy 2021-Q3 investor with vague AI thesis but no thematic positions"
- **Optimizer:** mean_variance λ=1, lookback 1.3y, max_weight 0.25, cadence quarterly
- **Wave-stage tilts:** 20 strict as-of-date news-researcher Sonnet calls (one per quarter-end), aggregated into `data/wave_history_5y.csv` with seven-lever discipline (date-stamped persona, named-event suppression, WebSearch `before:` filters, grounding rule, forbidden-phrase blocklist, self-critique pass, quarterly calibration probe)

| Path | Final | Return | Annualized | vs SPY |
|---|---|---|---|---|
| Buy-and-hold initial portfolio | $95,611 | +91.22% | +15.19%/yr | +13pp |
| Quarterly rebalance, **no AI tilt** | $121,141 | **+142.28%** | +21.30%/yr | +64pp |
| Quarterly rebalance, **with AI tilt** | $115,516 | +131.03% | +20.04%/yr | +53pp |
| SPY (rebased) | $89,073 | +78.15% | +13.43%/yr | — |

**Findings:**

1. Quarterly rebalancing alone (no AI signal) added +51pp over do-nothing, migrating the megacap-tech start toward wave-thematic targets as price-history μ picked them up.
2. The news-researcher's wave-stage classifications **subtracted ~5%** in final value vs the same rebalance loop without them. Consistent with the 1y backtest's −1.3% AI lift and the prior corner-pick 5y backtest's −2.5%.
3. Starting-allocation choice mattered less than expected — AAPL/MSFT/GOOGL/SPY-heavy buy-and-hold beat SPY by only +13pp.

The branch's `docs/backtest_5y.html` renders the full ten-chart dashboard (portfolio value with all three paths overlaid, AI-lift ratio chart, recommended-weights stacked bars over 20 quarters, wave-stage trajectories from the 20 quarterly news-researcher calls, etc.).

To reproduce: `git checkout 5y-backtest`, then the scaffolding under `scripts/` (`post_date_events.py`, `asof_news_prompt.md`, `aggregate_wave_history_5y.py`) plus the `.claude/skills/rebuild-wave-history-5y/` skill. Cost: ~$5 in Sonnet usage for the 20 strict as-of-date news classifications.

## Testing

```bash
.venv/bin/pytest tests/    # offline; no network calls, no API keys needed
```

Tests are pure-Python: synthetic price series → returns → optimizer → risk metrics. Network-dependent code paths (yfinance) are not exercised in CI.
