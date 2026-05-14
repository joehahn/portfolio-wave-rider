# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

This repository is a Claude Code demo: optimize a long-horizon stock and ETF portfolio against a user-authored investor profile. The README has the user-facing tour. This file is the rules Claude follows when operating in this repo.

This project was developed using Claude Code. The github is at https://github.com/joehahn/portfolio-wave-rider.

## Design at a glance

The LLM's job is **watchlist curation**, not numeric tilts on expected returns. At each rebalance the `watchlist-curator` agent reads recent news, proposes which tickers should be in the watchlist (adds and removes against the current set), and emits one JSON object. The Python harness validates the JSON against a contract (listing date via yfinance, `max_watchlist_size` cap, no double-adds, no stale removes), applies what survived to `holdings.csv` and `data/curation_history.csv`, then runs vanilla mean-variance on the post-change watchlist. The optimizer never sees any LLM-derived μ adjustments.

A previously-attempted design tilted μ by per-wave "cycle stage" multipliers (buildup 1.20, surge 1.10, peak 0.80, etc.). That subtracted 2–5% of final value across 1y, corner-pick 5y, and fair-start 5y backtests — postmortem in FINDINGS.md on the `5y-backtest` branch. The current design replaces it; the wave-tilt code was stripped from main.

The current 5y backtest (Sept 2021 → Apr 2026, starter watchlist of AAPL/MSFT/GOOGL/SPY/AGG, 20 quarterly curator calls) lifts realized return to **+135.5%** vs **+103.7%** buy-and-hold, **+80.2%** fixed-watchlist rebalance, and **+78.2%** SPY. Rendered at [docs/backtest_curator.html](https://joehahn.github.io/portfolio-wave-rider/backtest_curator.html). The full setup and reproducibility notes live in `REFERENCE.md` and `data/backtest_curator_5y/report.md`.

## Ground rules

Keep everything as simple and explainable as possible. Fewest files, least code, fewest functions. Write simple code that is well commented and understood at a glance. This is a demo, not a production system.

**Audience for any prose you generate** (READMEs, reports, code comments): data-science-savvy reader with modest finance and investing knowledge. When prose introduces a finance term, gloss it briefly in plain math or stats terms (e.g., "Sharpe ratio = `(E[r] − r_free) / σ`, signal-to-noise on returns"). The README has a glossary near the top; mirror that level when in doubt.

## The source of truth: `investor_profile.md`

`investor_profile.md` at the repo root is the source of truth for every recommendation. It declares the user's goals, strategy, constraints, exclusions, and the optimizer's mathematical model (`financial_model` YAML section: `risk_aversion` λ, `risk_free_rate`, `lookback_period`, `rebalance_period`, `max_watchlist_size`). The optimizer is always mean-variance; `λ` is the only investor-facing knob on the return/variance tradeoff. The CLI's argparse loads the `financial_model` defaults via `portfolio.load_financial_model()`; CLI flags (`--risk-aversion`, etc.) override per invocation. Every skill and subagent must load the profile before reasoning about allocations.

If `investor_profile.md` is missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit it. Do not fall back to a default profile.

A second user-authored file, `news_sources.md`, lists preferred news sources grouped by the technology waves named in the profile. Missing `news_sources.md` is not fatal.

## How conflicts are handled

When the best numerical answer violates a profile constraint, the agent still proposes the violating allocation but flags it explicitly in a "Profile conflicts" section of the final report:

1. Which constraint is violated (cite the line of `investor_profile.md`).
2. The magnitude of the violation.
3. The profile-satisfying alternative and what it costs on the stated goal.

The user decides. Never silently clamp a recommendation to fit the profile.

## Architecture

- Subagents (`.claude/agents/`): two LLM specialists with narrow tool allowlists.
  - `watchlist-curator` (Sonnet): reads recent news and the investor's wave thesis at each rebalance; proposes adds and removes to the active watchlist subject to a `max_watchlist_size` cap and a listing-date guardrail (enforced by the Python harness). Returns JSON; does not write files. When the harness passes a historical as-of date (backtest mode) the agent applies strict discipline: persona reset, WebSearch `before:` filters, suppression list from `scripts/post_date_events.py`, self-critique pass. Live mode skips all of that since the agent should use current information.
  - `report-writer` (Sonnet): synthesizes the analyze output and curator output into the final markdown report.
- Skills (`.claude/skills/`): two slash commands.
  - `/initialize-portfolio` (one-shot): reads the profile and an all-zero holdings.csv, produces a thesis-driven dollar allocation across the watchlist, calls `init-holdings` to convert dollars to shares, runs `snapshot --force`, persists the allocation to `data/thesis_baseline.json`, and writes a thesis-only report. No optimizer, no news. Refuses to run if holdings already has positions or thesis_baseline.json already exists.
  - `/review-portfolio` (recurring): the live curator-driven monthly review. Fires the watchlist-curator against today's date, applies adds/removes via `curate`, runs `analyze` and `recommend` on the post-change watchlist, calls the report-writer, and refreshes the dashboard. See `.claude/skills/review-portfolio/SKILL.md` for the six-step flow.
- All Python in two files:
  - `src/portfolio.py`: every math function (fetch_prices, compute_returns, optimize_portfolio, risk_metrics, analyze, initialize_holdings, snapshot_holdings, recommend_portfolio, apply_curator_decisions, reconstruct_watchlist_at, backtest, curator_backtest, build_dashboard, build_curator_dashboard).
  - `src/cli.py`: one entry point with seven subcommands (`init-holdings`, `analyze`, `curate`, `snapshot`, `recommend`, `backtest`, `dashboard`) that the skills and cron jobs invoke via Bash.
- Reports are written to `data/reports/YYYY-MM-DD-<skill>.md`.
- Dashboard is a single static `docs/index.html`, regenerated daily by cron. `docs/backtest_curator.html` is the (one-off, hand-refreshed) curator-backtest dashboard. Both are git-tracked and served by GitHub Pages from `main/docs/`. cron does not auto-push, so `git add docs/index.html && git commit && git push` is the manual publish step whenever you want the live demo refreshed.

## User-maintained inputs

- `investor_profile.md`: goals, constraints, exclusions.
- `holdings.csv`: `ticker,shares` for every ticker the user wants tracked. This file is the **watchlist universe** — the set of tickers passed to `optimize_portfolio` (so the optimizer can only assign weight to these tickers). `shares=0` is valid: it adds the ticker to the universe for optimization without representing a real position. To add a ticker, append `<TICKER>,0` and the next run picks it up; to remove one, delete the row.
- `news_sources.md`: optional curated wave sources.

## Time-series outputs (appended, not overwritten)

- `data/snapshots.csv`: daily per-ticker $ values. Schema: `date, ticker, shares, price, value, total_value`. Idempotent on date; pass `--force` to overwrite.
- `data/recommendations.csv`: optimizer output, one row block per `recommend` run. Schema: `date, ticker, weight, expected_return, annual_volatility, sharpe_ratio, objective`. Idempotent on date; pass `--force` to overwrite same-day runs.
- `data/curation_history.csv`: append-only log of every watchlist change. Schema: `date, action, ticker, wave_bucket, rationale, news_evidence_urls`. `action` is `add` or `remove`; `news_evidence_urls` is a `;`-separated list. The active watchlist at any date is reconstructable by replaying this file from day 0 against `holdings.csv`'s initial rows.
- `data/curator_runs/<run_id>/_starter.json`: per-run input file for `backtest --curator-runs-dir`. Schema: `{starter_watchlist: [...], start_date, end_date, rebalance_period, initial_usd, lookback_years, max_watchlist_size}`. Created by `scripts/setup_curator_run.py` (for backtest runs) or implicit (for live runs).
- `data/curator_runs/<run_id>/<YYYY-MM-DD>-curation.json`: one file per rebalance, the full JSON return from a watchlist-curator agent call. Schema matches the agent's output contract. The `5y-quarterly/` subdir holds the 20 backtest payloads; `live/` accumulates one file per `/review-portfolio` run.
- `data/curator_latest.json`: the most recent `/review-portfolio` curator output. Overwritten each run; gitignored.
- `data/thesis_baseline.json`: one-time artifact written by `/initialize-portfolio`. Schema: `{date, allocations_usd, reasoning, holdings}`. Read-only after creation; `build_dashboard` reads its `date` to scope the live dashboard's time-series charts. Delete the file manually only if you want to redo the thesis from scratch (then re-run `/initialize-portfolio`).

These are the user's history. Don't break their schemas. If you must extend them, add columns at the end and keep existing ones.

## Automation (cron, cross-platform)

One cron entry handles daily price snapshots. The shipped `scripts/cron_snapshot.sh` resolves its own location (no `PROJ` variable to maintain) and runs snapshot + dashboard in sequence, appending timestamped output to `data/snapshot.log`. Generate the crontab line from the project root with:

```bash
echo "30 16 * * 1-5  $(pwd)/scripts/cron_snapshot.sh"
```

Then `crontab -e` and paste. Works the same on macOS and Linux. cron only fires when the machine is awake at the trigger time; missed runs do not auto-replay. Use `--date YYYY-MM-DD` on `snapshot` to backfill.

The cron call refreshes `docs/index.html` (the dashboard CLI's default `--out`). The file is git-tracked but cron does not push — `git status` will show it modified after each run, and a manual `git add docs/index.html && git commit && git push` publishes the refresh.

`recommend` is invoked by `/review-portfolio` at each monthly review; cron only runs `snapshot` and `dashboard`. There is no daily/weekly cron entry for `recommend` — the curator's add/remove decisions are the only thing changing the optimizer's universe between monthly reviews, so a between-review `recommend` would produce a near-duplicate row.

## Repo rules

- Never write financial advice without citing the profile.
- Numbers come from Python, not from the LLM. If a subagent reports a number, it must have come from a `src.cli` invocation in the same turn.
- Don't modify `investor_profile.md` or `holdings.csv` without the user's explicit consent.
- Reports and the dashboard under `data/` are session artifacts; gitignored and safe to regenerate. The two appended CSVs (`snapshots.csv`, `recommendations.csv`) are also under `data/` but are the user's history; don't truncate them without consent.

## Running Python

- The venv is at `.venv/`. Activate with `source .venv/bin/activate`, or invoke directly: `.venv/bin/python -m src.cli <subcommand>`.
- Tests: `.venv/bin/pytest tests/`.
