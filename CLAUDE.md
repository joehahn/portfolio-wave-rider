# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-May-14 <br>
**branch:** main

This repository is a Claude Code demo: optimize a long-horizon stock and ETF portfolio against a user-authored investor profile. The README has the user-facing tour. This file is the rules Claude follows when operating in this repo.

This project was developed using Claude Code. The github is at https://github.com/joehahn/portfolio-wave-rider.

## Design at a glance

The LLM's job is **watchlist curation**, not numeric tilts on expected returns. At each rebalance the `watchlist-curator` agent reads recent news, proposes which tickers should be in the watchlist (adds and removes against the current set), and emits one JSON object. The Python harness validates the JSON against a contract (listing date via yfinance, `max_watchlist_size` cap, no double-adds, no stale removes), applies what survived to `watchlist.csv` (the curator-managed universe) and `data/curation_history.csv`, then runs vanilla mean-variance on the post-change universe (`watchlist.csv` ∪ held positions ∪ anchors). The optimizer never sees any LLM-derived μ adjustments.

A previously-attempted design tilted μ by per-wave "cycle stage" multipliers (buildup 1.20, surge 1.10, peak 0.80, etc.). That subtracted 2–5% of final value across 1y, corner-pick 5y, and fair-start 5y backtests — postmortem in FINDINGS.md on the `5y-backtest` branch. The current design replaces it; the wave-tilt code was stripped from main.

The backtest runs the **post-COVID, normal-regime** window (2022-03-31 → 2025-10-31, ~3.6y, 15 quarterly curator calls; window set in `investor_profile.md`'s `backtest` section), starter watchlist AAPL/MSFT/GOOGL/NVDA/SPY. There is **one optimizer config**, used identically for the live recommend path and the backtest: `λ=0.5 / lookback=0.5y / concentration_cap=0.90 / max_watchlist_size=5` (the profile's `financial_model` + top-level `concentration_cap`; the `backtest` section carries no optimizer overrides, so backtest == live). It backtests at **+2124.8%** vs +187.3% equal-weight buy-and-hold and +58.7% SPY (−48.9% max DD, +137.4% ann; ~+103pp/yr lift over B&H), ending **~90% RKLB / 10% QTUM** — most of the lift rests on the one RKLB position, now pinned at the 0.90 cap (see caveats). `docs/backtest_curator.html` renders this config, replaying the cap=5 curation in `data/curator_runs/postcovid-cap05/`. The optimizer universe also carries three permanent safe-haven anchors (`always_include: [SPY, AGG, IAU]`, see below); at this return-hungry config they draw ~0% weight, so they leave max DD essentially unchanged and only marginally affect the headline.

The backtest is in-sample with respect to the LLM's training knowledge (the curator could have memorized which 2022–2025 tickers later won), so it is a hindsight-tinted upper bound, not proof of a repeatable edge. The intended check for overfitting is **forward testing**: hold this config fixed and measure realized performance on quarters that postdate the model's training cutoff, where outcomes were genuinely unknowable in advance.

The backtest models a realistic next-session execution lag (`t_update_days=1`); the lag is material on this short window (set `--t-update-days 0` for the optimistic same-close upper bound). Setup and reproducibility live in `REFERENCE.md` and `data/backtest_curator_postcovid/report.md`. The published run dir is `data/curator_runs/postcovid-cap05/` (cap=5, the live default; the older cap=8 run is preserved at `data/curator_runs/postcovid/`, and the cap 12/16/24 variants at `data/curator_runs/postcovid-cap{12,16,24}/`); the longer 2021–2026 run (+1267% over its window) is preserved in `data/curator_runs/5y-sweep-cap08/` + `data/backtest_curator_5y/`.

## Ground rules

Keep everything as simple and explainable as possible. Fewest files, least code, fewest functions. Write simple code that is well commented and understood at a glance. This is a demo, not a production system.

