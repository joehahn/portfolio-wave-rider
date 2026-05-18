"""Replay curator JSONs chronologically to compute the current watchlist
at a given date, respecting the max_watchlist_size cap. Used by the
sweep-max-watchlist-size orchestrator to seed each new curator call.

Usage:
  python scripts/replay_watchlist.py --runs-dir <dir> --as-of <YYYY-MM-DD>

Prints the current watchlist as a JSON list. JSONs in <runs-dir> with
date >= as-of are skipped (since we're constructing the watchlist that
the date-<as-of> curator agent would see).

Validation mirrors a subset of apply_curator_decisions:
- skip adds for tickers already on the watchlist
- skip adds that would push size > cap (cap from _starter.json)
- skip removes for tickers not on the watchlist

Does NOT validate listing dates via yfinance — that's the math replay's
job. Good enough for staging the next agent's prompt.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runs-dir", required=True)
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; replay JSONs strictly before this date")
    args = p.parse_args(argv)

    runs = Path(args.runs_dir)
    starter_path = runs / "_starter.json"
    starter = json.loads(starter_path.read_text())
    cap = int(starter.get("max_watchlist_size", 12))
    watchlist: list[str] = list(starter["starter_watchlist"])

    json_files = sorted(p for p in runs.glob("*-curation.json"))
    for f in json_files:
        date = f.stem.replace("-curation", "")
        if date >= args.as_of:
            continue
        d = json.loads(f.read_text())
        # Mirror _validate_curator_payload: filter "already in" adds and
        # "stale" removes, then compute (current - removes) | adds. If
        # over cap, drop adds from the tail.
        raw_adds = [(a.get("ticker") or "").upper() for a in (d.get("adds") or [])]
        raw_removes = [(r.get("ticker") or "").upper() for r in (d.get("removes") or [])]
        adds = [t for t in raw_adds if t and t not in watchlist]
        removes = [t for t in raw_removes if t in watchlist]
        post = [t for t in watchlist if t not in removes] + [t for t in adds if t not in removes]
        if len(post) > cap:
            excess = len(post) - cap
            adds = adds[:-excess] if excess <= len(adds) else []
            post = [t for t in watchlist if t not in removes] + adds
        watchlist = post

    print(json.dumps(watchlist))
    return 0


if __name__ == "__main__":
    sys.exit(main())
