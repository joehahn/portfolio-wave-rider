# Portfolio Wave Rider

**Author:** Joe Hahn  
**Email:** jmh.datasciences@gmail.com  
**Date:** 2026-April-30 <br>
**branch:** main

A Claude Code demo: optimize a long-horizon stock and ETF portfolio against a user-authored investor profile. One slash command, two LLM subagents, four Python CLI subcommands, and a static dashboard.

## What it does

Three cadences:

| Cadence | Mechanism | What runs | Output |
|---|---|---|---|
| Daily, Mon-Fri 16:30 local | macOS launchd | `snapshot && dashboard`. Fetches close prices, appends $ values per holding, refreshes the dashboard. | `data/snapshots.csv`, `data/dashboard.html` |
| Weekly, Fri 17:00 local | macOS launchd | `recommend && dashboard`. Re-optimizes over the holdings universe, refreshes the dashboard. | `data/recommendations.csv`, `data/dashboard.html` |
| Monthly, you decide | You run `/review-portfolio` in Claude Code | Full LLM run: news plus analyze plus report plus dashboard refresh. | `data/reports/YYYY-MM-DD-review-portfolio.md`, `data/dashboard.html` |

The weekly cron is the lightweight Python-only sibling of `/review-portfolio`: pure Python, no news, no wave tilts. Run the skill when you want a fresh wave-stage read and a written narrative.

## How it's built

- One skill, `.claude/skills/review-portfolio/SKILL.md`. Reads the profile, calls the two subagents and the Python CLI, writes the report and refreshes the dashboard.
- Two subagents at `.claude/agents/`:
  - `news-researcher`: picks wave-aligned news per ticker, classifies each wave's stage, returns a `wave_views` mapping.
  - `report-writer`: synthesizes the analysis and news into the final markdown report.
- All Python in two files: `src/portfolio.py` (math) and `src/cli.py` (one entry point with four subcommands).
- The user-authored `investor_profile.md` is the source of truth. Every recommendation cites lines from it. When the optimal numerical answer violates a profile constraint, the report flags the conflict; it does not silently clamp.

```mermaid
flowchart TD
    user([User]) -->|/review-portfolio| skill[Skill: review-portfolio]
    profile[(investor_profile.md)] -.read.-> skill
    skill --> news[news-researcher]
    sources[(news_sources.md)] -.read.-> news
    skill -->|src.cli analyze --wave-views| analyze[Python: fetch + optimize + risk]
    news --> writer[report-writer]
    analyze --> writer
    writer --> out[/report.md + dashboard.html/]

    classDef agent fill:#e1f0ff,stroke:#3b82f6
    classDef cli fill:#fef3c7,stroke:#d97706
    classDef file fill:#f3f4f6,stroke:#6b7280
    class news,writer agent
    class analyze cli
    class out file
```

