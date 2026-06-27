---
name: initialize-portfolio
description: One-shot portfolio bootstrap. Reads the investor profile and an empty (all-shares-zero) holdings.csv, produces a thesis-driven dollar allocation across the watchlist, converts dollars to shares, records the thesis snapshot, persists the allocation to data/thesis_baseline.json so subsequent /review-portfolio runs can render thesis-vs-recommended comparisons, and writes a thesis-only report. No optimizer, no news, no LLM cost beyond the allocation reasoning.
---

# /initialize-portfolio

The day-1 bootstrap. Run this **once**, on a fresh repo, before any `/review-portfolio` call. It translates your wave thesis (in `investor_profile.md`) into a concrete dollar allocation across the tickers in `holdings.csv`. After this runs, `holdings.csv` has real share counts and `data/thesis_baseline.json` exists; from that point on you run `/review-portfolio` for the recurring math + news + report cycle.

## Before you start

1. Read `investor_profile.md`. If missing or empty, stop and tell the user to copy `investor_profile.example.md` to `investor_profile.md` and edit. Confirm `initial_investment_usd` is present and positive.
2. Read `holdings.csv`. **Every row must have `shares == 0`**. If any row has nonzero shares, stop and tell the user: this skill is one-shot bootstrap; running again would overwrite their existing positions. Suggest `/review-portfolio` instead.
3. If `data/thesis_baseline.json` already exists, stop and tell the user: thesis is already set; running `/initialize-portfolio` again would overwrite the persisted thesis baseline used by every subsequent /review-portfolio comparison. They can delete the file manually if they want to redo the thesis from scratch.

## Step 1 — propose the thesis allocation (LLM)

Produce a JSON object mapping each watchlist ticker to a dollar amount. Constraints:

- The dollar amounts must sum to `initial_investment_usd` exactly.
- Honor the profile's `exclusions`: any ticker in an excluded sector gets $0.
- Weight tickers using the wave thesis. For equities: lean into the current AI wave, then the named "next waves" listed in the profile (rockets/spacecraft, robotics, quantum computing, nuclear — fission and fusion). Non-equity tickers (bonds, cash, gold ETFs) get a smaller share unless the profile names a corresponding wave. Tickers tied to past or unrelated waves get smaller weights.
- Respect `concentration_cap`: no single ticker gets more than that fraction of `initial_investment_usd`.
- Do not optimize for Sharpe, volatility, or any other math metric. **This is the user's beliefs in dollar form.** No optimizer is involved at this step.

Before producing the JSON, state the reasoning per asset class and per ticker in plain prose. Cite the wave thesis when assigning equity weights. Save this reasoning text — it goes into the report and into `data/thesis_baseline.json`.

## Step 2 — convert dollars to shares (Bash)

```
python -m src.cli init-holdings --allocations '<json from step 1>' --out holdings.csv
```

Capture the per-ticker price, shares, and value from the CLI's JSON return.

## Step 3 — record the thesis snapshot (Bash)

```
python -m src.cli snapshot --force
```

Writes the first row of `data/snapshots.csv`. From here forward, every chart on the live dashboard scopes to dates >= today.

## Step 4 — persist the thesis baseline (Bash)

Write `data/thesis_baseline.json` with this schema:

```
{
  "date": "<today YYYY-MM-DD>",
  "allocations_usd": <step 1 JSON: ticker -> dollars>,
  "reasoning": <step 1 prose>,
  "holdings": <step 2 CLI return: per-ticker price/shares/value>
}
```

Use Bash + a small Python heredoc to write the JSON; do not invent CLI machinery.

## Step 5 — write the thesis-only report (Task → report-writer)

Pass:

```
{
  "user_request": <original prompt; e.g., "initialize portfolio">,
  "analysis": null,
  "news": null,
  "profile_conflicts": [],
  "thesis_baseline": <contents of step 4 JSON>
}
```

Write this report yourself, in the main loop, with the Write tool, to `data/reports/<date>-initialize-portfolio.md`. **Do not delegate to the `report-writer` subagent** (background subagents both fail to propagate file writes to the real repo and stall on the heavy read-then-generate step; the report is authored inline). Produce only the **The ask**, **Thesis allocation**, and **Caveats** sections (there is no optimizer output, no news, and no recommended weights), following `.claude/agents/report-writer.md`'s "Report structure" and "Table formatting" for those sections. After writing, grep the file for narrative em dashes (`grep -n "—" data/reports/<date>-initialize-portfolio.md`) and replace any in-sentence em dashes, leaving only the `# <Skill> — <date>` title and any table empty-cell placeholders.

## Step 6 — refresh the dashboard (Bash)

```
python -m src.cli dashboard --out docs/index.html
```

Renders the live dashboard. Most charts will be sparse (one date of snapshots, no recommendations yet) — that's expected. The first `/review-portfolio` will fill them in.

## Final output to the user

One short message:

- Path to the report: `data/reports/<date>-initialize-portfolio.md`.
- Total dollars allocated, ticker count.
- Two next steps: "(1) Run `/review-portfolio` to get the optimizer's recommended allocation and the thesis-vs-recommended comparison. (2) Open `docs/index.html` to see the dashboard."

## Rules

- **One-shot only.** The all-zero precondition + thesis_baseline.json existence check makes this safe to invoke as a guard.
- **No optimizer call.** The thesis allocation is beliefs in dollar form, not Sharpe-optimal. The optimizer enters at /review-portfolio.
- **No news-researcher call.** No LLM cost beyond the thesis-reasoning prose.
- Numbers come from `src.cli`: `init-holdings` returns per-ticker price/shares/value; `snapshot` records the position. The thesis dollar weights come from the LLM, but every share count and price passes through Python.
- Do not modify `investor_profile.md` mid-run.
