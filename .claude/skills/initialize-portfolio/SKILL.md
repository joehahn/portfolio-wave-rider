---
name: initialize-portfolio
description: One-time day 0 setup. Reads investor_profile.md to get initial_investment_usd, then proposes a thesis-driven dollar allocation across the holdings.csv watchlist using the wave thesis, asset_class_targets, and exclusions. Writes the resulting shares to holdings.csv and records day 0 via snapshot. The result is intentionally NOT mean-variance optimized; that is what /review-portfolio is for.
---

# /initialize-portfolio

Translates the user's beliefs into an initial dollar allocation. Day 0 of the demo: thesis-driven, not optimized.

## Before you start

1. Read `investor_profile.md`. If missing, stop and tell the user to copy `investor_profile.example.md`.
2. Confirm `initial_investment_usd` is present and positive. If missing, stop and tell the user to add it.
3. Read `holdings.csv` for the watchlist. If any ticker has shares > 0, warn the user that this skill will overwrite the file and ask for explicit confirmation before continuing.

## Orchestration

### Step 1: propose a thesis-driven dollar allocation

You produce a JSON object mapping each watchlist ticker to a dollar amount. Constraints:

- The dollar amounts must sum to `initial_investment_usd` exactly.
- Honor the profile's `exclusions`: any ticker in an excluded sector gets $0.
- Honor `asset_class_targets` as a guideline. Sum the dollar amounts within each asset class and try to roughly match the target percentages. Use the asset-class mapping in `.claude/agents/report-writer.md`.
- Within each asset class, weight tickers using the wave thesis. For equities specifically: lean into the current AI wave, then the named "next waves" (robotics, rockets/spacecraft, nuclear fusion, quantum computing). Tickers tied to past or unrelated waves get smaller weights.
- Respect `concentration_cap`: no single ticker gets more than that fraction of `initial_investment_usd`.
- Do not optimize for Sharpe, volatility, or any other math metric. This is the user's beliefs in dollar form.

Before producing the JSON, state your reasoning per asset class and per ticker in plain prose. Cite the wave thesis when assigning equity weights.

### Step 2: convert dollars to shares (Bash)

```
python -m src.cli init-holdings --allocations '<json from step 1>' --out holdings.csv
```

This fetches current prices, computes `shares = dollars / price` per ticker, and overwrites `holdings.csv`. Capture the JSON the CLI returns: it has the per-ticker price, shares, and value.

### Step 3: record day 0 (Bash)

```
python -m src.cli snapshot --force
```

`--force` ensures today's snapshot reflects the new holdings even if a row already exists for today.

### Step 4: refresh the dashboard (Bash)

```
python -m src.cli dashboard
```

### Step 5: write a report

Write `data/reports/YYYY-MM-DD-initialize-portfolio.md` with these sections:

- **The ask**: `initial_investment_usd`, the profile's stated wave thesis in one paragraph.
- **Allocation table**: ticker | asset name | asset class | wave bucket | $ allocated | % of portfolio | shares | price.
- **Asset-class breakdown**: a table comparing the resulting class percentages to `asset_class_targets`.
- **Wave-bucket breakdown**: percentage allocated to each wave (AI, robotics, rockets_spacecraft, nuclear_fusion, quantum, general_markets).
- **Reasoning**: the thesis-driven prose from step 1, organized by asset class.
- **Profile conflicts**: if the allocation can't honor every constraint (e.g., the watchlist has no precious-metals ETF but the target is 10%), surface it explicitly.
- **Closing**: "This is the day 0 distribution. Run `/review-portfolio` when you want the day 1 (mean-variance optimized with wave tilts) distribution. The gap between day 0 and day 1 is the marginal contribution of the optimizer relative to your stated beliefs."

## Final output to the user

One short message:

- Path to the report.
- Path to the dashboard (`data/dashboard.html`).
- Total dollars allocated.
- "Run /review-portfolio to see the day 1 (optimized) distribution."

## Rules

- The sum of allocations must equal `initial_investment_usd` exactly.
- Excluded sectors get $0 regardless of any other consideration.
- Numbers come from Python: the thesis-driven dollar weights come from you, but every share count, price, and value passes through `src.cli`.
- Never call `analyze`, `optimize_portfolio`, or any mean-variance code. This skill is intentionally pre-math.
- Never modify `investor_profile.md`.
