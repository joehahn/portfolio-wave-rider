# Reference

CLI flags, repo layout, architecture, and testing instructions for Portfolio Wave Rider. Narrative tour lives in [README.md](README.md); finance terms in [GLOSSARY.md](GLOSSARY.md).

## CLI reference

Seven subcommands. The daily cron calls `snapshot` and `dashboard`. The `/review-portfolio` skill calls `curate`, `analyze`, `recommend`, and `dashboard`. `backtest` is a one-off spot-check tool. Every subcommand prints a single JSON blob to stdout.

```bash
# Convert a thesis-driven dollar allocation into shares (used internally by the
# initialize-portfolio skill; runnable directly if you ever want to redo a thesis
# allocation, e.g. after expanding the watchlist).
.venv/bin/python -m src.cli init-holdings --allocations '{"NVDA": 5000, "MSFT": 5000, ...}' --out holdings.csv

# One-shot analysis (fetch prices + compute log-returns + optimize + risk metrics).
# The optimizer always maximizes the mean-variance utility μᵀw - λ·wᵀΣw subject
# to ∑wᵢ=1, wᵢ≥0, and wᵢ≤max_weight. λ (`--risk-aversion`) is the only knob on
# the return/variance tradeoff: small λ favors return (more equity-heavy), large
# λ favors variance reduction (more bond/cash-heavy).
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --period 0.5y --max-weight 0.80
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --risk-aversion 0.5

# Apply a watchlist-curator JSON payload to watchlist.csv and data/curation_history.csv.
# Validates against the contract (listing date via yfinance, max_watchlist_size,
# no double-adds, no stale removes, blocks removes when shares > 0). Output JSON
# lists applied_adds, applied_removes, and rejections with reasons.
.venv/bin/python -m src.cli curate --input data/curator_latest.json [--as-of-date YYYY-MM-DD] [--no-listing-check]

# Time-series logging
.venv/bin/python -m src.cli snapshot   [--date YYYY-MM-DD] [--force]
.venv/bin/python -m src.cli recommend  [--max-weight 0.80] [--force]

# Math-only walk-forward backtest of a fixed watchlist. Default window is a rolling
# 12 months ending today (yfinance silently clips to whatever trading day has data).
# Writes data/backtest/{snapshots, recommendations}.csv plus report.md to data/backtest/.
.venv/bin/python -m src.cli backtest [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] \
                                     [--initial-usd 50000] [--benchmarks SPY DIA QQQ]

# Curator-driven walk-forward backtest: same as above but consumes a directory of
# pre-collected watchlist-curator JSON payloads (one per rebalance date) plus a
# _starter.json config. Replays each payload through the curate + analyze loop, and
# computes a buy-and-hold-of-starter baseline for comparison. Writes snapshots.csv,
# recommendations.csv, baselines_totals.csv, curation_summary.json, and report.md
# to the out_dir.
.venv/bin/python -m src.cli backtest --curator-runs-dir data/curator_runs/5y-sweep-cap08 \
                                     --out-dir data/backtest_curator_5y

# Static dashboard. Default writes docs/index.html (the live portfolio).
# --curator-backtest-dir switches to the curator-backtest dashboard at
# docs/backtest_curator.html: two charts (equity-curve race + watchlist Gantt
# over time) plus a curation event log.
.venv/bin/python -m src.cli dashboard [--benchmarks SPY] [--out docs/index.html]
.venv/bin/python -m src.cli dashboard --curator-backtest-dir data/backtest_curator_5y \
                                       --curator-runs-dir data/curator_runs/5y-sweep-cap08
```

