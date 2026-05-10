# Wave-stage classification

Wave-stage classification is the process by which Portfolio Wave Rider takes recent news for each ticker, assigns one of five stages to each technology wave (AI, robotics, rockets/spacecraft, engineered biology, quantum, nuclear fusion, plus a catch-all general_markets), and translates that stage into a small multiplicative tilt on the optimizer's expected-return vector.

This document consolidates the full pipeline. For narrower angles see the cross-references at the end.

## Why classify waves at all

The optimizer estimates expected return μ from a 3-year price-history window. That estimate is backward-looking by construction. If you have a structural belief that one wave is in early adoption (cheap) and another is at peak enthusiasm (expensive), the price-history μ does not encode that belief. It only encodes "what happened recently."

The wave-stage tilt is a small, symmetric, evidence-grounded knob to nudge μ toward the user's wave thesis without overriding it. Multipliers range from 0.80 (peak) to 1.20 (buildup), so a "surge" call shifts μ by 10% before optimization. The optimizer still uses the price-history Σ for variance, and the concentration cap still applies. The tilt is one input, not a directive.

## The five stages

| stage | qualitative pattern in the news | multiplier on μ |
|---|---|---|
| `buildup` | quiet, cheap, under-owned. Real progress visible in industry data but not yet in stock prices. Sell-side is mixed or uninterested. | 1.20 |
| `surge` | adoption compounding, real revenue growth, hyperscaler or enterprise commitments. Multiples expanding but earnings catching up. | 1.10 |
| `peak` | enthusiasm IS the story. Valuations stretched, sell-side unanimously bullish, new entrants chasing, competition heating up. | 0.80 |
| `digestion` | post-crest hangover. Names down from highs, retail flows reversing, but no fundamental thesis breach. | 0.90 |
| `neutral` | not enough signal in the lookback window, or a general-markets ticker (bonds, cash, dividend ETF) with no wave attached. | 1.00 |

The qualitative descriptions are the working definitions used by the news-researcher subagent at `.claude/agents/news-researcher.md`. They are not tied to any quantitative threshold (for example, there is no rule that "surge means revenue growth above 30%"). The stage is pattern-recognition by the language model.

## How a stage gets assigned

The classifier is the `news-researcher` subagent (Sonnet). The full pipeline runs once per `/review-portfolio` invocation, and once per month-end during the strict-as-of-date pilot rebuild that populates `data/news_asof/`.

Steps:

1. **Per-ticker news harvest.** The agent runs 2 to 3 WebSearch queries per ticker, scoped first to the curated `news_sources.md` (per-wave domain list), then falling back to open search if the curated sources return nothing material. It picks 2 to 4 bullets per ticker covering earnings, guidance, regulatory action, M&A, product or research milestones.

2. **Aggregation up to wave.** Each ticker is mapped to a wave bucket via `TICKER_WAVE` in `src/portfolio.py` (hand-curated). The agent groups bullets by wave and judges the wave's stage based on the qualitative pattern across all bullets in that bucket. Stages are assigned per wave, not per ticker.

3. **Grounding rule.** The agent must cite at least one specific bullet that supports the stage call. If no supporting bullet exists in the lookback window, the stage must be `neutral`. This is the only mechanical guardrail. (Example: the 2025-05-31 and 2025-07-31 as-of-date pilots returned `robotics: neutral` because no May or July headline supported any directional call for BOTZ.)

4. **Per-ticker views derived.** Tickers inherit the stage of their wave bucket. NVDA, GOOGL, MSFT all get whatever stage AI got. The output is a `wave_views` dict `{ticker: stage}`.

The "algorithm" is the prompt at `.claude/agents/news-researcher.md`. There is no separate spec. Reading the prompt is reading the algorithm.

## Stage to math

The `wave_views` dict produced above is passed to `optimize_portfolio` via the `wave_views` parameter. Inside, `apply_wave_tilt` (in `src/portfolio.py:48`) multiplies each ticker's expected return by the multiplier for its assigned stage:

```
μ_tilted[ticker] = WAVE_STAGE_TILT[stage] × μ[ticker]
```

The default multipliers live in `WAVE_STAGE_TILT` at `src/portfolio.py:39`. They can be overridden per investor in `investor_profile.md`'s `financial_model.wave_stage_tilts` YAML block. The CLI loads the override via `portfolio.load_financial_model()`; CLI flags can override per invocation.

The optimizer then runs mean-variance on the tilted μ and the untouched Σ. Tilts only change the expected-return estimate, not the covariance and not the concentration cap.

## Where the history lives

Stage classifications accumulate in `data/wave_history.csv` (gitignored, regenerable). Schema:

```
date, wave, stage, evidence_tickers, rationale, seeded
```

One row per (date, wave). The `seeded` column is a provenance flag: `False` for organic real-time classifications and for as-of-date pilot runs, `True` for post-hoc backfill (see below).

Two paths populate this file:

**Organic accumulation.** Every `/review-portfolio` run calls `python -m src.cli wave-history`, which appends today's per-wave classifications. This is the slow path: one row per wave per run, monthly cadence.

