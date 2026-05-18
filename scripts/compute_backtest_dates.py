"""Compute the rolling 5-year set of quarter-end dates for /run-backtest.

Prints a JSON object the orchestrating skill consumes:

    {
      "today":           "YYYY-MM-DD",
      "runs_dir":        "data/curator_runs/5y-quarterly",
      "starter":         ["AAPL", "MSFT", "GOOGL", "SPY", "AGG"],
      "target_dates":    ["YYYY-MM-DD", ...],   # 20 most recent quarter-ends
      "existing_dates":  ["YYYY-MM-DD", ...],   # already have <date>-curation.json
      "missing_dates":   ["YYYY-MM-DD", ...],   # in target, not in existing
      "stale_dates":     ["YYYY-MM-DD", ...]    # in existing, no longer in target
    }

The skill fires curator Task calls for missing_dates, archives stale_dates,
regenerates _starter.json, and then invokes the replay backtest.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

DEFAULT_RUNS_DIR = "data/curator_runs/5y-quarterly"
STARTER_WATCHLIST = ["AAPL", "MSFT", "GOOGL", "NVDA", "SPY"]
N_QUARTERS = 21  # rolling-5y window: 21 quarter-ends span 20 intervals = 5y


def quarter_ends_through(today: pd.Timestamp, n: int = N_QUARTERS) -> list[pd.Timestamp]:
    """Return the n most recent calendar-quarter-end dates at or before today."""
    # Snap today back to the most recent quarter-end.
    qe = today + pd.offsets.QuarterEnd(0)
    if qe > today:
        qe -= pd.offsets.QuarterEnd()
    ends: list[pd.Timestamp] = []
    while len(ends) < n:
        ends.append(qe)
        qe -= pd.offsets.QuarterEnd()
    return sorted(ends)


def existing_curation_dates(runs_dir: Path) -> list[pd.Timestamp]:
    """Return sorted list of dates for which a <date>-curation.json already exists."""
    if not runs_dir.exists():
        return []
    out: list[pd.Timestamp] = []
    for f in runs_dir.glob("*-curation.json"):
        stem = f.stem.replace("-curation", "")
        try:
            out.append(pd.Timestamp(stem))
        except ValueError:
            continue
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--today", default=None,
                   help="override today's date (YYYY-MM-DD). Used for testing.")
    p.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    p.add_argument("--n-quarters", type=int, default=N_QUARTERS)
    args = p.parse_args(argv)

    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.today().normalize()
    runs_dir = Path(args.runs_dir)
    target = quarter_ends_through(today, args.n_quarters)
    existing = existing_curation_dates(runs_dir)
    target_set = {d.strftime("%Y-%m-%d") for d in target}
    existing_set = {d.strftime("%Y-%m-%d") for d in existing}
    missing = sorted(target_set - existing_set)
    stale = sorted(existing_set - target_set)

    print(json.dumps({
        "today": today.strftime("%Y-%m-%d"),
        "runs_dir": str(runs_dir),
        "starter": STARTER_WATCHLIST,
        "target_dates": sorted(target_set),
        "existing_dates": sorted(existing_set),
        "missing_dates": missing,
        "stale_dates": stale,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
