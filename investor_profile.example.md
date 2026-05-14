---
initial_investment_usd: 50000     # total dollars to allocate on day 0
concentration_cap: 0.25           # no single position > 25% of portfolio
min_trade_size_usd: 500           # don't propose trades smaller than this
exclusions:                       # sector / theme exclusions
  - tobacco
  - private_prisons
asset_class_targets:              # rough guide, not a hard constraint
  equities: 0.75
  precious metals: 0.10
  bonds: 0.10
  cash: 0.05
financial_model:                  # optimizer math; CLI flags override at runtime
  risk_aversion: 1.0              # λ in mean_variance utility (μᵀw − λ·wᵀΣw); the only knob on your return/variance tradeoff
  risk_free_rate: 0.04            # ≈ 1y Treasury yield; baseline subtracted from E[r] in Sharpe-ratio calc
  lookback_period: 1.3y           # history window for estimating μ and Σ
  rebalance_period: monthly       # monthly | quarterly | semi_annual | annual; how often the watchlist-curator runs
  max_watchlist_size: 12          # hard cap on how many tickers the curator may consider simultaneously
---

# Goals

- Maximize long-term returns while keeping drawdowns tolerable.
- Ride the current and future technology waves described below.

# Strategy & beliefs

## Core thesis: ride the wave, exit before the crest

Investment returns are shaped by long technology waves. Each wave follows a rough pattern: quiet buildup, adoption surge, peak enthusiasm, digestion. The best risk-adjusted returns come from entering during the buildup or early surge and trimming exposure before the crest. Two operating rules follow:

1. **Ride the current AI wave, but trim before it crests.**
2. **Invest early in the next waves while they are still cheap.**

## Past and current waves

- Past: the internet, mobile/cell phone technology.
- Current: **artificial intelligence**.

## Likely next waves (where I want early exposure)

Listed roughly in order of when material commercial impact is likely to land:

- **Rockets & spacecraft**: will make space resources (asteroids, satellite services) accessible and commercially usable.
- **Robotics**: will lower labor costs across physical industries.
- **Engineered biology**: will lower the cost of programming biology (gene editing, designer proteins, programmable cells), with payoffs across medicine, agriculture, and bio-manufacturing.
- **Quantum computing**: will make hard computations faster and cheaper.
- **Nuclear (fission and fusion)**: will make energy abundant and lower-cost. Near-term exposure comes from the fission renaissance (uranium, SMRs, utility PPAs with AI data centers); fusion is the longer-horizon thesis behind the same wave.
