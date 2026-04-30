# Portfolio Wave Rider

A Claude Code demo of **skills + subagents** orchestrating a long-horizon
portfolio workflow against a user-authored profile. Skills are the
invocation surface; subagents are specialists with narrow tool
allowlists; all numbers come from Python via one CLI.

Audience: technical users new to skills/subagents.

## What it does

Three cadences, plus a dashboard:

| Cadence | Mechanism | What runs | Output |
|---|---|---|---|
| **Daily** (Mon–Fri 16:30 local) | macOS launchd | `snapshot && dashboard` — fetch close prices, append $ values per holding, refresh dashboard | `data/snapshots.csv`, `data/dashboard.html` |
| **Weekly** (Fri 17:00 local) | macOS launchd | `recommend && dashboard` — re-optimize over the holdings universe, refresh dashboard | `data/recommendations.csv`, `data/dashboard.html` |
| **Monthly** (you decide) | You run `/review-portfolio` in Claude Code | Full LLM run: news + analyze + report + dashboard refresh | `data/reports/YYYY-MM-DD-review-portfolio.md`, `data/dashboard.html` |

The weekly cron is the **lightweight Python-only sibling** of
`/review-portfolio` — no news, no wave tilts. Run the skill when you
want a fresh wave-stage read and a written narrative.

## How it's built

- **Skill** (`.claude/skills/review-portfolio/SKILL.md`) — the one slash
  command you invoke: `/review-portfolio`. It orchestrates the
  subagents and produces a markdown report + refreshed dashboard.
- **Subagents** (`.claude/agents/*.md`) — two LLM specialists:
  - `news-researcher` — headlines per ticker; classifies wave stages
  - `report-writer` — synthesizes the final markdown
- **Python** (`src/portfolio.py` + `src/cli.py`) — all math in two
  files; one `analyze` CLI call does fetch + optimize + risk in one shot.
  The skill invokes the CLI via Bash; LLMs never compute numbers.
- **Profile** (`investor_profile.md`) — the source of truth. The skill
  reads it before recommending anything. When the optimal numerical
  answer violates a constraint, the report **flags the conflict**
  rather than silently clamping. That's the demo's punchline.

The flagship `/review-portfolio` flow:

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

Two LLM specialists (blue) bracket one Python call (yellow). The
profile and `news_sources.md` are read-only inputs.

## Initial setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy the example profile and holdings, then edit them:
cp investor_profile.example.md investor_profile.md
cp holdings.example.csv holdings.csv
```

The two files you maintain:

- **`investor_profile.md`** — goals, risk tolerance, concentration cap,
  exclusions, asset-class targets. Every recommendation cites lines from
  this file.
- **`holdings.csv`** — `ticker,shares` for everything you want tracked.
  Set `shares` to 0 for tickers you're watching but don't yet own; the
  daily snapshot still logs prices, so you have history when you buy in.

Optional: `news_sources.md` (curated sources per technology wave —
improves the news-researcher's signal; missing is fine, falls back to
open search).

## Daily / weekly / monthly: what you do

- **Daily** — nothing. The launchd job appends a row per ticker to
  `data/snapshots.csv` and refreshes `data/dashboard.html` at 16:30 local.
- **Weekly** — nothing. Friday 17:00 local appends one optimization run
  to `data/recommendations.csv` and refreshes the dashboard.
- **Monthly** — run `/review-portfolio` in Claude Code. Read the report,
  decide on rebalances, execute trades in your brokerage, then update
  `holdings.csv` to match.
- **Whenever** — open `data/dashboard.html` in a browser to see
  portfolio value, weight drift, and the latest recommended weights.
- **When trading** — edit `holdings.csv` to reflect new share counts.
  The snapshot picks up the new positions on its next run.

## Outputs to monitor

| File | What's in it | When to look |
|---|---|---|
| `data/dashboard.html` | Three Plotly charts: portfolio value, weight drift, latest weights | Open in a browser any time |
| `data/snapshots.csv` | Daily $ value per ticker + total | If you want raw history |
| `data/recommendations.csv` | Weekly optimization weights + Sharpe | If you want raw history |
| `data/reports/*.md` | LLM-written narrative reports | After each `/review-portfolio` |
| `data/snapshot.log` / `data/recommend.log` | launchd stdout/stderr | If a scheduled run looks missing |

The **"Profile conflicts"** section of any report is the most important
thing to read — it tells you when the math wants something your profile
forbids.

## What to think about

- **The wave thesis vs. the optimizer.** Your profile probably says
  "aggressive, ride tech waves." Mean-variance over a 2–3 year window
  often prefers the safe-haven sleeve (bonds/gold/cash) because it had
  a smooth recent run. The conflict section will show this gap. You
  decide whether to override.
- **Sample bias.** The realized Sharpe on any 2–3 year window is
  usually optimistic vs. forward-looking reality. Watch the
  in-sample/out-of-sample degradation in the risk report.
- **Estimation error.** Mean-variance amplifies small errors in
  expected-return estimates. Heavy weight at the concentration cap is
  often a symptom, not signal.
- **Wave-stage tilts.** The `/review-portfolio` skill applies
  multipliers (buildup 1.20x, surge 1.10x, peak 0.80x, etc.) based on
  the news-researcher's read. These tilts are conditional — track the
  realized vs. tilted Sharpe gap (the "views premium") to see if they
  pay.
- **The numbers come from Python.** If you ever see a figure in a
  report that didn't come from `src.cli`, that's a bug — flag it.

## CLI reference

Four subcommands. The skill calls `analyze`; the cron jobs call the
other three.

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

Plists live at `~/Library/LaunchAgents/com.user.portfolio-{snapshot,recommend}.plist`.
launchd runs only while logged in; if your Mac is asleep at trigger
time the job runs on wake; if powered off the run is missed (use
`--date YYYY-MM-DD` to backfill).

## Layout

```
portfolio-wave-rider/
├── investor_profile.md      # your north star (you write this; gitignored)
├── investor_profile.example.md  # template to copy
├── holdings.csv             # ticker,shares (you maintain this; gitignored)
├── holdings.example.csv     # template to copy
├── news_sources.md          # optional curated sources per wave
├── CLAUDE.md                # rules for Claude operating in this repo
├── .claude/
│   ├── agents/              # 2 subagent specs (news-researcher, report-writer)
│   ├── skills/              # 1 skill (review-portfolio)
│   └── settings.json        # tool allowlist
├── src/
│   ├── portfolio.py         # all math
│   └── cli.py               # one CLI, four subcommands
├── tests/
└── data/
    ├── snapshots.csv        # daily, appended (your history)
    ├── recommendations.csv  # weekly, appended (your history)
    ├── dashboard.html       # static Plotly dashboard (gitignored, regenerated)
    ├── reports/             # LLM-written reports (gitignored)
    └── *.log                # launchd output (gitignored)
```

## Testing

```bash
.venv/bin/pytest tests/    # offline; no network, no API
```

## Extending it

- New math → add a function to `src/portfolio.py` and a subcommand to
  `src/cli.py`.
- New specialist → add `.claude/agents/<name>.md`.
- New workflow → add `.claude/skills/<name>/SKILL.md`.

## Disclaimer

Technical demo. Not financial advice. Historical performance is not
predictive. Don't trade real money on this output without independent
verification.

## License

MIT.