**Pilot rebuild, the path the public demo uses.** A fresh repo can backfill 12 months of trajectories by running 12 news-researcher subagents in parallel, each with strict as-of-date instructions: "today is 2025-10-31; suppress all post-October knowledge; date-scope WebSearch with `before:2025-11-01`." The 12 archived payloads live at `data/news_asof/`, and `scripts/rebuild_wave_history.py` aggregates them into `wave_history.csv`. Run cost is roughly 5 USD in Sonnet usage.

**Seed backfill, deprecated for the public demo.** `python -m src.cli seed-wave-history` writes 12 monthly post-hoc judgments tagged `seeded=True`. The dates on those rows are honest (each row carries its end-of-month date), but the rationales were authored in May 2026 with full knowledge of subsequent events. From the perspective of the dated row, the content has foresight. This is what quant finance calls **look-ahead bias**, and it inflates backtest returns. The public demo switched from this path to the pilot rebuild on commit `416f4a2`. Headline backtest return dropped from +159% to +110%, max drawdown halved (from -40% to -20%), and Sharpe rose from 1.6 to 2.2. The seed's "extra" return was foresight-driven.

## How the backtest queries it

The headline backtest, the lambda sweep, and the concentration sweep all share the same lookup helper. Defined inline as `_wave_views_at(date)` in `src/portfolio.py:backtest()` and mirrored as `wave_views_at(wh_df, date)` in the two sweep scripts under `scripts/`:

```python
relevant = wh_df[wh_df["date"] <= date]
latest_date = relevant["date"].max()
latest = relevant[relevant["date"] == latest_date]
wave_to_stage = dict(zip(latest["wave"], latest["stage"]))
return {t: wave_to_stage.get(TICKER_WAVE.get(t, "general_markets"), "neutral")
        for t in tickers}
```

At each monthly rebalance (first trading day of each new month), the helper filters to rows dated on or before that day and picks the most recent classification. A November 2025 rebalance only sees rows from October 2025 or earlier. This is the as-of-date discipline that makes the backtest free of price-side look-ahead bias.

The wave-history-side protection depends on the rows themselves having honest content, which is what the pilot rebuild establishes. The seed path produced rows whose dates were honest but whose rationales had foresight, so the as-of-date filter alone was not sufficient under the seed.

## Residual caveat: training-data leakage

The news-researcher runs on Sonnet, whose training cutoff is early 2026. Even when the prompt says "today is 2025-10-31, ignore anything published after that date," the model retains general knowledge of subsequent events. WebSearch results can be date-scoped via `before:YYYY-MM-DD` queries, but the model's reasoning over those headlines can be subtly informed by post-date knowledge. The pilot rebuild reduces look-ahead bias substantially relative to the seed path, but does not eliminate it. The truly clean version would require a model with a training cutoff before May 2025.

## Configurability

Everything except the language-model judgment is parameterized.

- `WAVE_STAGE_TILT` defaults: `src/portfolio.py:39`. Override per investor in `investor_profile.md`'s `financial_model.wave_stage_tilts` block.
- `TICKER_WAVE`: ticker-to-wave mapping in `src/portfolio.py`. Hand-curated; add new tickers here when extending the watchlist.
- Wave list: hardcoded in `_WAVE_DISPLAY_ORDER` in `src/portfolio.py`. Adding a wave means adding to this list, to `news_sources.md`, to the news-researcher prompt's wave vocabulary in `.claude/agents/news-researcher.md`, and to `TICKER_WAVE` for any tickers in the new wave.
- Lookback window for the optimizer's μ: `lookback_period` in the profile YAML, default 3y.
- The strict-as-of-date prompt template that drove the pilot rebuild lives in the commit message of `416f4a2`. Each per-date prompt names the as-of date, gives a training-data suppression list, and includes the WebSearch `before:` filter.

## Code and document cross-references

| Topic | Location |
|---|---|
| Stage definitions and grounding rule | `.claude/agents/news-researcher.md` Step 5 |
| Multiplier table with intuitions and ASCII wave diagram | `GLOSSARY.md` Wave-stage tilt section |
| Math formula in narrative form | `README.md` "Wave-stage tilts" subsection |
| Stage tilt code | `src/portfolio.py:apply_wave_tilt`, `WAVE_STAGE_TILT` |
| As-of-date lookup code | `src/portfolio.py:backtest()`, `_wave_views_at` helper |
| Wave-history schema | `CLAUDE.md` "Time-series outputs" section |
| Two paths to populate (seed vs agent-based) | `README.md` "Wave-stage trajectories" bullet |
| Pilot rebuild script | `scripts/rebuild_wave_history.py` |
| Archived as-of-date payloads | `data/news_asof/` |
| YAML schema for tilt overrides | `investor_profile.example.md` lines 17 to 23 |
| Lambda and concentration sweeps using the corrected wave history | `docs/lambda_comparison.html`, `docs/max_weight_comparison.html`, `docs/backtest.html` |
