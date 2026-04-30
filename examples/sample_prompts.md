# Example prompts

Run these inside Claude Code in this directory. All of them assume
`investor_profile.md` exists — run `/init-profile` first if it doesn't.

## `/optimize-portfolio`

### 1. Max-Sharpe on US tech
```
/optimize-portfolio AAPL MSFT NVDA GOOGL AMZN META, max-Sharpe, 3 years
```
Expect a recommendation with a concentration warning if any name clears
your `concentration_cap`.

### 2. Minimum-variance, broad market
```
/optimize-portfolio SPY QQQ IWM EFA EEM TLT GLD, min-variance, 5 years monthly
```

### 3. Target-return with a check
```
/optimize-portfolio AAPL JNJ JPM XOM PG, target-return 10%, 10 years
```
If your profile's `target_return_annual` is 7% and this ask is 10%, the
optimizer will likely flag a `max_drawdown_tolerance` conflict. Read the
"Profile conflicts" section — that's the point.

### 4. Sector-ETF comparison
```
/optimize-portfolio XLK XLF XLE XLV XLP, max-Sharpe, 5 years, cap each at 30%
```

## `/rebalance`

### 5. Rebalance an existing portfolio
```
/rebalance

Current holdings:
  VOO: $180,000
  VXUS: $60,000
  BND: $40,000
  AAPL: $20,000
Cash on hand: $5,000

Use the most recent report under data/reports/ as the target.
```
Expect trades rounded to $100, sub-`min_trade_size_usd` trades dropped,
and any exclusion or concentration issue flagged.

## `/init-profile`

### 6. First-time setup
```
/init-profile
```
Interviews you, writes `investor_profile.md` at the repo root. Run
before any of the other skills.
