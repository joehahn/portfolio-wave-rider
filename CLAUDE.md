# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-04 <br>
**branch:** main

This repository is a Claude Code demo: optimize a long-horizon stock and ETF portfolio against a user-authored investor profile. The README has the user-facing tour. This file is the rules Claude follows when operating in this repo.

This project was developed using Claude Code. The github is at https://github.com/joehahn/portfolio-wave-rider.

## Status: in-flight rebuild

`main` is currently in a transitional state. The previous wave-stage-tilt design (an LLM-driven `wave_views` tilt on μ) didn't pan out in 5-year backtests; see FINDINGS.md on the `5y-backtest` branch for the postmortem. The replacement design is an LLM-as-watchlist-curator: the LLM proposes which tickers should be in the watchlist over time, the optimizer runs vanilla mean-variance on whatever watchlist results.

Progress on the rebuild:

- **Stage A (done):** ripped all tilt code from main. Six CLI subcommands still work.
- **Stage B (done):** defined the curator contract. `.claude/agents/watchlist-curator.md` specifies the inputs the agent receives, the JSON it returns, and the guardrails on its proposed adds. `investor_profile.example.md` gains `rebalance_period` and `max_watchlist_size` fields. No code consumes the contract yet.
- **Stage C (next):** wire the contract. New `curate` CLI subcommand applies adds/removes to `holdings.csv` and appends to `data/curation_history.csv`. The `backtest` subcommand grows a curator-driven walk-forward variant that calls the agent at each rebalance with `as_of_date` discipline.
- **Stage D:** rewrite `/review-portfolio` and `/run-backtest` skills against the new flow; update `report-writer.md`; refresh `docs/*.html`.

Until stage D lands:

- `/review-portfolio` and `/run-backtest` slash commands return "skill not found".
- The `1y-baseline` branch holds the last working 1-year demo.
- GitHub Pages serves from `1y-baseline`, so the public demo URL is unaffected by main's scaffolding.

## Ground rules

Keep everything as simple and explainable as possible. Fewest files, least code, fewest functions. Write simple code that is well commented and understood at a glance. This is a demo, not a production system.

**Audience for any prose you generate** (READMEs, reports, code comments): data-science-savvy reader with modest finance and investing knowledge. When prose introduces a finance term, gloss it briefly in plain math or stats terms (e.g., "Sharpe ratio = `(E[r] − r_free) / σ`, signal-to-noise on returns"). The README has a glossary near the top; mirror that level when in doubt.

## The source of truth: `investor_profile.md`

`investor_profile.md` at the repo root is the source of truth for every recommendation. It declares the user's goals, strategy, constraints, exclusions, and the optimizer's mathematical model (`financial_model` YAML section: `objective`, `risk_aversion` λ, `risk_free_rate`, `lookback_period`, `rebalance_period`, `max_watchlist_size`). The CLI's argparse loads the `financial_model` defaults via `portfolio.load_financial_model()`; CLI flags (`--objective`, `--risk-aversion`, etc.) override per invocation. Every skill and subagent must load the profile before reasoning about allocations.

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
  - `watchlist-curator`: reads recent news and the investor's wave thesis at each rebalance; proposes adds and removes to the active watchlist (subject to a `max_watchlist_size` cap and a listing-date guardrail enforced by the harness). Returns JSON; does not write files. Contract is defined as of stage B but no Python code consumes it yet.
  - `report-writer`: synthesizes the analysis and curator payloads into the final markdown report. Inputs will change once stage D wires the curator into `/review-portfolio`.
- Skills (`.claude/skills/`): one slash command active right now.
  - `/initialize-portfolio` (one-shot): reads the profile and an all-zero holdings.csv, produces a thesis-driven dollar allocation across the watchlist, calls `init-holdings` to convert dollars to shares, runs `snapshot --force`, persists the allocation to `data/thesis_baseline.json`, and writes a thesis-only report. No optimizer, no news. Refuses to run if holdings already has positions or thesis_baseline.json already exists.
  - `/review-portfolio` and `/run-backtest` are absent on this branch; both come back in the curator rebuild.
