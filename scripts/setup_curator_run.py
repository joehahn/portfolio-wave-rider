"""Set up a curator-backtest runs directory.

Creates ``data/curator_runs/<run_id>/`` with:
- ``_starter.json``: run config (starter watchlist, window, cadence, etc.)
- ``_sandbox_holdings.csv``: tracks the watchlist state as we fire agents
  in batches; the curate code path mutates this file. Initialized to the
  starter watchlist at shares=0.
- ``_sandbox_history.csv`` is created empty; ``apply_curator_decisions``
  appends to it as each saved payload is replayed forward.
- ``_sandbox_profile.md``: a tiny YAML-front-matter file holding
  max_watchlist_size so the curate code path can load it.

Usage:
    python scripts/setup_curator_run.py 5y-quarterly

The script is idempotent: re-running won't clobber existing files, so
captured curation payloads survive setup reruns.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# Quarterly cadence over the 5y window (Sept 2021 to Apr 2026). Matches
# scripts/post_date_events.py ASOF_DATES_5Y exactly.
ASOF_DATES_5Y_QUARTERLY = [
    "2021-09-30", "2021-12-31",
    "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31",
    "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
    "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
    "2026-03-31", "2026-04-30",
]

RUNS = {
    "5y-quarterly": {
        "starter_watchlist": ["AAPL", "MSFT", "GOOGL", "SPY", "AGG"],
        "as_of_dates": ASOF_DATES_5Y_QUARTERLY,
        "rebalance_period": "quarterly",
        "initial_usd": 50000.0,
        "lookback_years": 1.3,
        "max_watchlist_size": 12,
        "start_date": "2021-09-30",
        "end_date": "2026-04-30",
    },
}


def setup(run_id: str) -> Path:
    if run_id not in RUNS:
        raise SystemExit(f"unknown run_id {run_id!r}; known: {sorted(RUNS)}")
    cfg = RUNS[run_id]
    out = Path(f"data/curator_runs/{run_id}")
    out.mkdir(parents=True, exist_ok=True)

    starter_path = out / "_starter.json"
    if starter_path.exists():
        print(f"{starter_path}: already exists, leaving alone")
    else:
        starter_path.write_text(json.dumps(cfg, indent=2))
        print(f"{starter_path}: wrote run config")

    holdings = out / "_sandbox_holdings.csv"
    if holdings.exists():
        print(f"{holdings}: already exists, leaving alone")
    else:
        pd.DataFrame({
            "ticker": cfg["starter_watchlist"],
            "shares": [0] * len(cfg["starter_watchlist"]),
        }).to_csv(holdings, index=False)
        print(f"{holdings}: initialized to starter watchlist")

    profile = out / "_sandbox_profile.md"
    if not profile.exists():
        profile.write_text(
            "---\n"
            f"financial_model:\n"
            f"  max_watchlist_size: {cfg['max_watchlist_size']}\n"
            "---\n"
            f"# sandbox profile for {run_id}\n"
        )
        print(f"{profile}: wrote sandbox profile")

    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", default="5y-quarterly")
    args = ap.parse_args()
    out = setup(args.run_id)
    print(f"\nrun dir: {out}")
    print(f"to inspect: ls {out}/")
    sys.exit(0)
