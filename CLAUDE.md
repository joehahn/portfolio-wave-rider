# Portfolio Wave Rider

A Claude Code demo that optimizes an investment portfolio against a
user-authored investor profile, using subagents for specialist roles and
skills as the invocation surface.

## Ground rules

This project is meant to be an explainable demo that shows off the use of
Claude and skills and subagents to deliver a result. Keep everything as simple
and explainable as possible — fewest files, least code, fewest functions.

When writing code, write simple code that is well commented and understood
at a glance.

## The north star: `investor_profile.md`

`investor_profile.md` (at the repo root) is the source of truth for every
recommendation. It declares the user's goals, strategy, constraints, and
exclusions. **Every skill and subagent must load it before reasoning about
allocations.**

If `investor_profile.md` is missing or empty, stop and run `/init-profile` —
do not fall back to a default profile.

A second user-authored file, `news_sources.md`, lists preferred news
sources grouped by the technology waves named in the profile. The
`news-researcher` subagent tries these sources first before falling back
to open web search. Missing `news_sources.md` is not fatal.

## How conflicts are handled

When the best numerical answer violates a profile constraint, the agent
still proposes the violating allocation but must flag it explicitly in a
"Profile conflicts" section of the final report:

1. Which constraint is violated (cite the line of `investor_profile.md`).
2. The magnitude of the violation.
3. The profile-satisfying alternative and what it costs on the stated goal.

The user decides. Never silently clamp a recommendation to fit the profile.

## Architecture

- **Subagents** live in `.claude/agents/`. Each is a specialist with a
  narrow tool allowlist and its own context. They don't talk to each
  other — they return structured summaries to the orchestrating skill.
- **Skills** live in `.claude/skills/`. A skill is the top-level
  workflow a user invokes: `/init-profile`, `/optimize-portfolio`,
  `/rebalance`. Skills orchestrate subagents and produce the final
  report.
- **All Python lives in two files**:
  - `src/portfolio.py` — every math function (fetch, compute_returns,
    optimize_portfolio, risk_metrics, backtest, snapshot_holdings,
    recommend_portfolio) plus a tiny disk-backed handle store.
  - `src/cli.py` — one entry point with subcommands (`fetch-data`,
    `optimize`, `risk`, `backtest`, `snapshot`, `recommend`) that
    subagents and cron jobs invoke via Bash.
- **State** is persisted under `data/state/` as pickle files keyed by
  handles (`prices_1`, `returns_1`). This lets separate Python
  processes share DataFrames.
- **Reports** are written to `data/reports/YYYY-MM-DD-<skill>.md`.

## User-maintained inputs

- `investor_profile.md` — goals, constraints, exclusions.
- `holdings.csv` — `ticker,shares` for every ticker the user wants
  tracked. shares=0 is valid (price-only history before they buy in).
- `news_sources.md` — optional curated wave sources.

## Time-series outputs (appended, not overwritten)

- `data/snapshots.csv` — daily per-ticker `$` values. Schema:
  `date, ticker, shares, price, value, total_value`. Idempotent on
  date; pass `--force` to overwrite.
- `data/recommendations.csv` — weekly lightweight optimizer output.
  Schema: `date, ticker, weight, expected_return, annual_volatility,
  sharpe_ratio, objective`. Idempotent on date; pass `--force` to
  overwrite.

These are the user-facing time series for trend visualization. Don't
break their schemas; if you must extend, add columns at the end and
keep existing ones.

## Automation (macOS launchd)

Two launchd plists live at `~/Library/LaunchAgents/`:

- `com.user.portfolio-snapshot.plist` — Mon–Fri 16:30 local, runs
  `python -m src.cli snapshot`. Logs to `data/snapshot.log`.
- `com.user.portfolio-recommend.plist` — Fri 17:00 local, runs
  `python -m src.cli recommend`. Logs to `data/recommend.log`.

The weekly `recommend` is the **lightweight** sibling of
`/optimize-portfolio` — pure Python, no news-researcher, no wave-stage
tilts. Use the full skill when the user wants fresh wave classification
and a written report.

## Repo rules

- Never write financial advice without citing the profile.
- Numbers come from Python, not from the LLM. If a subagent reports a
  number, it must have come from a `src.cli` invocation in the same turn.
- Don't modify `investor_profile.md` or `holdings.csv` without the
  user's explicit consent.
- Reports and handles under `data/` are session artifacts; gitignored
  and safe to delete. The two appended CSVs (`snapshots.csv`,
  `recommendations.csv`) are also under `data/` but are the user's
  history — don't truncate them without consent.

## Running Python

- The venv is at `.venv/`. Activate with `source .venv/bin/activate`,
  or invoke directly: `.venv/bin/python -m src.cli <subcommand>`.
- Tests: `.venv/bin/pytest tests/`.