- All Python in two files:
  - `src/portfolio.py`: every math function (fetch_prices, compute_returns, optimize_portfolio, risk_metrics, analyze, initialize_holdings, snapshot_holdings, recommend_portfolio, backtest, build_dashboard, render_news_page).
  - `src/cli.py`: one entry point with six subcommands (`init-holdings`, `analyze`, `snapshot`, `recommend`, `backtest`, `dashboard`) that the skill and cron jobs invoke via Bash. `backtest` is a one-off spot-check tool, not part of any cron flow.
- Reports are written to `data/reports/YYYY-MM-DD-<skill>.md`.
- Dashboard is a single static `docs/index.html`, regenerated after each snapshot or recommend run. The same file is what GitHub Pages serves at the public-demo URL; cron does not auto-push, so `git add docs/index.html && git commit && git push` is the manual publish step whenever you want the live demo refreshed.

## User-maintained inputs

- `investor_profile.md`: goals, constraints, exclusions.
- `holdings.csv`: `ticker,shares` for every ticker the user wants tracked. This file is the **watchlist universe** — the set of tickers passed to `optimize_portfolio` (so the optimizer can only assign weight to these tickers). `shares=0` is valid: it adds the ticker to the universe for optimization without representing a real position. To add a ticker, append `<TICKER>,0` and the next run picks it up; to remove one, delete the row.
- `news_sources.md`: optional curated wave sources.

## Time-series outputs (appended, not overwritten)

- `data/snapshots.csv`: daily per-ticker $ values. Schema: `date, ticker, shares, price, value, total_value`. Idempotent on date; pass `--force` to overwrite.
- `data/recommendations.csv`: optimizer output, one row block per `recommend` run. Schema: `date, ticker, weight, expected_return, annual_volatility, sharpe_ratio, objective`. Idempotent on date; pass `--force` to overwrite same-day runs.
- `data/curation_history.csv` *(lands in stage C)*: append-only log of every watchlist change. Schema: `date, action, ticker, wave_bucket, rationale, news_evidence_urls`. `action` is `add` or `remove`; `news_evidence_urls` is a `;`-separated list. The active watchlist at any date is reconstructable by replaying this file from day 0 against `holdings.csv`'s initial rows. Drives the dashboard's watchlist-composition-over-time chart.
- `data/thesis_baseline.json`: one-time artifact written by `/initialize-portfolio`. Schema: `{date, allocations_usd, reasoning, holdings}`. Read-only after creation; `build_dashboard` reads its `date` to scope the live dashboard's time-series charts. Delete the file manually only if you want to redo the thesis from scratch (then re-run `/initialize-portfolio`).

These are the user's history. Don't break their schemas. If you must extend them, add columns at the end and keep existing ones.

## Automation (cron, cross-platform)

One cron entry handles daily price snapshots. The exact crontab installed on the author's machine:

```cron
PROJ=/Users/joehahn/Library/CloudStorage/Dropbox/prog/claude/portfolio-wave-rider
# Daily snapshot + dashboard refresh, Mon-Fri 16:30 local
30 16 * * 1-5  cd $PROJ && .venv/bin/python -m src.cli snapshot && .venv/bin/python -m src.cli dashboard >> data/snapshot.log 2>&1
```

The cron call refreshes `docs/index.html` (the dashboard CLI's default `--out`). The file is git-tracked but cron does not push — `git status` will show it modified after each run, and a manual `git add docs/index.html && git commit && git push` publishes the refresh.

Set `PROJ` to wherever you cloned the repo, then `crontab -e` and paste. Works the same on macOS and Linux. cron only fires when the machine is awake at the trigger time; missed runs do not auto-replay. Use `--date YYYY-MM-DD` to backfill.

`recommend` is currently a manual invocation. The curator rebuild will reattach it to the next-gen `/review-portfolio` skill.

## Repo rules

- Never write financial advice without citing the profile.
- Numbers come from Python, not from the LLM. If a subagent reports a number, it must have come from a `src.cli` invocation in the same turn.
- Don't modify `investor_profile.md` or `holdings.csv` without the user's explicit consent.
- Reports and the dashboard under `data/` are session artifacts; gitignored and safe to regenerate. The two appended CSVs (`snapshots.csv`, `recommendations.csv`) are also under `data/` but are the user's history; don't truncate them without consent.

## Running Python

- The venv is at `.venv/`. Activate with `source .venv/bin/activate`, or invoke directly: `.venv/bin/python -m src.cli <subcommand>`.
- Tests: `.venv/bin/pytest tests/`.