**Avoid hacks when doing it right is cheap.** When working around an issue, prefer the correct fix if it takes comparable effort to the workaround. Before adding a bolt-on (a post-hoc refetch, a patch script, a special case), check whether the data or capability is already available upstream and just being dropped — fixing it at the source is usually both cleaner and less code. (Example: author bylines were extracted during the pool's lede fetch and then discarded; the right fix was to keep them in `build_article_pool`, not to re-fetch every page afterward.) Flag a deliberate shortcut explicitly so it doesn't masquerade as the real thing.

**Audience for any prose you generate** (READMEs, reports, code comments): data-science-savvy reader with modest finance and investing knowledge. When prose introduces a finance term, gloss it briefly in plain math or stats terms (e.g., "Sharpe ratio = `(E[r] − r_free) / σ`, signal-to-noise on returns"). The README has a glossary near the top; mirror that level when in doubt.

## The source of truth: `investor_profile.md`

`investor_profile.md` at the repo root is the source of truth for every recommendation. It declares the user's goals, strategy, constraints, exclusions, and the optimizer's mathematical model (`financial_model` YAML section: `risk_aversion` λ, `risk_free_rate`, `lookback_period`, `rebalance_period`, `max_watchlist_size`, `concentration_cap` (the optimizer's per-position max weight, i.e. the `--max-weight` default), and `t_update_days` — the backtest-only execution lag from a rebalance signal to the trade landing, default 1 session), plus the top-level `always_include` (a list of permanent optimizer-universe anchors, e.g. `[SPY, AGG, IAU]`, that the profile unions into the optimizer universe but that sit OUTSIDE the curator's `max_watchlist_size` and OUTSIDE `watchlist.csv`; the curator never adds or removes them and they do not count toward the cap). The optimizer is always mean-variance; `λ` is the only investor-facing knob on the return/variance tradeoff. The CLI's argparse loads these defaults via `portfolio.load_financial_model()`; `concentration_cap` lives inside `financial_model` (a legacy top-level key is still honored as a fallback), so the cap has a single source of truth; CLI flags (`--risk-aversion`, `--max-weight`, etc.) override per invocation. Every skill and subagent must load the profile before reasoning about allocations.

If `investor_profile.md` is missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit it. Do not fall back to a default profile.

A second user-authored file, `news_sources.md`, lists preferred news sources grouped by the waves named in the profile (technology and non-technology). Missing `news_sources.md` is not fatal.

## How conflicts are handled

When the best numerical answer violates a profile constraint, the agent still proposes the violating allocation but flags it explicitly in a "Profile conflicts" section of the final report:

1. Which constraint is violated (cite the line of `investor_profile.md`).
2. The magnitude of the violation.
3. The profile-satisfying alternative and what it costs on the stated goal.

The user decides. Never silently clamp a recommendation to fit the profile.

## Architecture

- **No Claude-Code skills or subagents.** `.claude/skills/` is gone. The flows that used to be slash commands (`/initialize-portfolio`, `/review-portfolio`, `/run-backtest`, `/sweep-max-watchlist-size`) are now CLI subcommands, `scripts/` helpers, and cron shims, so nothing in the pipeline needs a Claude Code session.
- `.claude/agents/watchlist-curator.md` survives as a **prompt file**, not a subagent: `src/curator.py` reads it as the curator's system prompt. `.claude/agents/report-writer.md` is legacy and read by nothing.
- Python:
  - `src/portfolio.py`: every math function (fetch_prices, compute_returns, optimize_portfolio, risk_metrics, analyze, initialize_holdings, snapshot_holdings, recommend_portfolio, apply_curator_decisions, reconstruct_watchlist_at, backtest, curator_backtest, build_dashboard, build_curator_dashboard).
  - `src/curator.py`: the ONE place the curator LLM is invoked, shared by the forward loop and the backtest. Routes `claude-*` ids to the Anthropic SDK and `vendor/model` ids to OpenRouter, parses the JSON decision, retries transient errors, falls back to `no_changes`.
  - `src/retriever.py` + `src/corpus.py`: the news pull (`WebSearchRetriever` forward, `GkgWaybackRetriever` historical) and the corpus it writes to.
  - `src/cli.py`: one entry point with nine subcommands (`init-holdings`, `analyze`, `snapshot`, `recommend`, `curate`, `backtest`, `dashboard`, `pull-news`, `review`) that the cron jobs invoke via Bash.
- `cli review` writes its report to `data/reports/YYYY-MM-DD-review-portfolio.md`.
- Dashboard is a single static `docs/index.html`, regenerated daily by cron. `docs/backtest_curator.html` is the curator-backtest dashboard, regenerated by the backtest render scripts in `scripts/`. Both are git-tracked and served by GitHub Pages from `main/docs/`. cron does not auto-push, so `git add docs/index.html && git commit && git push` is the manual publish step whenever you want the live demo refreshed. Pages deploys via GitHub Actions (`.github/workflows/pages.yml`, `build_type: workflow`), which uploads `docs/` as-is and skips the legacy Jekyll builder — a push touching `docs/**` triggers the deploy; watch it under the repo's Actions tab. (The old legacy branch builder was flaky, failing with instant "Page build failed" errors; `docs/.nojekyll` is kept as belt-and-suspenders.)

## User-maintained inputs

- `investor_profile.md`: goals, constraints, exclusions.
- `holdings.csv`: `ticker,shares` for the user's **real positions only** (`shares > 0`). This is the source of truth for what the user actually owns; it drives snapshots, current allocation, and the "current" side of trade recommendations. **User-edited only** — the curator/cron never writes it (the user updates it after executing trades in their brokerage).
- `watchlist.csv`: single `ticker` column — the **curator-managed optimizer universe**. Auto-written by the biweekly `review` (adds/removes); the user does not normally edit it. The optimizer universe is **`watchlist.csv` ∪ (tickers held in `holdings.csv`, shares>0) ∪ the profile's `always_include` anchors** (see `portfolio._optimizer_universe`). A ticker the curator drops from the watchlist but that the user still holds stays in the universe (so it can be recommended for sale) and exits only when sold out of `holdings.csv`. Anchors (SPY/AGG/IAU) come from the profile, are protected from curator add/remove, and sit outside the `max_watchlist_size` count. Both files are gitignored (personal). Split from the old single `holdings.csv` by `scripts/migrate_watchlist.py`.
- `news_sources.md`: optional curated wave sources.

## Time-series outputs (appended, not overwritten)

- `data/snapshots.csv`: daily per-ticker $ values. Schema: `date, ticker, shares, price, value, total_value`. Idempotent on date; pass `--force` to overwrite.
- `data/recommendations.csv`: optimizer output, one row block per `recommend` run. Schema: `date, ticker, weight, expected_return, annual_volatility, sharpe_ratio, objective`. Idempotent on date; pass `--force` to overwrite same-day runs.
- `data/curation_history.csv`: append-only log of every watchlist change. Schema: `date, action, ticker, wave_bucket, rationale, news_evidence_urls`. `action` is `add` or `remove`; `news_evidence_urls` is a `;`-separated list. The active watchlist at any date is reconstructable by replaying this file from day 0 against the initial `watchlist.csv` rows.
- `data/curator_runs/<run_id>/_starter.json`: per-run input file for `backtest --curator-runs-dir`. Schema: `{starter_watchlist: [...], as_of_dates: [...], start_date, end_date, rebalance_period, initial_usd, lookback_years, max_watchlist_size}`. Created by `scripts/setup_curator_run.py` (for backtest runs) or implicit (for live runs).
- `data/curator_runs/<run_id>/<YYYY-MM-DD>-curation.json`: one file per rebalance, the full JSON return from a watchlist-curator agent call. Schema matches the agent's output contract. The canonical backtest payloads live in `postcovid/` (one JSON per quarter-end, cap=8, the post-COVID window all four sweeps share); the swept `max_watchlist_size` variants live in `postcovid-cap{05,12,16,24}/`. The `5y-sweep-cap*/` and `5y-quarterly/` dirs are the historical 5y-window (2021→2026) record from before the sweeps were unified onto the post-COVID window; `live/` accumulates one file per live `review` run.
- `data/curator_latest.json`: the most recent live `review` curator output. Overwritten each run; gitignored.
- `data/thesis_baseline.json`: the day-0 allocation. Schema: `{date, allocations_usd, reasoning, holdings}`. Written once at portfolio inception (originally by the retired `/initialize-portfolio` skill; nothing writes it now) and read-only thereafter. `build_dashboard` reads its `date` to scope the live dashboard's time-series charts, and `scripts/build_forward_dashboard.py` reads it as the inception anchor. Both reads are guarded, so a missing file degrades gracefully rather than crashing.

These are the user's history. Don't break their schemas. If you must extend them, add columns at the end and keep existing ones.

## Automation (cron, cross-platform)

Three cron scripts drive the forward loop: `scripts/news_pull.sh` (daily at 18:30 local, incl. weekends) pulls news into `data/forward_corpus/`, then render-refreshes the RBS (Retriever Bootstrap) dashboard off that fresh corpus (`scripts/build_bootstrap_dashboard.py`, no LLM — it lives here, not the snapshot job, because RBS visualizes the corpus this job writes); `scripts/price_snapshot.sh` (weekday at 16:30 local) runs the price snapshot, then three render-only dashboard refreshes off the fresh snapshot: `dashboard` (the live `index.html`), `scripts/refresh_cbs.py` (live-extends the CBS equity curve, no LLM), and `scripts/build_forward_dashboard.py` (the Forwardtest (FT) dashboard: value/holdings/trades). All three refreshes live in this one job because they read the same-day `snapshots.csv` and must run after it; and `scripts/review_curation.sh` (weekday at 19:00 local) runs the self-gating `review --if-due`. The review was split out of the snapshot job and moved to 19:00 so it runs AFTER the 18:30 news pull and thus curates on same-day news; `--if-due` self-gates to `rebalance_period` (biweekly), so firing every weekday is safe and the cadence stays profile-driven (no cron edit to change it). Users install them by pasting three lines into `crontab -e` (README §6 has the block, using a `PWR_path` variable); `scripts/install_cron.sh` is an optional helper that adds all three idempotently. All scripts resolve their own location, are pure-bash, log to `data/snapshot.log`, and work on macOS and Linux.

cron only fires while the machine is awake; missed runs do not auto-replay. Use `--date YYYY-MM-DD` on `snapshot` to backfill.

The `price_snapshot.sh` cron refreshes `docs/index.html` (the dashboard CLI's default `--out`), `docs/curator_bootstrap.html` (the CBS refresh), and `docs/forward_dashboard.html` (the FT refresh). All are git-tracked but cron does not push — `git status` will show them modified after each run, and a manual `git add docs/ && git commit && git push` publishes the refresh. The push (touching `docs/**`) fires the `Deploy dashboard to Pages` GitHub Action, which deploys the site; there is no separate publish step.

`recommend` is invoked by the biweekly `review --if-due` cron (`scripts/review_curation.sh`) and by a manual `review` run; the daily `price_snapshot.sh` cron only runs `snapshot` + `dashboard` + the CBS refresh, never `recommend`. There is no daily `recommend` entry — the curator's add/remove decisions are the only thing changing the optimizer's universe between rebalances, so a between-review `recommend` would produce a near-duplicate row.

## Repo rules

- Never write financial advice without citing the profile.
- Numbers come from Python, not from the LLM. If a subagent reports a number, it must have come from a `src.cli` invocation in the same turn.
- **Never hardcode a value that has an input-file source.** Every optimizer/backtest/curator knob lives in `investor_profile.md` (via `load_financial_model` / `load_backtest_config` / `load_forward_config` / `load_wave_thesis` / `load_exclusions`); read it from there, never inline a literal (`risk_free_rate=0.04`, `initial_usd=50000`, `ANCHORS=[...]`, the wave thesis) that silently shadows the profile. A hardcoded default that happens to match the profile today is still a bug: it makes a future profile edit a no-op. This is the same fail-loud principle as the required `thesis`/`exclusions` args on `curate()`. When a value is genuinely swept (cap/λ/lookback in a sweep), read the *non-swept* knobs from the profile so only the intended dimension varies.
- Don't modify `investor_profile.md` or `holdings.csv` without the user's explicit consent. (`holdings.csv` is user-edited only — the user updates it after real trades; `watchlist.csv` is the curator/cron's file.)
- Reports and the dashboard under `data/` are session artifacts; gitignored and safe to regenerate. The two appended CSVs (`snapshots.csv`, `recommendations.csv`) are also under `data/` but are the user's history; don't truncate them without consent.

## Running Python

- The venv is at `.venv/`. Activate with `source .venv/bin/activate`, or invoke directly: `.venv/bin/python -m src.cli <subcommand>`.
- Tests: `.venv/bin/pytest tests/`.