To inspect a math-only backtest visually without auto-render, point the dashboard at the backtest CSVs:

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
├── holdings.csv                # ticker,shares -- your REAL positions (you maintain this; gitignored)
├── watchlist.csv               # ticker -- curator-managed optimizer universe (auto-written; gitignored)
├── holdings.example.csv        # template to copy
├── news_sources.md             # optional curated sources per wave bucket
├── README.md                   # narrative tour + headline result
├── REFERENCE.md                # this file: CLI, layout, architecture, testing
├── GLOSSARY.md                 # finance and stats terms
├── CLAUDE.md                   # rules for Claude operating in this repo
├── .claude/
│   ├── agents/                 # 2 subagent specs
│   │   ├── watchlist-curator.md  # proposes adds/removes per rebalance from news
│   │   └── report-writer.md      # synthesizes analyze + curator into a report
│   ├── skills/                 # 4 slash commands
│   │   ├── initialize-portfolio/SKILL.md      # one-shot thesis allocation (day 0)
│   │   ├── review-portfolio/SKILL.md          # recurring curator-driven review
│   │   ├── run-backtest/SKILL.md              # rolling-5y backtest refresh + auto-publish
│   │   └── sweep-max-watchlist-size/SKILL.md  # 4-cap experiment over the 5y window
│   └── settings.json           # tool allowlist
├── src/
│   ├── portfolio.py            # all math
│   └── cli.py                  # one CLI, seven subcommands
├── scripts/
│   ├── setup_curator_run.py    # creates a curator runs dir + _starter.json
│   ├── compute_backtest_dates.py  # rolling-5y date diff used by /run-backtest
│   ├── post_date_events.py     # chronological event timeline; suppression list for as-of-date backtests
│   ├── replay_watchlist.py     # replays curator JSONs to compute the watchlist at any as-of date
│   ├── sweep.py                # parameter sweeps for risk_aversion / lookback / concentration_cap
│   ├── sweep_watchlist_size.py # aggregates per-cap _backtest dirs into the cap-sweep page
│   ├── walk_forward.py         # robustness check: are sweep winners stable across halves of the window?
│   ├── run_sweeps.sh           # convenience runner for the three replay sweeps
│   ├── price_snapshot.sh        # cron entry: snapshot + dashboard, logs to data/snapshot.log
│   ├── install_cron.sh         # idempotent installer for the cron entry
│   └── autopush_docs.sh        # auto-pushes docs/ when cron updates docs/index.html
├── tests/
├── data/                       # gitignored except curator_runs/ and backtest_curator_*/
│   ├── snapshots.csv           # daily, appended (your history)
│   ├── recommendations.csv     # appended on each recommend run (your history)
│   ├── curation_history.csv    # appended on each curate run (your history)
│   ├── thesis_baseline.json    # one-time artifact from /initialize-portfolio
│   ├── curator_latest.json     # most recent /review-portfolio curator output
│   ├── curator_runs/           # one subdir per curator backtest run + a live/ archive
│   │   ├── 5y-sweep-cap08/       # canonical 5y backtest JSONs (cap=8, committed)
│   │   ├── 5y-quarterly/         # cap=12 historical record from before the default migration
│   │   ├── 5y-sweep-cap{05,16,24}/  # /sweep-max-watchlist-size variants (committed)
│   │   └── live/                 # one JSON per /review-portfolio run (committed)
│   ├── backtest/               # output of math-only `cli backtest` runs (gitignored)
│   ├── backtest_curator_5y/    # output of the curator-driven 5y backtest (committed)
│   ├── reports/                # LLM-written reports (gitignored)
│   └── *.log                   # cron output (gitignored)
└── docs/                       # GitHub Pages publishing root
    ├── index.html                       # live dashboard (regenerated daily by cron)
    ├── backtest_curator.html            # 5y curator-backtest dashboard
    ├── sweep_risk_aversion.html         # λ sweep
    ├── sweep_lookback.html              # lookback-period sweep
    ├── sweep_concentration_cap.html     # concentration-cap sweep
    └── sweep_max_watchlist_size.html    # max_watchlist_size sweep
