# /review-portfolio

Live curator-driven portfolio review. Each run sends the watchlist-curator agent out to read recent news, propose adds and removes against the current watchlist, applies the resulting changes to `holdings.csv` and `data/curation_history.csv`, runs the optimizer on the new watchlist, writes a profile-aware report, and refreshes the live dashboard.

The math here is identical to the curator backtest (`backtest --curator-runs-dir`); the only difference is that this skill fires ONE curator call against today's date (no as-of-date discipline, no suppression list) instead of replaying pre-collected payloads.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit. Do not fall back to a default.
2. Read `holdings.csv` for the current watchlist. Every ticker in this file is passed to the curator as `current_watchlist` (including rows with `shares=0`).
3. **Empty-holdings guard**: if every row in `holdings.csv` has `shares == 0`, stop and tell the user this is a fresh repo; they should run `/initialize-portfolio` first to set the thesis allocation. Do not proceed.
4. Read `data/thesis_baseline.json` if it exists. Its contents (`date`, `allocations_usd`, `reasoning`, `holdings`) are passed to the report-writer so every review report can render the thesis-vs-recommended comparison.
5. Load the profile's `financial_model` settings via `python -m src.cli` (the CLI does this automatically via `portfolio.load_financial_model`). Defaults: `rebalance_period: monthly`, `max_watchlist_size: 8`.

## Orchestration

### Step 1 — fire the watchlist-curator (Task)

Spawn the `watchlist-curator` subagent. Pass a self-contained prompt with these per-call inputs:

```json
{
  "as_of_date": "<today, YYYY-MM-DD>",
  "current_watchlist": [<ticker list from holdings.csv>],
  "max_watchlist_size": <from profile, default 8>,
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

Then record the curator's **real** WebSearch queries (ground truth, not the agent's self-report). The `Task` result for the curator call includes an `output_file:` path (the agent transcript); run:

```bash
.venv/bin/python scripts/extract_search_terms.py <curator output_file> \
  --into data/curator_latest.json
```

This parses the actual `WebSearch` tool calls out of the transcript and writes them into `search_terms`, so the dashboard's "Curator search terms" panel shows what the curator truly searched. If the transcript can't be parsed it leaves the agent's self-reported `search_terms` in place (graceful fallback). Copy the updated `curator_latest.json` over `data/curator_runs/live/<today>-curation.json` so the archive matches.

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

### Step 5 — write the report (Task, with watchdog + inline fallback)

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

**Watchdog (required).** The `report-writer` subagent has stalled in practice: it reads all its inputs successfully and then the background generation step hangs, so it never emits the `Write` and never sends a completion notification, which would otherwise block the run forever. The cause is not the inputs or tools (every tool call returns) but a flaky background-inference generation; the main loop's own generation has not shown this. So do NOT wait indefinitely on the subagent. Right after spawning it, launch a background watchdog that polls for the report file with a timeout, e.g.:

```bash
f="data/reports/$(date +%F)-review-portfolio.md"
for i in $(seq 1 16); do [ -f "$f" ] && { echo "REPORT_WRITTEN after $((i*15))s"; exit 0; }; sleep 15; done
echo "TIMEOUT: report-writer stalled — falling back to inline"
```

Run it with `run_in_background: true` so whichever finishes first (the subagent's completion notification or the watchdog) wakes you. If the report file exists, continue. If the watchdog times out with no file:

1. Stop the stalled subagent with `TaskStop` (so a late zombie can't overwrite the file).
2. Tell the user the report-writer stalled and you are writing the report inline.
3. **Write the report yourself** with the Write tool, directly to `data/reports/<today>-review-portfolio.md`, from the same inputs (the analyze JSON, `data/curator_latest.json`, the curate result, `holdings.csv`, `data/thesis_baseline.json`, `investor_profile.md`). To match the subagent's output, **read `.claude/agents/report-writer.md` and follow its "Report structure" and "Table formatting" sections exactly** — do not improvise an abbreviated set. That means all of its sections, in order: `The ask`, `Recommended allocation` (with the Asset-name column and per-ticker trades/tilts), `Thesis allocation` (thesis vs recommended %, omit only if `thesis_baseline` is null), `How this maps to the profile`, `Profile conflicts` (always present, even if empty; cite the `investor_profile.md` line for any conflict; flag single-name concentration at the cap; never silently clamp), `Risk picture` (Sharpe, vol, max drawdown, VaR, CVaR from the analyze JSON), `Watchlist changes this period` (on a no-change run write "Quiet period — curator proposed no changes."), `News evidence` (omit entirely when there are no adds), and `Caveats` (the report ends here). Follow the repo prose rules (no em dashes in narrative; numbers only from the `src.cli` outputs). A no-change run still gets the full structure; it is not an excuse for a short report.

   **Em-dash self-check (required after the inline write).** The inline path is the main loop writing prose directly, which has repeatedly slipped em dashes into the narrative against the no-em-dash rule. After writing the file, grep it and fix any narrative em dashes before finishing:

   ```bash
   grep -n "—" data/reports/$(date +%F)-review-portfolio.md
   ```

   Replace every em dash that sits in a sentence (clause separators, asides) with a comma, colon, semicolon, parentheses, or a new sentence. Two are allowed to remain and should NOT be changed: the report's `# <Skill> — <date>` title (the report-writer spec's header format) and `—` used as an empty-cell placeholder inside tables. Anything else is a violation; re-grep after fixing to confirm only the title and table placeholders remain. This check applies only to the inline fallback; the `report-writer` subagent handles its own prose.

The inline fallback is not a cure for the flaky generation; it removes the silent-stall failure mode (no completion notification → infinite wait) and makes the step retryable in the main loop, which has been reliable.

### Step 6 — refresh snapshot, then dashboard (Bash)

```bash
.venv/bin/python -m src.cli snapshot --force
.venv/bin/python -m src.cli dashboard
```

`snapshot --force` must run first. `curate` (Step 2) mutates `holdings.csv` (adds at `shares=0`, removes deleted), but the price snapshot is otherwise only refreshed by the daily cron. Without this step, any ticker added or removed this run is stale in `data/snapshots.csv` until the next cron fire, and the dashboard's trade table (chart 5, "Trades to move from actual to recommended") joins the latest recommendation to the latest snapshot by price: a freshly-added ticker has a target weight but no price row, so the NaN-price guard silently drops it and the BUY never appears. Re-snapshotting `--force` gives every current-watchlist ticker (including new `shares=0` adds) a price row for today, so the trade table is complete. `--force` overwrites today's rows only.

`dashboard` regenerates `docs/index.html`. Time-series charts are scoped to dates >= `thesis_baseline.date` if the file exists.

The curator's per-add news evidence is rendered inline in the report's "News evidence" section (step 5), so there is no separate news HTML page to refresh.

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
