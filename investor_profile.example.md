---
name: "Example Investor"
horizon_years: 10
risk_tolerance: aggressive        # conservative | moderate | aggressive
target_return_annual: 0.10        # 10% annualized
max_drawdown_tolerance: 0.25      # portfolio should not reasonably exceed 25% drawdown
tax_status: taxable               # taxable | tax_advantaged | mixed
concentration_cap: 0.25           # no single position > 25% of portfolio
rebalance_cadence: monthly        # monthly | quarterly | semiannual | annual | threshold
min_trade_size_usd: 500           # don't propose trades smaller than this (~1% of a $50K portfolio)
exclusions:                       # sector / theme exclusions
  - tobacco
  - private_prisons
asset_class_targets:              # rough guide, not a hard constraint
  equities: 0.70
  precious metals: 0.1
  bonds: 0.1
  cash: 0.05
  cryptocurrencies: 0.05
---

# Goals

- Maximize long-term returns while keeping drawdowns tolerable.
- Tolerate meaningful short-term risk when it buys durable long-term exposure to a technology wave I believe in.
- Ride the current and future technology waves described below.

# Strategy & beliefs

## Core thesis: ride the wave, exit before the crest

Investment returns are shaped by long technology waves. Each wave
follows a rough pattern — quiet buildup, adoption surge, peak
enthusiasm, digestion — and the best risk-adjusted returns come from
entering during the buildup or early surge and trimming exposure
before the crest. Two operating rules follow:

1. **Ride the current AI wave, but trim before it crests.**
2. **Invest early in the next waves while they are still cheap.**

## Past and current waves

- Past: the internet, mobile/cell phone technology.
- Current: **artificial intelligence**.

## Likely next waves (where I want early exposure)

- **Robotics** — lowers labor costs across physical industries.
- **Rockets & spacecraft** — makes space resources (asteroids, satellite services) accessible and commercially usable.
- **Nuclear fusion** — makes energy abundant and lower-cost.
- **Quantum computing** — makes hard computations faster and cheaper.

## Portfolio construction beliefs

- Favor broad, diversified exposure over stock-picking. Use individual
  names only when they add a factor tilt or thematic exposure that
  broad funds miss.
- Willing to tilt toward quality and low-volatility factors; skeptical
  of momentum as a standalone strategy for a long-horizon investor.
- Comfortable holding cash when valuations look stretched (CAPE > 30, credit spreads < 2%); not a perma-bull.
- Avoid leverage and single-stock concentration even when optimizers suggest them.
- Tax efficiency matters: prefer ETFs over active funds in taxable
  accounts; prefer municipal bonds over corporates in taxable accounts
  when yields permit.

# Hard constraints

- No options, no margin, no short selling.
- Do not recommend any position that would violate `concentration_cap`, `exclusions`, or asset class no-go rules without flagging it explicitly.
- Do not recommend trades smaller than `min_trade_size_usd`.

# Soft preferences

- Prefer ETFs with expense ratio < 0.25% and AUM > $1B.
- Lean toward US-domiciled funds to simplify tax reporting.
- OK with some international equity exposure (15–25% of equity sleeve) but
  avoid single-country emerging-market funds.

# Notes for Claude

When any recommendation conflicts with this profile, propose it anyway if
it's materially better on the user's stated goal — but call out the conflict
explicitly in a "Profile conflicts" section of the report, including:

1. Which constraint or preference is violated.
2. The magnitude of the violation.
3. The alternative that would have satisfied the profile, and what it costs the user in expected return / Sharpe / drawdown.

The user decides, not the agent.