Two LLM specialists (blue) bracket one Python call (yellow). The profile and `news_sources.md` are read-only inputs.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy templates and edit:
cp investor_profile.example.md investor_profile.md
cp holdings.example.csv holdings.csv
```

The two files you maintain:

- `investor_profile.md`: goals, risk tolerance, concentration cap, exclusions, asset-class targets. Every recommendation cites lines from this file.
- `holdings.csv`: `ticker,shares` for everything you want tracked. Set `shares` to 0 for tickers you watch but do not yet own; the daily snapshot still logs prices.

Optional: `news_sources.md`, a curated list of sources per technology wave. Improves the news-researcher's signal. Missing is fine; falls back to open search.

## Operations

- Daily: nothing. The launchd job appends a row per ticker to `data/snapshots.csv` and refreshes `data/dashboard.html`.
- Weekly: nothing. Friday 17:00 local appends one optimization run to `data/recommendations.csv` and refreshes the dashboard.
- Monthly: run `/review-portfolio` in Claude Code. Read the report, decide on rebalances, execute trades in your brokerage, then update `holdings.csv`.
- Anytime: open `data/dashboard.html` in a browser.
- After trading: edit `holdings.csv` to reflect new share counts. The next snapshot picks up the new positions.

## Outputs to monitor

| File | What's in it | When to look |
|---|---|---|
| `data/dashboard.html` | Three Plotly charts: portfolio value over time, weight drift, latest recommended weights | Open in a browser any time |
| `data/snapshots.csv` | Daily $ value per ticker plus total | If you want raw history |
| `data/recommendations.csv` | Weekly optimization weights and Sharpe | If you want raw history |
| `data/reports/*.md` | LLM-written narrative reports | After each `/review-portfolio` |
| `data/snapshot.log`, `data/recommend.log` | launchd stdout/stderr | If a scheduled run looks missing |

The "Profile conflicts" section of any report is the most important thing to read. It tells you when the math wants something the profile forbids.

## Things to watch

- Wave thesis vs the optimizer. An aggressive profile that says "ride tech waves" is often outvoted by mean-variance over a 2-3 year window, because the safe-haven sleeve had a smooth recent run. The conflict section will show this gap. You decide whether to override.
- Sample bias. The realized Sharpe on any 2-3 year window is usually optimistic vs forward-looking reality.
- Estimation error. Mean-variance amplifies small errors in expected-return estimates. Heavy weight at the concentration cap is often a symptom, not signal.
- Wave-stage tilts. The skill applies multipliers based on the news-researcher's read: buildup 1.20x, surge 1.10x, peak 0.80x, digestion 0.90x, neutral 1.00x. Track the realized vs tilted Sharpe gap to see if these tilts pay.
- Numbers come from Python. If a figure in a report did not come from `src.cli`, that is a bug.

## CLI reference

Four subcommands. The skill calls `analyze`. The cron jobs call the other three.

```bash
# One-shot analysis (fetch + optimize + risk in a single call)
.venv/bin/python -m src.cli analyze --tickers AAPL MSFT NVDA --period 3y --max-weight 0.25

# Time-series logging
.venv/bin/python -m src.cli snapshot   [--date YYYY-MM-DD] [--force]
.venv/bin/python -m src.cli recommend  [--max-weight 0.25] [--force]

# Static dashboard (reads the two CSVs above; writes data/dashboard.html)
.venv/bin/python -m src.cli dashboard
```

## launchd management

```bash
launchctl list | grep portfolio                              # status
launchctl start com.user.portfolio-snapshot                  # run snapshot now
launchctl start com.user.portfolio-recommend                 # run recommend now
launchctl unload ~/Library/LaunchAgents/com.user.portfolio-snapshot.plist   # disable
```

Plists live at `~/Library/LaunchAgents/com.user.portfolio-{snapshot,recommend}.plist`. launchd runs only while logged in. If the Mac is asleep at trigger time the job runs on wake. If powered off the run is missed. Use `--date YYYY-MM-DD` to backfill.

## Layout

```
portfolio-wave-rider/
├── investor_profile.md         # source of truth (you write this; gitignored)
├── investor_profile.example.md # template to copy
├── holdings.csv                # ticker,shares (you maintain this; gitignored)
├── holdings.example.csv        # template to copy
├── news_sources.md             # optional curated sources per wave
├── CLAUDE.md                   # rules for Claude operating in this repo
├── .claude/
│   ├── agents/                 # 2 subagent specs (news-researcher, report-writer)
│   ├── skills/                 # 1 skill (review-portfolio)
│   └── settings.json           # tool allowlist
├── src/
│   ├── portfolio.py            # all math
│   └── cli.py                  # one CLI, four subcommands
├── tests/
└── data/
    ├── snapshots.csv           # daily, appended (your history)
    ├── recommendations.csv     # weekly, appended (your history)
    ├── dashboard.html          # static Plotly dashboard (gitignored, regenerated)
    ├── reports/                # LLM-written reports (gitignored)
    └── *.log                   # launchd output (gitignored)
```

## Testing

```bash
.venv/bin/pytest tests/    # offline; no network, no API
```

## Notes

This project was developed with [Claude Code](https://claude.com/claude-code). See `CLAUDE.md` for the rules Claude follows when operating in this repo.

## Disclaimer

Technical demo. Not financial advice. Historical performance is not predictive. Do not trade real money on this output without independent verification.

## License

MIT.
