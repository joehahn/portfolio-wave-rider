---
name: review-portfolio
description: Recurring portfolio review. Reads the investor profile, holdings, and (if present) the persisted thesis baseline; gathers wave-aligned news via the news-researcher subagent; runs the analyze CLI with wave-stage tilts; and writes a profile-aware report plus a refreshed dashboard. The thesis baseline (if any) is rendered as a side-by-side thesis-vs-recommended comparison on every run, not just the first.
---

# /review-portfolio

Holdings in, profile-aware report out. Run this monthly to get a fresh wave-stage news read plus a written narrative. The weekly cron is the lightweight Python-only sibling.

This skill assumes a portfolio already exists. If you're starting fresh, run `/initialize-portfolio` first to set up the thesis allocation; come back here once `data/thesis_baseline.json` exists.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit. Never fall back to a default.
2. Read `holdings.csv` for the ticker universe. Every ticker in this file is passed to the news-researcher (Step 1) and to `analyze` (Step 2). The optimizer can only assign weight to tickers in this file. Rows with `shares=0` are part of the universe just like populated rows — they get news and they get a weight slot.
3. **Empty-holdings guard.** If **every** row in `holdings.csv` has `shares == 0`, stop and tell the user: this is a fresh repo; they should run `/initialize-portfolio` first to set up the thesis allocation. Do not proceed.
4. Read `data/thesis_baseline.json` if it exists. Its contents (`date`, `allocations_usd`, `reasoning`, `holdings`) are passed to the report-writer so every review report can render the thesis-vs-recommended comparison. If the file doesn't exist, that's fine — the comparison section is just omitted.
5. Parse the user's request. Optional overrides: objective (default from profile's `financial_model.objective`), period (default from `financial_model.lookback_period`), max_weight (default from `concentration_cap`).

## Orchestration

### Step 1 — gather news (Task → news-researcher)

Pass the ticker list and `lookback_days = 30`. State the lookback explicitly in the Task prompt so the agent uses it.

Get back: `wave_views` (ticker → stage), bullets, `wave_stages` (per-wave call with rationale + evidence), and `exclusion_conflicts`.

After the agent returns, write the full payload (with an added top-level `date` field set to today's date in `YYYY-MM-DD` form) to `data/news_latest.json`. The dashboard reads this file to render the "Latest news" headlines section. The file is overwritten on each run (no history kept).

Then Bash:

```
python -m src.cli wave-history
```

This reads `data/news_latest.json` and appends today's per-wave stage classifications to `data/wave_history.csv` so the dashboard's wave-stage trajectory chart accumulates a time-series across runs. Idempotent on date; pass `--force` to overwrite if needed (e.g., if you re-run on the same day).

Then Bash again, to archive the full news payload for forensic re-reading later:

```
mkdir -p data/news && cp data/news_latest.json "data/news/$(date -I)-news.json"
```

Each archived file is a snapshot of the full per-ticker bullets the news-researcher produced on that date. Files accumulate (no pruning); about 25 KB per run.

### Step 2 — run analysis (Bash)

```
python -m src.cli analyze \
  --tickers <list> \
  --period <period> \
  --max-weight <max_weight> \
  --objective <objective> \
  --wave-views '<json from step 1>'
```

The CLI returns a single JSON blob with `optimization` (weights, Sharpe, applied_wave_views, profile boundary flags) and `risk` (Sharpe, vol, max drawdown, VaR, CVaR).

### Step 3 — write report (Task → report-writer)

Pass:

```
{
  "user_request": <original prompt>,
  "analysis": <step 2 JSON>,
  "news": <step 1 payload>,
  "profile_conflicts": <merged from step 1 + step 2>,
  "thesis_baseline": <contents of data/thesis_baseline.json if it exists, OR null>
}
```

The report is written to `data/reports/YYYY-MM-DD-review-portfolio.md`. When `thesis_baseline` is non-null, the report-writer must include a "Thesis allocation" section showing the thesis-driven allocation alongside the optimizer's recommendation, and a one-paragraph interpretation of the gap.

### Step 4 — refresh dashboard (Bash)

```
python -m src.cli dashboard --nav-current live
```

Regenerates the live dashboard at `docs/index.html` (the CLI's default `--out`). Time-series charts are scoped to dates >= `thesis_baseline.date` if the file exists.

## Final output to the user

One short message:

- Path to the report.
- Path to the dashboard (`docs/index.html`).
- One-line summary: objective + Sharpe + profile_conflicts count.
- "Read the report, especially the 'Profile conflicts' section."

## Rules

- **Never skip the empty-holdings guard.** If `holdings.csv` is all-zero, the right next step is `/initialize-portfolio`, not this skill.
- Never modify the profile mid-run.
- Never silently clamp weights to satisfy the profile; surface conflicts instead.
- Numbers come from `src.cli` (`analyze`, `snapshot`, `recommend`, `dashboard`). Risk metrics, weights, and prices all pass through Python.
- Step 1 must run before Step 2. Do not parallelize.
- The thesis baseline is read-only here. /initialize-portfolio writes it; /review-portfolio only renders it.
