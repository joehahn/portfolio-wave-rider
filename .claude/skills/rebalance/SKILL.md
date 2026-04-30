---
name: rebalance
description: Given the user's current holdings (dollar amounts per ticker) and an optional target allocation, produce a trade list that respects profile constraints — concentration cap, exclusions, minimum trade size — and explains the rationale. Does not execute trades.
---

# /rebalance

## Before you start

1. Read `investor_profile.md`. If missing, stop and ask the user to run
   `/init-profile`.
2. Collect inputs from the user if not already provided:
   - `current_holdings`: a mapping ticker -> dollar value.
   - `target_weights`: optional, either a mapping ticker -> weight
     summing to 1.0, or a reference to the most recent report under
     `data/reports/` (offer to load it).
   - `cash_on_hand_usd`: optional, default 0.

   Use `AskUserQuestion` if anything essential is missing.

## Orchestration

### Step 1 — anchor to a target

If the user provided `target_weights`, use those.

Otherwise, derive target weights by invoking `optimize-portfolio` with
the ticker universe implied by `current_holdings`. Use the resulting
weights as the target. Save the resulting report path so the final
output can reference it.

### Step 2 — compute trades

This is deterministic arithmetic — do it in your own reasoning, not via
a Python wrapper.

1. Compute `total_value = sum(current_holdings.values()) + cash_on_hand_usd`.
2. For each ticker in the universe (union of `current_holdings` keys and
   `target_weights` keys):
   - `target_value = total_value * target_weights.get(ticker, 0)`
   - `current_value = current_holdings.get(ticker, 0)`
   - `trade_usd = target_value - current_value`
3. Drop any trade with `abs(trade_usd) < profile.min_trade_size_usd`.
4. Round remaining trades to the nearest $100.

### Step 3 — check against the profile

Before presenting the trade list:

- Any resulting position > `concentration_cap`? Flag as `profile_conflict`.
- Any ticker in `exclusions`? Flag. Do NOT drop it silently; surface it.
- Any trade below `min_trade_size_usd`? Already dropped, but explain.

### Step 4 — write the trade list

Write to `data/reports/YYYY-MM-DD-rebalance.md` with this outline:

```
# Rebalance plan — <date>

## Current vs target
<table: ticker, current_usd, target_usd, target_pct>

## Trades
<table: ticker, action (buy/sell), usd_amount, rounded. Drop the zero rows.>

## Tax and cost notes
<if tax_status=taxable: flag which sells are sells, remind the user to
consider tax-lot selection and holding period. Do not compute tax.>

## Profile conflicts
<same format as the /optimize-portfolio skill. Must be present even if
empty.>

## Assumptions
- Trades below $<min_trade_size_usd> were dropped.
- Amounts rounded to the nearest $100.
- No order type or timing implied.
```

## Final output

Print:

- Path to the written plan.
- A one-line summary: total buy $ / total sell $ / number of trades.
- The `profile_conflicts` count.

## What you must NOT do

- Do not execute trades or integrate with any broker.
- Do not propose tax-lot selection — flag it as out of scope and
  recommend the user's tax software.
- Do not include trades below `min_trade_size_usd`.
- Do not modify `investor_profile.md`.
