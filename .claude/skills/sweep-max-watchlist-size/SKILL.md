# /sweep-max-watchlist-size

Sweeps the `max_watchlist_size` cap by firing fresh `watchlist-curator`
calls at four cap values (5, 12, 16, 24) over the **15 quarter-end dates of
the canonical post-COVID window (2022-03-31 → 2025-10-31)** — the same window
the other three sweeps (λ, lookback, concentration_cap) replay, so all four
sweeps are directly comparable. cap=8 is the project's default and *is* the
canonical `data/curator_runs/postcovid/` run dir, so it is already complete and
never re-fired here.

Each cap shapes the curator's decisions (not just the optimizer), so unlike the
other three sweeps this one cannot be a pure replay — every (cap, date) pair
needs its own curator call. The decisions are **path-dependent**: at each
rebalance the curator sees the watchlist as evolved from the 2022-03-31 starter
through all prior quarters, so a cap's 2022-03-31 JSON is not interchangeable
with any other run's — the JSONs from the old 5y window (2021→2026) cannot be
reused here.

**Cost / time:** 4 caps × 15 dates = **60 fresh curator calls** (Claude Code
subagent tokens, ~$0.15-equivalent each ≈ ~$9 from scratch; **no API key is
used**, so this never touches the GHR project's API credits). ~15 min wall
clock at 4-parallel batching. **Idempotent:** the skill skips any (cap, date)
pair whose JSON already exists, so partial runs resume cheaply. To avoid
contending with other Claude Code work for the shared subscription, run this in
its own session / off-hours.

## Before you start

1. cap=8 is the canonical postcovid run at `data/curator_runs/postcovid/`
   (15 JSONs) — do NOT re-fire it; the render script reads it directly.
2. The four swept-cap dirs `data/curator_runs/postcovid-cap{05,12,16,24}/`
   are created by `scripts/setup_curator_run.py` (Step 0). Each holds a
   `_starter.json` (starter `[AAPL, MSFT, GOOGL, NVDA, SPY]`, the postcovid
   window/dates, with its cap-specific `max_watchlist_size`) plus gitignored
   `_sandbox_*` files the curate replay uses.
3. Strict as-of-date discipline applies to every fresh curator call
   (persona reset, WebSearch `before:` filters, suppression list,
   self-critique). See `.claude/agents/watchlist-curator.md`.

## Orchestration

### Step 0 — create the per-cap run dirs (Bash, idempotent)

```bash
for cap in postcovid-cap05 postcovid-cap12 postcovid-cap16 postcovid-cap24; do
  .venv/bin/python scripts/setup_curator_run.py $cap
done
```

### Step 1 — figure out what's missing (Bash)

```bash
for cap in 05 12 16 24; do
  echo -n "cap=$cap: "
  ls data/curator_runs/postcovid-cap${cap}/*-curation.json 2>/dev/null | wc -l
done
```

Each cap should have 15 `<date>-curation.json` files when complete.

### Step 2 — fire curator calls for missing (cap, date) pairs (Task, batched)

The 15 quarter-end dates are:

```
2022-03-31, 2022-06-30, 2022-09-30, 2022-12-31,
2023-03-31, 2023-06-30, 2023-09-30, 2023-12-31,
2024-03-31, 2024-06-30, 2024-09-30, 2024-12-31,
2025-03-31, 2025-06-30, 2025-09-30
```

For each date in chronological order, fire up to 4 parallel Task calls (one per
cap = 5, 12, 16, 24), skipping the (cap, date) pair if its JSON already exists.
The dates must be processed in order because each call's `current_watchlist`
input depends on the prior dates' applied adds and removes.

Compute `current_watchlist` for each (cap, date) via:

```bash
.venv/bin/python scripts/replay_watchlist.py \
  --runs-dir data/curator_runs/postcovid-cap{NN} \
  --as-of {YYYY-MM-DD}
```

The script replays prior JSONs through the validator's logic (drops
"already in" adds, "stale" removes, and trailing adds that would push the
watchlist over the cap).

Each agent prompt (use `subagent_type="general-purpose"` since
`watchlist-curator` is not directly available via the Agent tool — embed the
curator spec inline) needs:

- `as_of_date`: the date.
- `current_watchlist`: from `replay_watchlist.py`.
- `max_watchlist_size`: the cap.
- `rebalance_period`: quarterly.
- `recent_news_lookback_days`: 90.
- `profile_wave_thesis`: AI ride/trim + rockets / robotics / quantum /
  nuclear (no engineered_biology). Load from `investor_profile.md`.
- `exclusions`: solar, wind.
- `post_date_events`: from
  `python -c "from scripts.post_date_events import events_after; print('\n'.join(events_after('{DATE}')))"`.

Save each Task's JSON return to
`data/curator_runs/postcovid-cap{NN}/{DATE}-curation.json`.

### Step 3 — render the sweep dashboard (Bash)

```bash
.venv/bin/python scripts/sweep_watchlist_size.py
```

This replays each cap's runs dir (including `postcovid/` for cap=8) through
`curator_backtest` at the profile's held-constant optimizer config — the same
config the other three sweeps use — so no separate per-cap `backtest` step is
needed. It writes `docs/sweep_max_watchlist_size.html` (chart + summary table +
nav strip). Caps with no curation JSONs yet are skipped, so the page renders
mid-build.

### Step 4 — commit and push (Bash)

```bash
git add data/curator_runs/postcovid-cap*/ docs/sweep_max_watchlist_size.html
git commit -m "Refresh max_watchlist_size sweep (postcovid window)"
git push origin main
```

## Rules

- cap=8 is the canonical postcovid run at `data/curator_runs/postcovid/`; it is
  already complete — never re-fire it.
- All caps use the same window as the other three sweeps (2022-03-31 →
  2025-10-31, 15 quarterly calls). If that window ever changes in
  `investor_profile.md`'s `backtest` section, every cap's JSONs must be
  re-fired (they are path-dependent on the start date).
- Dates are processed in chronological order per cap; cross-cap calls for the
  same date can run in parallel (no dependency).
- Skip (cap, date) pairs whose JSON already exists; the skill is resumable.
- Strict as-of-date discipline applies to every fresh call regardless of cap.
- The curator's wave_bucket enum technically allows `engineered_biology` but
  the current profile doesn't name biology; the prompt should explicitly forbid
  ARKG/NTLA/CRSP/EDIT to avoid the thesis-misalignment issue documented in
  CLAUDE.md.
