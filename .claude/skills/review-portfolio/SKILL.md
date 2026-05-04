---
name: review-portfolio
description: Full portfolio review. Reads the investor profile and holdings, gathers wave-aligned news, runs the analyze CLI with wave-stage tilts, and writes a profile-aware report plus a refreshed dashboard. On a first run (when holdings.csv has all-zero shares), this skill also does the day 0 thesis-driven dollar allocation before optimizing, so the report can show beliefs and math side-by-side. The single demo flow.
---

# /review-portfolio

Holdings in, profile-aware report out. Run this monthly to get a fresh wave-stage news read plus a written narrative. The weekly cron is the lightweight Python-only sibling.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit. Never fall back to a default.
2. Read `holdings.csv` for the ticker universe.
3. Parse the user's request. Optional overrides: objective (default `max_sharpe`), period (default `3y`), max_weight (default = profile's `concentration_cap`).

## Step 0 — first-run check (conditional)

Inspect `holdings.csv`. If **every** row has `shares == 0`, this is a first run. Do the day 0 thesis allocation before optimizing. Otherwise skip Step 0 entirely.

### When Step 0 fires:

1. Confirm `initial_investment_usd` in the profile is present and positive. If missing, stop and tell the user to add it.
2. Produce a JSON object mapping each watchlist ticker to a dollar amount. Constraints:
   - The dollar amounts must sum to `initial_investment_usd` exactly.
   - Honor the profile's `exclusions`: any ticker in an excluded sector gets $0.
   - Honor `asset_class_targets` as a guideline. Sum the dollar amounts within each asset class and try to roughly match the target percentages. Use the asset-class mapping in `.claude/agents/report-writer.md`.
   - Within each asset class, weight tickers using the wave thesis. For equities specifically: lean into the current AI wave, then the named "next waves" listed in the profile (rockets/spacecraft, robotics, engineered biology, quantum computing, nuclear fusion). Tickers tied to past or unrelated waves get smaller weights.
   - Respect `concentration_cap`: no single ticker gets more than that fraction of `initial_investment_usd`.
   - Do not optimize for Sharpe, volatility, or any other math metric. This is the user's beliefs in dollar form.
3. State the reasoning per asset class and per ticker in plain prose before producing the JSON. Cite the wave thesis when assigning equity weights. Save this reasoning to pass to the report-writer.
4. Bash:
   ```
   python -m src.cli init-holdings --allocations '<json>' --out holdings.csv
   ```
   Capture the per-ticker price, shares, and value from the CLI's JSON return.
5. Bash:
   ```
   python -m src.cli snapshot --force
   ```
   Records day 0.
6. Save `day_0_baseline = { allocations_usd: <step 2 JSON>, reasoning: <step 3 prose>, holdings: <step 4 return> }` for the report-writer.

## Orchestration

### Step 1 — gather news (Task → news-researcher)

Pass the ticker list. Get back: `wave_views` (ticker → stage), bullets, `wave_stages` (per-wave call with rationale + evidence), and `exclusion_conflicts`.

After the agent returns, write the full payload (with an added top-level `date` field set to today's date in `YYYY-MM-DD` form) to `data/news_latest.json`. The dashboard reads this file to render the "Latest news" headlines section. The file is overwritten on each run (no history kept).

Then Bash:

```
python -m src.cli wave-history
```

This reads `data/news_latest.json` and appends today's per-wave stage classifications to `data/wave_history.csv` so the dashboard's wave-stage trajectory chart accumulates a time-series across runs. Idempotent on date; pass `--force` to overwrite if needed (e.g., if you re-run on the same day).

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
  "day_0_baseline": <step 0 payload, OR null if not a first run>
}
```

The report is written to `data/reports/YYYY-MM-DD-review-portfolio.md`. When `day_0_baseline` is non-null, the report-writer must include a "Day 0 baseline" section showing the thesis-driven allocation alongside the optimizer's recommendation, and a one-paragraph interpretation of the gap.

### Step 4 — refresh dashboard (Bash)

```
python -m src.cli dashboard
```

Regenerates `data/dashboard.html` with the latest snapshots and recommendations data.

## Final output to the user

One short message:

- Path to the report.
- Path to the dashboard (`data/dashboard.html`).
- One-line summary: objective + Sharpe + profile_conflicts count.
- If Step 0 fired, also: total dollars allocated on day 0 and "this was a first run; the report includes a Day 0 baseline section."
- "Read the report, especially the 'Profile conflicts' section."

## Rules

- Never skip Step 0's check (the holdings-all-zero detection). The first-run branch is the demo's setup arc.
- Never modify the profile mid-run.
- Never silently clamp weights to satisfy the profile; surface conflicts instead.
- Numbers come from `src.cli` (`init-holdings`, `analyze`, `snapshot`, `dashboard`). The thesis-driven dollar weights in Step 0 come from the LLM, but every share count, price, and risk metric passes through Python.
- Step 0 must run before Step 1, and Step 1 must run before Step 2. Do not parallelize.
- If Step 0 fires, Step 1's news-researcher sees the *populated* holdings.csv (post-init-holdings), so the news context applies to the day 0 positions.