```

## Outputs

| File | What's in it | When to look |
|---|---|---|
| `docs/index.html` | Plotly charts of the live portfolio. Regenerated by cron after each daily snapshot. | Open in a browser any time |
| `docs/backtest_curator.html` | Curator-backtest dashboard: six charts — equity-curve race, 90-day rolling Sharpe, watchlist Gantt over time, per-holding $ gain, $ by asset class, $ by wave. | One-off; refresh by re-running `dashboard --curator-backtest-dir` |
| `data/snapshots.csv` | Long-format daily snapshots (date, ticker, shares, price, value, total_value). | Raw price/share history |
| `data/recommendations.csv` | Long-format optimizer output (date, ticker, weight, return, vol, Sharpe, objective). One row block per recommend run. | Raw weight history |
| `data/curation_history.csv` | One row per applied add or remove: date, action, ticker, wave_bucket, rationale, news_evidence_urls. | Audit trail of watchlist composition over time |
| `data/curator_latest.json` | Most recent watchlist-curator JSON return (overwritten each `/review-portfolio` run). | Latest curator decisions + evidence |
| `data/curator_runs/<run_id>/*-curation.json` | Per-rebalance archive of curator outputs from backtest runs and live runs. | Forensic re-read; replay input to `backtest --curator-runs-dir` |
| `data/backtest_curator_5y/report.md` | Headline curator-backtest numbers (curator vs both baselines vs SPY, max drawdown, weight stability). | After re-running the 5y replay |
| `data/reports/YYYY-MM-DD-<skill>.md` | LLM-written narrative reports from `/initialize-portfolio` and `/review-portfolio`. | After each skill run |
| `data/snapshot.log` | cron stdout/stderr. | If a scheduled run looks missing |

Note: when a ticker leaves the universe (removed from `watchlist.csv` by the curator, or sold out of `holdings.csv`), historical rows in `data/snapshots.csv` and `data/recommendations.csv` are not pruned, so old charts still render correctly. No new rows accumulate for the removed ticker going forward.

The "Profile conflicts" section of any report is the most important thing to read. It tells you when the optimizer wanted something the profile forbids.

## How it's built

The diagram below shows the `/review-portfolio` flow — the recurring path that fires once per rebalance. The other three skills (`/initialize-portfolio`, `/run-backtest`, `/sweep-max-watchlist-size`) reuse the same CLI subcommands and subagents in different combinations; see each `SKILL.md` for the per-skill orchestration.

```mermaid
flowchart TD
    user([User]) -->|/review-portfolio| skill[Skill: review-portfolio]
    profile[(investor_profile.md)] -.read.-> skill
    watchlist[(watchlist.csv)] -.read.-> skill
    skill --> curator[watchlist-curator]
    sources[(news_sources.md)] -.read.-> curator
    curator -->|writes JSON| latest[(curator_latest.json)]
    latest --> curate[CLI: curate]
    curate -->|mutates| watchlist_w[watchlist.csv]
    curate -->|appends| history[(curation_history.csv)]
    skill --> analyze[CLI: analyze]
    skill --> recommend[CLI: recommend]
    recommend -->|appends| recs[(recommendations.csv)]
    analyze --> writer[report-writer]
    curator --> writer
    curate --> writer
    writer --> report[/report.md/]
    skill --> dash[CLI: dashboard]
    dash --> idx[/docs/index.html/]

    classDef agent fill:#e1f0ff,stroke:#3b82f6
    classDef cli fill:#fef3c7,stroke:#d97706
    classDef file fill:#f3f4f6,stroke:#6b7280
    class curator,writer agent
    class curate,analyze,recommend,dash cli
    class report,idx,history,latest,recs file
```

Two LLM specialists (blue) bracket four Python calls (yellow). The profile is the source of truth; the curator decides composition; the optimizer decides weights.

- Four skills at `.claude/skills/`:
  - `initialize-portfolio` (one-shot): reads the profile, produces a thesis-driven dollar allocation, writes `holdings.csv` (real positions) + `watchlist.csv` (curator universe), persists the allocation to `data/thesis_baseline.json`, and writes a thesis-only report. No optimizer, no news.
  - `review-portfolio` (recurring): fires one watchlist-curator call against today's date, applies adds/removes via `curate`, runs `analyze` and `recommend` on the post-change watchlist, calls report-writer for a profile-aware narrative, and refreshes the live dashboard.
  - `run-backtest` (on-demand maintenance): refreshes the canonical 5-year curator backtest against a rolling 5-year window ending today, regenerates `docs/backtest_curator.html`, and commits the result.
  - `sweep-max-watchlist-size` (on-demand experiment): fires the watchlist-curator at four `max_watchlist_size` values across the 21 quarter-end dates of the standard 5y backtest and renders `docs/sweep_max_watchlist_size.html`.
- Two subagents at `.claude/agents/`:
  - `watchlist-curator` (Sonnet): reads recent news (and `news_sources.md` if present), proposes adds and removes against the current watchlist. Returns JSON; does not write files. Carries strict as-of-date discipline (persona reset, WebSearch `before:` filters, suppression list, self-critique pass) when the harness passes a historical as-of date — used by curator backtests to suppress lookahead bias.
  - `report-writer` (Sonnet): synthesizes the analyze output and curator output into the final markdown report.
- All Python in two files: `src/portfolio.py` (math) and `src/cli.py` (one entry point with seven subcommands).
- The user-authored `investor_profile.md` is the source of truth. Every recommendation cites lines from it. When the optimal numerical answer violates a profile constraint, the report flags the conflict in a dedicated section; it does not silently clamp.

## Retrieval engines

Staged here until it gets its own doc. The README deliberately keeps the backtest-vs-forward retrieval split out of the tour.

Both paths read the same `retrieval_config.json`: `wave_keywords` (per-wave phrases that surface an article), `org_stoplist` (non-company entities to drop), and `engine` (two GKG-only guards, `ontopic_offset` and `max_scan_gb`, a BigQuery cost cap). What differs is how the keywords are used.

- **Historical (backtest, bootstrap)**: `GkgWaybackRetriever` in `src/retriever.py`, wrapping `scripts/gkg_pool.py` + `scripts/news_pool.py`. GDELT's GKG table in BigQuery discovers the date-honest article list, matching the keyword regex against article titles and URLs, then Wayback supplies each article's as-of lede so the curator only ever sees text that existed on the decision date. A title-gated live fetch fills Wayback misses (`backtest_sdk._apply_live_fallback`), which carries some look-ahead risk and is tagged as such. Because BigQuery is queried with the keyword regex, changing `wave_keywords` requires a re-ingest before the backtest reflects it.
- **Forward (daily live pull)**: `WebSearchRetriever` in `src/retriever.py` turns each wave's keywords into a fixed `web_search` query, run through a cheap model with no discretion (`retrieval_model` in the profile's `forward` section), and trafilatura extracts each article's full text at pull time. No look-ahead risk, since as-of is today.
- `GkgWaybackRetriever` is also the cold-start backfill for the forward corpus (`cli.py pull --backfill`), which is how the bootstrap corpus was seeded with a trailing window before the daily cron took over.

## The curator backtest

The headline experiment behind the watchlist-curator design. See [docs/backtest_gkg_3yr_kimi.html](https://joehahn.github.io/portfolio-wave-rider/backtest_gkg_3yr_kimi.html) for the rendered result.

- **Window**: 2023-05-04 to 2026-05-03 (about 3 years, 157 weekly rebalances). Set in `investor_profile.md`'s `backtest` section (`start_date` / `end_date`).
- **Starter watchlist**: AAPL, MSFT, GOOGL, NVDA, SPY, a realistic tech-savvy investor's holding.
- **Optimizer (one config)**: mean-variance `λ=2.0`, 30-day price lookback, `concentration_cap=0.80`, weekly cadence, `max_watchlist_size=5`, plus the three permanent `always_include` anchors SPY / AGG / IAU. All from `investor_profile.md` (`financial_model` plus the top-level `concentration_cap`). The same config drives the live recommend path and the backtest.
- **Curator**: kimi-k2.5 (`backtest.curator_model` in the profile), run with `--no-reasoning` via OpenRouter, one call per week. Each call reads a date-clean **GKG plus Wayback** news pool (see the retriever section) instead of live WebSearch, so it only ever sees period-correct news. A full 157-week curate costs about $1.60.
- **Decisions**: kimi was disciplined, making only four swaps across 157 weeks, every ticker real and US-listed. It added RKLB (rockets, on a 2024-05 Space Force catalyst), LMT (defense, 2024-05), CEG (nuclear, 2024-10, the Microsoft / Three Mile Island restart), and re-added GOOGL as a quantum name (2024-12, the Willow chip). It removed GOOGL, AAPL, and LMT over the run, and tried to remove the SPY anchor (rejected by the validator). It held NVDA and MSFT from the starter throughout.

| Strategy | Total return | Notes |
|---|---|---|
| **Curator** (kimi; cap 0.8, λ 2.0, 30-day lookback, weekly) | **+957%** | about +120%/yr, 29% max drawdown, ends RKLB 60% / NVDA 40% |
| Buy-and-hold starter (equal-weight, then hold) | +217% | includes NVDA |
| SPY benchmark (rebased) | +85% | same start |

The curator beat the buy-and-hold investor by about **+740 percentage points**, or **3.4x** its gain. NVDA is in the starter, so the curator gets no credit for the obvious AI winner; the lift comes from its thematic adds and the optimizer's weekly re-weighting.

**Where the return comes from.** Attributed by the wave the curator held each name under at each date (the dashboard's time-aware split), the gain is roughly rockets (RKLB) +$340K, AI (NVDA and MSFT) +$80K, quantum (GOOGL) +$74K, and geopolitical (LMT) +$7K, with nuclear (CEG) about -$7K and the cashlike anchors about -$12K of drag. Rocket Lab alone is about 71% of the gain, so the headline rests on one position.

**Safe-haven anchors.** The optimizer's universe always includes SPY (equity), AGG (bonds), and IAU (gold), set via the profile's `always_include` key and sitting outside the curator's `max_watchlist_size` budget. At this return-hungry config they draw little weight and net a small drag (about -$12K), so they leave the drawdown essentially unchanged. A forced floor, a lower cap, or a higher `λ` would be needed to make them an actual downside buffer.

**Execution lag.** The replay models the gap between a rebalance signal (decided at that date's close) and the trade landing later, since a live user runs a review and only acts afterward. The flag `--t-update-days` (default 1 session, also in the profile's `backtest` section) sets the lag in trading days. The one-time initial capital deployment is not lagged.

**What the clean retriever fixes, and what it does not.** Moving from live WebSearch to the date-honest GKG plus Wayback corpus removes the *retrieval* leak: at a 2023 as-of date the curator can no longer surface a 2026 "best stocks to buy" list, and it only cites articles that carried a period-correct date (server-enforced GKG date bounds plus archived same-date ledes). This is the same hardening the sibling [geo-herd-rider](https://github.com/joehahn/geo-herd-rider) project uses. It does **not** remove the deeper leak: the curator is an LLM whose training postdates the window, so at a 2024 as-of date it may already know that rockets and nuclear won. The candidate selection carries that prior even when no post-date fact is cited, and the one bet that drove the result (RKLB) is exactly the pick most exposed to it. So the backtest stays a hindsight-tinted upper bound, not a clean out-of-sample result, and with a single dominant bet (n=1) it cannot separate skill from luck.

**Forward testing (the only real check).** Hold this config fixed and measure realized performance on rebalances that postdate the model's training cutoff, where the outcomes were genuinely unknowable when decided. Operationally, extend the `backtest` window's `end_date` forward (only the new dates fire, existing curation JSONs are untouched), or compare the live `/review-portfolio` track record against the backtest's expectation. Setting **`forward_split_date`** in the profile's `backtest` section (or `--forward-split-date`) splits `report.md` and the dashboard into an in-sample segment (rebalances on or before the date) and an out-of-sample segment (after it), reporting the curator return, buy-and-hold return, and lift for each. It is reporting only and never touches the optimizer math. If the out-of-sample edge holds near the in-sample edge, the curator adds real signal; if it collapses toward buy-and-hold, the in-sample result was hindsight. This is the next phase of the project.

To reproduce, replay the saved curations through the optimizer (a few seconds, no LLM) and rebuild the dashboard:

```bash
python -m src.cli backtest --curator-runs-dir data/curator_runs/gkg-3yr-kimi \
    --out-dir data/backtest_curator_gkg_3yr_kimi --benchmarks SPY
python -m src.cli dashboard --curator-backtest-dir data/backtest_curator_gkg_3yr_kimi \
    --curator-runs-dir data/curator_runs/gkg-3yr-kimi --curator-model moonshotai/kimi-k2.5 \
    --out docs/backtest_gkg_3yr_kimi.html
```

The optimizer knobs default to the live config in `investor_profile.md`; pass `--max-weight` / `--risk-aversion` / `--lookback-years` only to test a different candidate. Re-curating from scratch (fresh `watchlist-curator` calls via `scripts/backtest_sdk.py`) costs about $1.60 in kimi tokens.

### Prior wave-stage tilt experiment (frozen on `5y-backtest` branch)

The previously-attempted design (LLM classified each technology wave's cycle stage and tilted μ accordingly) didn't survive multi-year backtests: AI tilts subtracted **−2.5%** to **−4.6%** of final value across the same 5y window. Postmortem and preserved artifacts on the [`5y-backtest`](https://github.com/joehahn/portfolio-wave-rider/tree/5y-backtest) branch in `FINDINGS.md`. Three things the tilt design got wrong: granularity (per-wave bucket too coarse — NVDA news ≠ GOOGL news), cadence (quarterly too slow for news with days-long half-life), and magnitude (±20% multiplier mis-calibrated). The curator design sidesteps all three by making the LLM's job a coarse-grained add/remove decision rather than a continuous numerical tilt.

### GBTC inclusion experiment (rejected)

Tested whether putting GBTC (the spot-BTC trust, full 2021→2026 price history) in the starter watchlist alongside `[AAPL, MSFT, GOOGL, NVDA, SPY]` would lift returns. It hurts (this ablation was run at `t_update_days=0`, so its baseline is the +1259.13% / $679,564 same-close run): final value drops from $679,564 to $356,595, annualized return from +68.5% to +48.1%, and max drawdown widens from −45.6% to −60.2%. The story is timing risk on the optimizer's 1.5y lookback — at 2021-03-31 it loaded 61% GBTC off the 2020 rally, was still at 47% GBTC heading into the 2022 crash, and reloaded to 70% GBTC at 2025-01-02 after the 2024 recovery showed up in the trailing window. BTC's high volatility plus its two large drawdowns in this window make it a momentum trap for a mean-variance optimizer with a 1.5y memory. Crypto can be added by an individual user who wants it (declare a "digital assets" wave in `investor_profile.md` and the curator will weigh it on its own merits); it's not in the default demo.

## Automation (cron, cross-platform)

One cron entry handles daily price snapshots and dashboard refresh. Install with:

```bash
./scripts/install_cron.sh
```

The helper appends one line to your crontab (preserving anything else there) that fires `scripts/price_snapshot.sh` Mon-Fri at 16:30 local. Works the same on macOS and Linux. Both scripts resolve their own location, so there's no `PROJ` variable to maintain. `install_cron.sh` is idempotent (re-running is safe). To uninstall: `crontab -e` and delete the matching line.

Each fire runs `snapshot` then `dashboard`, appending timestamped output to `data/snapshot.log`. cron only fires while the machine is awake; missed runs do not auto-replay. Use `--date YYYY-MM-DD` on `snapshot` to backfill a missed day.

The cron refreshes `docs/index.html`. The file is git-tracked but cron does not push — `git status` will show it modified after each run, and a manual `git add docs/index.html && git commit && git push` publishes the refresh.

## Testing

```bash
.venv/bin/pytest tests/    # offline; no network calls, no API keys needed
```

Tests are pure-Python: synthetic price series → returns → optimizer → risk metrics → curator validation → curator-backtest replay. Network-dependent code paths (yfinance, agent calls) are not exercised in CI.

## Things to watch

- **Sample bias.** The realized Sharpe on a 1-2 year window is usually optimistic vs the forward-looking distribution. Returns are non-stationary; vol clusters; means are noisy.
- **Estimation error in `μ`.** Mean-variance amplifies small errors in the expected-return estimate. A weight pinned at the concentration cap is often a symptom of estimation noise, not a real signal. This is the well-known Markowitz blow-up. Run `python -m src.cli backtest` to walk the optimizer forward on real historical data; if the weight-stability L1 metric is small (~0.02 means weights barely move week to week) the estimation noise isn't driving the solution.
- **Curator hindsight risk in backtests.** When the curator runs against a historical as-of date, its job is to use only information available at that date. The agent spec enforces this with a persona reset, WebSearch `before:` filters, a suppression list of post-date events, and a self-critique pass, but the discipline is best-effort, not airtight. Sample a few of the cited evidence URLs against their dates before trusting a backtest's headline number.
- **Numbers come from Python.** If a figure in a report did not come from `src.cli`, that's a bug. The LLM is allowed to write prose; it is not allowed to do arithmetic.

## Roadmap

The next build is one sequenced design, the **escalator scout layer**, named after the project's own metaphor: tickers are escalators that rise, sit flat, or fall over time, and the job is to board the accelerating ones and rotate off the decaying ones. The goal is a measured lift over the current curator+MVO 5y baseline (+1267% realized). Lightweight specialist *scouts* each score every ticker on one leading indicator, a *combiner* fuses the scores into a ranking, the top N go to a turnover-penalized mean-variance optimizer, and the LLM still never touches a final number. The scout layer sits between the curator (which decides *which* escalators exist in the universe, on a slow news-driven clock) and the optimizer (which sets dollar weights). The two places the lift can come from are signal *diversity* (primary-source facts the news-only curator misses) and *timing* (a momentum rank that boards accelerating names and rotates off decaying ones faster than the quarterly curator can). Each step lives on an experimental branch and races the baseline walk-forward before touching the live flow.

1. **Momentum scout + turnover-penalized MVO.** The smallest change with the fastest read on whether timing alone beats the baseline, pure Python, no new dependencies. The curator widens the universe (cap 30-50); a volatility-adjusted trailing-return score (per-ticker `mean(r)/σ(r)` over a tunable lookback, the "escalator speed") ranks them and picks the top N; mean-variance weights those N with an L1 turnover penalty so the optimizer pays a cost to churn positions (the one genuinely-sequential win, captured convexly instead of with RL). Sweep the lookback walk-forward, 3-12 month formation windows are the documented cross-sectional-momentum anomaly, same hygiene as the concentration-cap sweep. A proof-of-concept of the `momentum_score` / `rank_by_momentum` primitives plus the opt-in `turnover_penalty` optimizer param is prototyped on a parked local branch (`escalator-scout-step1`), default off so existing flows are untouched.

2. **One primary-source scout: EDGAR Form 4 insider buys.** Add a second scout reading a primary source the news-only curator misses. SEC EDGAR is free, official, and API-accessible; insiders buying their own stock is one of the few documented informative filing signals. Emit a per-ticker score, measure whether fused picks beat momentum-alone. USASpending.gov contract awards (space / nuclear / quantum waves) and Kalshi prediction-market probabilities (macro regime context) are the next two scouts in line; add one at a time and measure each.

3. **Learned combiner: pooled cross-sectional return model.** Once two or more scouts exist, learn how to weight them. Supervised regression on the (ticker, month) panel, scout scores as features predicting forward *relative* return (demeaned per date), trained across a wide universe with time-blocked, embargoed validation. This is the honest version of the "RL across ticker batches" idea: batches of tickers are not independent games because they share one macro history, and since holding a stock doesn't change its future returns the Q-function degenerates to a regression, so use a regressor. Survivorship bias in yfinance ticker lists is the main data risk and gets a haircut in any headline number.

**Why not RL.** A full PPO / Gymnasium / Stable-Baselines3 design was scoped and rejected as overkill for this retail use case, where the investor's trades do not move the market. The problem decomposes cleanly: the *prediction* half (which escalator rises) has no action-to-state feedback, so the Q-function degenerates to a regression, use a regressor (step 3); the *control* half (when to trade given costs and a risk budget) is genuinely sequential but **convex**, so a turnover-penalized mean-variance optimizer solves it deterministically (step 1), no policy network and no sampling. RL would only earn its complexity under conditions none of which hold here: market impact at institutional size (trades move prices), non-convex frictions (multi-period tax-lot or combinatorial constraints), or many independent training episodes (we have one macro history).
