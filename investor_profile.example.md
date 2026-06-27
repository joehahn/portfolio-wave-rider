---
initial_investment_usd: 50000     # total dollars to allocate on day 0
concentration_cap: 0.80           # no single position > 80% of portfolio
min_trade_size_usd: 1000          # don't propose trades smaller than this
exclusions:                       # sector / theme exclusions
  - solar energy (companies and ETFs)
  - wind energy (companies and ETFs)
financial_model:                  # optimizer math
  risk_aversion: 0.33             # λ in mean_variance utility (μᵀw − λ·wᵀΣw)
  risk_free_rate: 0.04            # ≈ 1y Treasury yield; baseline subtracted from E[r] in Sharpe-ratio calc
  lookback_period: 1.5y           # history window for estimating μ and Σ
  rebalance_period: monthly       # monthly | quarterly | semi_annual | annual; how often the watchlist-curator runs
  max_watchlist_size: 8           # hard cap on number of tickers the curator may consider
backtest:                         # only affects /run-backtest and sweeps, not live recommendations
  start_date: 2022-03-31          # window start (a quarter-end; first rebalance lands here). Post-COVID/post-stimulus 
  end_date: 2025-10-31            # window end (just before the late-2025 Iran-war runup)
  t_update_days: 1                # trading-day lag from rebalance signal to trade (1 = next session, 0 = same-close)
  # Optional backtest-only optimizer overrides (omit to use the live values from
  # financial_model + the top-level concentration_cap). Use these to test a candidate
  # config on the backtest before adopting it live. Example:
  #   risk_aversion: 0.5
  #   lookback_years: 1.0
  #   concentration_cap: 0.5
---

# Goals

- Maximize long-term returns while keeping drawdowns tolerable.
- Ride identifiable investment waves to early exposure; trim before they crest.

# Strategy & beliefs

## Core thesis: ride the wave, exit before the crest

Investment returns are shaped by long waves — durable, structurally-driven shifts in what's worth owning. Most are technology-driven (internet, mobile, AI), but some are geopolitical, demographic, regulatory, or commodity-cycle in nature. Each wave follows a rough pattern: quiet buildup, adoption surge, peak enthusiasm, digestion. The best risk-adjusted returns come from entering during the buildup or early surge and trimming exposure before the crest. Two operating rules follow:

1. **Ride the current dominant wave (AI), but trim before it crests.**
2. **Invest early in the next waves while they are still cheap.**

## Past and current technology waves

- Past: the internet, mobile/cell phones.
- Current: **artificial intelligence**.

## Likely next technology waves (where I want early exposure)

Listed roughly in order of when material commercial impact is likely to land:

- **Rockets & spacecraft**: will make space resources (satellite services, Moon, asteroids) commercially usable.
- **Robotics**: will lower labor costs across physical industries.
- **Quantum computing**: hard computations become faster and drive new discoveries and products and services.
- **Nuclear (fission and fusion)**: abundant lower-cost energy. Near-term: fission renaissance via small modular reactors (SMRs) and long-term power purchase agreements (PPAs) selling utility output to AI data centers that need always-on baseload power. Longer-horizon: fusion.

## Non-technology waves I'm watching

Not every durable repricing is tech-driven. Two current examples:

- **Geopolitical realignment driving energy prices** *(active, Spring 2026)*. The Iran war is keeping oil and gas spot prices elevated. Tailwind for energy producers (majors, midstream) and tactical winners in oil tankers / LNG carriers if shipping reroutes around the Strait of Hormuz persist. Headwind for fuel-cost-sensitive sectors (airlines, trucking).
- **Aging-population demographics** *(slow-burning, multi-decade)*. Japan, China, much of Europe are past peak working-age population. Beneficiaries: healthcare, eldercare REITs (real estate investment trusts that own senior-housing properties), and automation that backfills labor shortages.
