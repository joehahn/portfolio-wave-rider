---
# ============================================================================
# STARTER investor profile. Copy to the repo root and edit:
#     cp examples/investor_profile.md investor_profile.md
#
# This file is the single source of truth for every recommendation. Nothing
# below is hardcoded anywhere in the code, so editing here is how you change
# the system's behavior. The numbers are DEFAULTS chosen to be reasonable, not
# optimal: use the Sweeps dashboard to tune them against your own thesis.
# ============================================================================

initial_investment_usd: 50000            # total dollars to allocate on day 0
starter_watchlist: [AAPL, GOOGL, AMZN]   # the day-0 watchlist, equal-weighted
                                         # (matches the placeholder rows in examples/holdings.csv)
always_include: [SPY, AGG, IAU]          # permanent optimizer anchors (equity / bond / gold safe havens).
                                         # Always in the optimizer universe, OUTSIDE max_watchlist_size,
                                         # never added or removed by the curator. Use [] to disable.
dashboard_growth_guides_pct_per_week: [0.5, 1.0, 1.5]   # dotted reference curves on dashboard plot 1

exclusions:                       # sectors or themes the curator must never propose
  - solar energy (companies and ETFs)
  - wind energy (companies and ETFs)

financial_model:                  # the optimizer's math. All of it is swept by the Sweeps dashboard.
  concentration_cap: 0.35         # per-position cap: no single holding above 35% of the portfolio.
                                  # Lower forces breadth; higher lets winners run and raises drawdown.
  min_trade_size_frac: 0.05       # skip trades smaller than this FRACTION of portfolio value (0.05 = 5%),
                                  # so the optimizer does not churn on noise
  risk_aversion: 2.0              # λ in the mean-variance utility (μᵀw − λ·wᵀΣw). Higher = more
                                  # diversified and risk-averse; lower = more concentrated in whatever is running.
  risk_free_rate: 0.04            # ≈ 1y Treasury yield; the baseline subtracted from E[r] in the Sharpe ratio
  rebalance_period: monthly       # weekly | biweekly | monthly | quarterly. How often the curator runs.
  optimizer_lookback_days: 180    # trailing calendar days of prices used to estimate μ and Σ.
                                  # Short = faster rotation but noisier; long = steadier but slower to react.
  news_lookback_days: 14          # trailing calendar days of news the curator reads each rebalance
  max_watchlist_size: 10          # hard cap on tickers the curator may carry. Tighter caps concentrate.

backtest:                              # the historical replay only; does not affect live recommendations
  start_date: 2023-07-22               # window start
  end_date: 2026-07-22                 # window end
  max_articles: 100                    # max ranked articles per rebalance pool
  t_update_days: 1                     # trading-day lag from signal to trade (1 = next session, 0 = same close)
  curator_model: moonshotai/kimi-k2.5  # LLM that curates in the backtest (routed via OpenRouter)

forward:                                      # the live, out-of-sample path
  curator_model: claude-sonnet-5              # LLM that curates the watchlist live (routed via Anthropic)
  retrieval_model: claude-haiku-4-5-20251001  # cheap model that drives the live web_search news pull
  retriever: websearch                        # live news source
  inception_date: 2026-01-01                  # date forward news ingestion began

---

# Goals

- Maximize long-term returns while keeping drawdowns tolerable.
- Ride identifiable investment waves to early exposure; trim before they crest.

# Strategy & beliefs

## Core thesis: ride the wave, exit before the crest

Investment returns are shaped by long waves, meaning durable structurally-driven shifts in what is worth owning. Most are technology-driven (the internet, mobile, AI), but some are geopolitical, demographic, regulatory, or commodity-cycle in nature. Each wave follows a rough pattern: quiet buildup, adoption surge, peak enthusiasm, digestion. The best risk-adjusted returns come from entering during the buildup or early surge and trimming before the crest.

This section is prose, not configuration, and the curator reads it verbatim at every rebalance. Write it in your own words. The more concretely you describe what you believe and why, the more useful the curator's proposals will be. Replace everything below with your own thesis.

## Current technology waves

- **Artificial intelligence.** The dominant current wave. Hold exposure, but watch for signs of the crest.

## Likely next technology waves (where I want early exposure)

Roughly in order of when material commercial impact is likely to land:

- **Rockets & spacecraft**: making space resources (satellite services, the Moon, asteroids) commercially usable.
- **Robotics**: lowering labor costs across physical industries.
- **Quantum computing**: making hard computations tractable and driving new products and services.
- **Nuclear (fission and fusion)**: abundant lower-cost energy. Near-term via small modular reactors; longer-horizon via fusion.

## Non-technology waves I'm watching

Not every durable repricing is technology-driven. For example:

- **Aging-population demographics** *(slow-burning, multi-decade)*. Much of Japan, China, and Europe is past peak working-age population. Beneficiaries: healthcare, eldercare REITs (real estate investment trusts owning senior-housing properties), and automation that backfills labor shortages.
