---
name: review-portfolio
description: Full LLM-driven portfolio review. Reads the profile and holdings, gathers wave-aligned news, runs the analyze CLI with wave tilts, and writes a profile-aware report plus a refreshed dashboard. The flagship demo flow.
---

# /review-portfolio

Holdings in, profile-aware report out. Run this monthly when you want a
fresh wave-stage read and a written narrative. The weekly cron is the
lightweight Python-only sibling.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the
   user to copy `investor_profile.example.md` to `investor_profile.md`
   and edit. Never fall back to a default.
2. Read `holdings.csv` for the ticker universe.
3. Parse the user's request. Optional overrides: objective (default
   `max_sharpe`), period (default `3y`), max_weight (default = profile's
   `concentration_cap`).

## Orchestration

### Step 1 — gather news (Task → news-researcher)

Pass the ticker list. Get back: `wave_views` (ticker → stage), bullets,
`wave_stages` (per-wave call with rationale + evidence), and
`exclusion_conflicts`.

### Step 2 — run analysis (Bash)

```
python -m src.cli analyze \
  --tickers <list> \
  --period <period> \
  --max-weight <max_weight> \
  --objective <objective> \
  --wave-views '<json from step 1>'
```

The CLI returns a single JSON blob with `optimization` (weights, Sharpe,
applied_wave_views, profile boundary flags) and `risk` (Sharpe, vol, max
drawdown, VaR, CVaR).

### Step 3 — write report (Task → report-writer)

Pass:

```
{
  "user_request": <original prompt>,
  "analysis": <step 2 JSON>,
  "news": <step 1 payload>,
  "profile_conflicts": <merged from step 1 + step 2>
}
```

The report is written to `data/reports/YYYY-MM-DD-review-portfolio.md`.

### Step 4 — refresh dashboard (Bash)

```
python -m src.cli dashboard
```

Regenerates `data/dashboard.html` with the latest snapshots and
recommendations data.

## Final output to the user

One short message:

- Path to the report
- Path to the dashboard (`data/dashboard.html`)
- One-line summary: objective + Sharpe + profile_conflicts count
- "Read the report, especially the 'Profile conflicts' section."

## Rules

- Never skip step 1 (profile load) — the demo's whole point.
- Never modify the profile mid-run.
- Never silently clamp weights to satisfy the profile — surface conflicts.
- Numbers come from `src.cli analyze`, not from the LLM.
