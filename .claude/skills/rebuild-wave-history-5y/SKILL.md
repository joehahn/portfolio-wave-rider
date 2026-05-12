---
name: rebuild-wave-history-5y
description: One-shot strict as-of-date wave-history backfill for the 5y backtest experiment. Fires 20 parallel news-researcher Task calls (one per quarter-end from 2021-Q3 to 2026-Q2), each with the stand-in-the-shoes-of-the-date discipline. Archives 20 JSON payloads under data/news_asof_5y/ then aggregates into data/wave_history_5y.csv. Does NOT touch data/wave_history.csv (the live file). Cost: roughly $15-25 in Sonnet usage; wall time ~30 minutes if parallelized.
---

# /rebuild-wave-history-5y

The 5y backtest needs a wave-history series anchored at quarter-end
dates 2021-Q3 through 2026-Q2. This skill orchestrates the 20
strict-as-of-date news-researcher calls and aggregates them into a
parallel `data/wave_history_5y.csv` so the live 1y wave history is
not disturbed.

## Before you start

1. **Verify the safety backup.** Confirm that `data/wave_history.csv.bak`
   exists and matches the current size of `data/wave_history.csv`. If
   not, copy it: `cp data/wave_history.csv data/wave_history.csv.bak`.
   The skill must never write to the live file.
2. Confirm that `scripts/post_date_events.py` and
   `scripts/asof_news_prompt.md` are present on this branch.
3. Confirm the universe: the 5y backtest uses 10 tickers, dropping
   ARKG (bio out of scope for this experiment) and NUKZ for as-of
   dates before 2024-01-24 (the ETF launched then). The skill should
   exclude NUKZ from the tickers list when filling the prompt for any
   pre-2024-01-24 as-of date.
4. Create the output dir: `mkdir -p data/news_asof_5y`.

## Step 1 — load inputs

```python
from scripts.post_date_events import ASOF_DATES_5Y, events_after
prompt_template = open("scripts/asof_news_prompt.md").read()
# Extract just the body inside the triple-backtick block.
```

The universe per as-of date:
- Always include: AGG, BIL, IAU, GOOGL, RKLB, NVDA, MSFT, BOTZ, QTUM, VIG
- Include NUKZ only if `as_of_date >= "2024-01-24"`.
- Never include ARKG.

(RKLB started 2021-08-25, so the earliest as-of date 2021-09-30
already has ~5 weeks of RKLB history. That's thin but acceptable for
news coverage; the news-researcher will say `rockets_spacecraft:
neutral` if no material headlines.)

## Step 2 — fire 20 parallel Task calls

For each `as_of_date` in `ASOF_DATES_5Y`, construct the per-call
prompt by templating:

- `{as_of_date}` = the as-of date
- `{as_of_date_plus_one}` = the day after (for WebSearch `before:`)
- `{quarter_label}` = e.g., "2023-Q3"
- `{tickers}` = comma-separated ticker list (with NUKZ conditional)
- `{post_date_events}` = `\n`.join(events_after(as_of_date))

Send all 20 Task calls in a single message (parallel execution). Each
spawns the `news-researcher` subagent with the templated prompt.

When all 20 return, write each payload to:

```
data/news_asof_5y/<as_of_date>-news.json
```

with one extra top-level field: `"as_of_date": "<as_of_date>"`.

## Step 3 — aggregate (Bash)

```
python scripts/aggregate_wave_history_5y.py
```

Writes `data/wave_history_5y.csv` from the 20 payloads. Prints the
total count of `self_critique_downgrades` across all 20 calls — a
sanity check that the stand-in-the-shoes-of-the-date discipline is
actually catching hindsight leakage. Zero across 20 calls is
suspicious (the self-critique pass isn't biting).

## Step 4 — calibration probe

Manually inspect three quarters as a sanity check that the suppression
worked:

- **2021-Q3** (before ChatGPT): AI should be `buildup` or `neutral`,
  never `surge` or `peak`. Nuclear should be `neutral` (the data-center
  PPA narrative didn't exist yet). Rockets should be `neutral` or
  `buildup` (RKLB just listed).
- **2023-Q1** (ChatGPT shockwave begun, but pre-Nvidia $11B guide):
  AI should be `buildup` or `surge`. Nuclear still `neutral`.
- **2024-Q2** (Nvidia GB200 announced, Microsoft-Three Mile Island
  PPA not yet): AI should be `surge` or `peak`. Nuclear should be
  `buildup` or `neutral`, NOT yet `surge`.

If any of these comes out wildly post-dated (e.g., 2021-Q3 AI =
`surge`), the discipline failed and the affected payloads should
either be regenerated with a sterner preamble or marked as
contaminated in the downstream backtest.

## Final output to the user

One short message:

- Path to the aggregated history: `data/wave_history_5y.csv`.
- Total self-critique downgrades across all 20 calls.
- Quick calibration summary on the three probe quarters.
- Suggest committing the 20 payloads and the CSV on the branch:
  `git add data/news_asof_5y/ data/wave_history_5y.csv && git commit -m "5y wave-history backfill"`.

## Rules

- **Never write to `data/wave_history.csv` (the live file).** Output
  goes only to `data/wave_history_5y.csv` and `data/news_asof_5y/`.
- All 20 Task calls must use the templated prompt from
  `scripts/asof_news_prompt.md`. Do not reuse the standard
  news-researcher prompt; the as-of-date discipline is the whole point.
- After the 20 calls return, run the aggregator from a Bash shell
  (not by hand-merging JSON files).
- This is a one-shot skill for the 5y-backtest branch. If you need
  to re-run it, delete `data/news_asof_5y/` and `data/wave_history_5y.csv`
  first.
