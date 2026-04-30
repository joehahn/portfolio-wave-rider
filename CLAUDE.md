# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-April-30 <br>
**branch:** main

This repository is a Claude Code demo: optimize a long-horizon stock and ETF portfolio against a user-authored investor profile. The README has the user-facing tour. This file is the rules Claude follows when operating in this repo.

This project was developed using Claude Code. The github is at https://github.com/joehahn/portfolio-wave-rider.

## Ground rules

Keep everything as simple and explainable as possible. Fewest files, least code, fewest functions. Write simple code that is well commented and understood at a glance. This is a demo, not a production system.

## The source of truth: `investor_profile.md`

`investor_profile.md` at the repo root is the source of truth for every recommendation. It declares the user's goals, strategy, constraints, and exclusions. Every skill and subagent must load it before reasoning about allocations.

If `investor_profile.md` is missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit it. Do not fall back to a default profile.

A second user-authored file, `news_sources.md`, lists preferred news sources grouped by the technology waves named in the profile. The `news-researcher` subagent tries these sources first before falling back to open web search. Missing `news_sources.md` is not fatal.

## How conflicts are handled

When the best numerical answer violates a profile constraint, the agent still proposes the violating allocation but flags it explicitly in a "Profile conflicts" section of the final report:

1. Which constraint is violated (cite the line of `investor_profile.md`).
2. The magnitude of the violation.
3. The profile-satisfying alternative and what it costs on the stated goal.

The user decides. Never silently clamp a recommendation to fit the profile.

## Architecture

- Subagents (`.claude/agents/`): two LLM specialists with narrow tool allowlists.
  - `news-researcher`: picks wave-aligned news per ticker, classifies each wave's stage, returns a `wave_views` mapping that the optimizer consumes as a tilt on expected returns.
  - `report-writer`: synthesizes the analysis and news payloads into the final markdown report.
- Skill (`.claude/skills/`): one slash command.
  - `/review-portfolio`: orchestrates the news-researcher, runs the `analyze` CLI, then invokes the report-writer and refreshes the dashboard.
- All Python in two files:
  - `src/portfolio.py`: every math function (fetch_prices, compute_returns, optimize_portfolio, risk_metrics, analyze, snapshot_holdings, recommend_portfolio, build_dashboard).
  - `src/cli.py`: one entry point with four subcommands (`analyze`, `snapshot`, `recommend`, `dashboard`) that the skill and cron jobs invoke via Bash.
- Reports are written to `data/reports/YYYY-MM-DD-<skill>.md`.
- Dashboard is a single static `data/dashboard.html`, regenerated after each snapshot or recommend run and at the end of `/review-portfolio`.

## User-maintained inputs

- `investor_profile.md`: goals, constraints, exclusions.
- `holdings.csv`: `ticker,shares` for every ticker the user wants tracked. shares=0 is valid (price-only history before the user buys in).
- `news_sources.md`: optional curated wave sources.

## Time-series outputs (appended, not overwritten)

- `data/snapshots.csv`: daily per-ticker $ values. Schema: `date, ticker, shares, price, value, total_value`. Idempotent on date; pass `--force` to overwrite.
- `data/recommendations.csv`: weekly lightweight optimizer output. Schema: `date, ticker, weight, expected_return, annual_volatility, sharpe_ratio, objective`. Idempotent on date; pass `--force` to overwrite.

These are the user's history. Don't break their schemas. If you must extend them, add columns at the end and keep existing ones.

## Automation (cron, cross-platform)

Two cron entries handle the daily and weekly automation. The exact crontab installed on the author's machine:

```cron
PROJ=/Users/joehahn/Library/CloudStorage/Dropbox/prog/claude/portfolio-wave-rider
# Daily snapshot + dashboard refresh, Mon-Fri 16:30 local
30 16 * * 1-5  cd $PROJ && .venv/bin/python -m src.cli snapshot && .venv/bin/python -m src.cli dashboard >> data/snapshot.log 2>&1
# Weekly recommend + dashboard refresh, Fri 17:00 local
0  17 * * 5    cd $PROJ && .venv/bin/python -m src.cli recommend && .venv/bin/python -m src.cli dashboard >> data/recommend.log 2>&1
```

Set `PROJ` to wherever you cloned the repo, then `crontab -e` and paste. Works the same on macOS and Linux. cron only fires when the machine is awake at the trigger time; missed runs do not auto-replay. Use `--date YYYY-MM-DD` to backfill.

The weekly `recommend` is the lightweight sibling of `/review-portfolio`: pure Python, no news-researcher, no wave-stage tilts. Use the full skill when the user wants fresh wave classification and a written report.

## Repo rules

- Never write financial advice without citing the profile.
- Numbers come from Python, not from the LLM. If a subagent reports a number, it must have come from a `src.cli` invocation in the same turn.
- Don't modify `investor_profile.md` or `holdings.csv` without the user's explicit consent.
- Reports and the dashboard under `data/` are session artifacts; gitignored and safe to regenerate. The two appended CSVs (`snapshots.csv`, `recommendations.csv`) are also under `data/` but are the user's history; don't truncate them without consent.

## Running Python

- The venv is at `.venv/`. Activate with `source .venv/bin/activate`, or invoke directly: `.venv/bin/python -m src.cli <subcommand>`.
- Tests: `.venv/bin/pytest tests/`.
