#!/usr/bin/env python3
"""One-time migration: split the single holdings.csv into holdings.csv + watchlist.csv.

Before: holdings.csv = real positions (shares>0) AND the curator's watch-only rows (shares=0)
        AND the always_include anchors (shares=0), all in one file the cron and the user both wrote.
After:  holdings.csv  = real positions only (shares>0), user-edited only.
        watchlist.csv = the curator-managed universe (single 'ticker' column), cron-written only.

Behavior-preserving: the optimizer universe was `all holdings tickers`; it becomes
`watchlist ∪ held ∪ anchors` = (all non-anchor) ∪ held ∪ anchors = the same set.

Backs up the original to holdings.csv.pre-split.bak. Refuses to run if watchlist.csv already
exists (pass --force to overwrite). Run once:  .venv/bin/python scripts/migrate_watchlist.py
"""
import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402


def main(force: bool) -> int:
    holdings_p = ROOT / "holdings.csv"
    watchlist_p = ROOT / "watchlist.csv"
    backup_p = ROOT / "holdings.csv.pre-split.bak"

    if not holdings_p.exists():
        print("no holdings.csv to migrate", file=sys.stderr)
        return 1
    if watchlist_p.exists() and not force:
        print(f"{watchlist_p.name} already exists; refusing to overwrite (pass --force)", file=sys.stderr)
        return 1

    df = pd.read_csv(holdings_p)
    if "ticker" not in df.columns or "shares" not in df.columns:
        print("holdings.csv must have ticker,shares columns", file=sys.stderr)
        return 1
    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df["shares"] = df["shares"].astype(float)
    anchors = {t.upper() for t in portfolio.load_financial_model().get("always_include", [])}

    watchlist = [t for t in df["ticker"].tolist() if t not in anchors]     # curator universe = all non-anchor
    held = df[df["shares"] > 0][["ticker", "shares"]]                       # real positions = shares>0

    shutil.copyfile(holdings_p, backup_p)
    pd.DataFrame({"ticker": watchlist}).to_csv(watchlist_p, index=False)
    held.to_csv(holdings_p, index=False)

    print(f"backed up original -> {backup_p.name}")
    print(f"watchlist.csv ({len(watchlist)} tickers): {watchlist}")
    print(f"holdings.csv ({len(held)} real positions): {held['ticker'].tolist()}")
    print(f"anchors (from profile, enter universe separately): {sorted(anchors)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Split holdings.csv into holdings.csv + watchlist.csv (one-time).")
    ap.add_argument("--force", action="store_true", help="overwrite an existing watchlist.csv")
    sys.exit(main(ap.parse_args().force))
