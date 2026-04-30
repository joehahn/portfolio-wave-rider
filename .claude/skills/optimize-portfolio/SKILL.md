---
name: optimize-portfolio
description: Given a ticker universe and optional objective, build an optimized portfolio that honors the investor profile, run risk + backtest + news, and write a profile-aware report to data/reports/. The flagship demo flow.
---

# /optimize-portfolio

Tickers in, profile-aware recommendation out.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the
   user to run `/init-profile`. Never fall back to a default.
2. Parse the user's request. Required: a ticker list. Optional:
   objective (default `max_sharpe`), lookback (default `3y`), constraints.

## Orchestration

Invoke each specialist with the `Task` tool.

### Step 1 — data + news (parallel — one message, two Task calls)

Both only need the ticker list, so fan them out at the same time.

- `market-data-fetcher`: tickers + period → `returns_handle`.
- `news-researcher`: tickers → bullets, `wave_stages`, and a
  `wave_views` mapping (ticker -> stage).

### Step 2 — optimize (sequential, needs step 1 outputs)

Invoke `optimizer` with `returns_handle`, the objective, `max_weight`
set to the profile's `concentration_cap`, and the `wave_views` mapping
from the news-researcher passed through as `--wave-views`. The tilt is
applied to expected returns before the solver runs. Save the weights
and any `profile_conflicts`.

### Step 3 — risk + backtest (sequential, needs step 2 weights)

Invoke `risk-analyst` with the weights and `returns_handle` for
in-sample risk metrics plus the in/out-of-sample backtest.

### Step 4 — report (sequential)

Invoke `report-writer` with:

```
{
  "user_request": <original prompt>,
  "optimizer": <step 2; includes applied_wave_views>,
  "risk": <step 3 payload; risk + backtest>,
  "news": <step 1 news-researcher payload; includes wave_stages>,
  "profile_conflicts": <merged list from step 2 + step 3>
}
```

The report-writer must include a "Wave stages" section that shows the
news-researcher's stage call for each wave and the resulting per-ticker
tilts, so the user can see exactly how news moved the weights.

Report is written to `data/reports/YYYY-MM-DD-optimize-portfolio.md`.

## Final output to the user

One short message:

- Path to the report.
- One-line summary: objective + Sharpe + profile_conflicts count.
- "Read the report, especially the 'Profile conflicts' section."

## Rules

- Never skip step 1 — the profile load is the demo's whole point.
- Never modify the profile mid-run.
- Never silently clamp weights to satisfy the profile — surface conflicts.
