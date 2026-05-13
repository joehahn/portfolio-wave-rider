# /review-portfolio

Live curator-driven portfolio review. Each run sends the watchlist-curator agent out to read recent news, propose adds and removes against the current watchlist, applies the resulting changes to `holdings.csv` and `data/curation_history.csv`, runs the optimizer on the new watchlist, writes a profile-aware report, and refreshes the live dashboard.

The math here is identical to the curator backtest (`backtest --curator-runs-dir`); the only difference is that this skill fires ONE curator call against today's date (no as-of-date discipline, no suppression list) instead of replaying pre-collected payloads.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit. Do not fall back to a default.
2. Read `holdings.csv` for the current watchlist. Every ticker in this file is passed to the curator as `current_watchlist` (including rows with `shares=0`).
3. **Empty-holdings guard**: if every row in `holdings.csv` has `shares == 0`, stop and tell the user this is a fresh repo; they should run `/initialize-portfolio` first to set the thesis allocation. Do not proceed.
4. Read `data/thesis_baseline.json` if it exists. Its contents (`date`, `allocations_usd`, `reasoning`, `holdings`) are passed to the report-writer so every review report can render the thesis-vs-recommended comparison.
5. Load the profile's `financial_model` settings via `python -m src.cli` (the CLI does this automatically via `portfolio.load_financial_model`). Defaults: `rebalance_period: monthly`, `max_watchlist_size: 12`.

## Orchestration

### Step 1 — fire the watchlist-curator (Task)

Spawn the `watchlist-curator` subagent. Pass a self-contained prompt with these per-call inputs:

```json
{
  "as_of_date": "<today, YYYY-MM-DD>",
  "current_watchlist": [<ticker list from holdings.csv>],
  "max_watchlist_size": <from profile, default 12>,
  "rebalance_period": "<from profile, default monthly>",
  "recent_news_lookback_days": <30 for monthly, 90 for quarterly>,
  "profile_wave_thesis": "<prose extracted from the 'Strategy & beliefs' section of investor_profile.md>",
  "exclusions": [<exclusions array from profile YAML>]
}
```

This is a LIVE run, not a backtest. **Do NOT pass a `post_date_events` suppression list** and **do NOT instruct the agent to apply WebSearch `before:` filters**. The agent should use any current information available. The "as-of-date discipline" sections of the watchlist-curator spec only apply when `as_of_date` is in the past; the agent will skip them automatically.

If the watchlist is already at `max_watchlist_size`, add a sentence in the prompt reminding the agent that any add must be paired with a remove.

Save the agent's JSON return to:
- `data/curator_latest.json` (overwritten each run; the dashboard reads this for the "Latest curation" panel)
- `data/curator_runs/live/<today>-curation.json` (archived; accumulates a history of every live run)

### Step 2 — apply curate (Bash)

```bash
.venv/bin/python -m src.cli curate \
  --input data/curator_latest.json \
  --as-of-date <today>
```

Notes:
- The CLI calls `apply_curator_decisions`, which validates the payload against the contract (listing-date check via yfinance, `max_watchlist_size`, no double-adds, no stale removes, blocked removes for tickers with `shares > 0`).
- Rejected adds and removes appear in the `rejections` array; surface them in the report.
- `holdings.csv` is mutated in place: adds appended at `shares=0`, removes deleted entirely.
- One row per applied change is appended to `data/curation_history.csv`.

### Step 3 — run analyze (Bash)

```bash
.venv/bin/python -m src.cli analyze \
  --tickers <post-curate watchlist tickers, space-separated> \
  --period <profile lookback_period> \
  --objective <profile objective> \
  --risk-aversion <profile risk_aversion> \
  --max-weight <profile concentration_cap>
```

Returns a JSON blob with `optimization` (weights, Sharpe, expected return, vol) and `risk` (Sharpe, vol, max drawdown, VaR, CVaR).

### Step 4 — append recommendation (Bash)

```bash
.venv/bin/python -m src.cli recommend --force
```

Appends today's optimizer output to `data/recommendations.csv` so the dashboard's weight-history chart picks up the new row block. `--force` is required because re-running on the same day should overwrite, not duplicate.

### Step 5 — write the report (Task)

Spawn the `report-writer` subagent. Pass:

```json
{
  "user_request": "<original prompt>",
  "analysis": <step 3 JSON>,
  "curator": <step 1 JSON, the watchlist-curator's full return>,
  "curate_result": <step 2 JSON, with applied_adds/applied_removes/rejections>,
  "profile_conflicts": <merged from steps 2-4>,
  "thesis_baseline": <contents of data/thesis_baseline.json if it exists, else null>
}
```

The report is written to `data/reports/YYYY-MM-DD-review-portfolio.md`.

### Step 6 — refresh dashboard (Bash)

```bash
.venv/bin/python -m src.cli dashboard --nav-current live
```

Regenerates `docs/index.html`. Time-series charts are scoped to dates >= `thesis_baseline.date` if the file exists.

## Final output to the user

One short message:

- Path to the report.
- Path to the dashboard (`docs/index.html`).
- One-line summary: number of adds + removes applied, optimizer Sharpe, profile_conflicts count.
- "Read the report, especially the 'Profile conflicts' and 'Watchlist changes' sections."

## Rules

- **Never skip the empty-holdings guard.** Fresh repo → `/initialize-portfolio`, not this skill.
- Never modify the profile mid-run.
- Never silently clamp weights to satisfy the profile; surface conflicts instead.
- Numbers come from `src.cli`. The curator decides composition; the optimizer decides weights; both pass through Python before reaching the report.
- Step 1 must run before Steps 2 through 6 (the rest depend on it). Steps 3 and 4 can run in either order.
- The thesis baseline is read-only here. `/initialize-portfolio` writes it; `/review-portfolio` only renders it.
- Live mode never uses the as-of-date suppression list. That discipline is only for backtest replays via the agent's spec.
