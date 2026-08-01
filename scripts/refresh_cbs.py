#!/usr/bin/env python3
"""Render-only daily refresh of a forward PAPER portfolio dashboard (CBS by default, FT via flags).

Re-replays the FIXED curation JSONs of a run through the latest prices and re-renders its dashboard,
so the equity curve LIVE-EXTENDS as each daily price snapshot lands. This does NOT call the curator
(no LLM, no cost): it only rolls the replay window's end to today and re-runs the pure-math optimizer
replay + dashboard render.

Called by scripts/price_snapshot.sh after the daily snapshot, once per paper portfolio:
  CBS:  python scripts/refresh_cbs.py
  FT :  python scripts/refresh_cbs.py --run-dir data/curator_runs/forward-ft --out docs/index.html \
            --heading Forwardtest --acronym FT

To actually re-curate (feed new news through the LLM) use scripts/run_bootstrap_curator.py instead;
that pins the window back to the last curation date, and this script rolls it forward again.

Note: past the last curation date the replay keeps mean-variance rebalancing the FROZEN watchlist on
its cadence (no new adds/removes until the curator runs again).
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402


def _args(argv=None):
    p = argparse.ArgumentParser(description="Re-replay a paper portfolio at today's prices and re-render.")
    p.add_argument("--run-dir", default="data/curator_runs/bootstrap-cbs")
    p.add_argument("--out", default="docs/curator_bootstrap.html")
    p.add_argument("--heading", default="Curator Bootstrap")
    p.add_argument("--acronym", default="CBS")
    p.add_argument("--handoff", default="2026-07-22", help="backtest -> forward news boundary marker")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = _args(argv)
    RUN = ROOT / a.run_dir
    sp = RUN / "_starter.json"
    if not sp.exists():
        print(f"{a.acronym} not initialized (run run_bootstrap_curator.py first); skipping refresh",
              file=sys.stderr)
        return 0
    fm = portfolio.load_financial_model()
    anchors = fm.get("always_include") or ["SPY", "AGG", "IAU"]
    # Roll the replay window's end to today so the forward equity curve extends with new prices.
    starter = json.loads(sp.read_text())
    starter["end_date"] = datetime.now().strftime("%Y-%m-%d")
    sp.write_text(json.dumps(starter, indent=2))
    # The seeding CBT run (persisted by run_bootstrap_curator.py) for the paper-vs-backtest KPI table.
    _seed_src = starter.get("seed_src")
    _cmp_dir = str(ROOT / _seed_src / "_backtest") if _seed_src else None
    # Pure-math replay of the existing curation JSONs through the (now longer) window -- no LLM call.
    portfolio.curator_backtest(
        runs_dir=str(RUN), out_dir=str(RUN / "_backtest"), max_weight=float(fm["concentration_cap"]),
        risk_aversion=float(fm["risk_aversion"]), risk_free_rate=float(fm["risk_free_rate"]),
        t_update_days=int(portfolio.load_backtest_config()["t_update_days"]), benchmarks=["SPY"],
        lookback_years_override=fm["optimizer_lookback_days"] / 365.0, always_include=anchors,
        min_trade_frac=float(fm["min_trade_size_frac"]))     # model the live no-trade band in the replay
    portfolio.build_curator_dashboard(
        backtest_dir=str(RUN / "_backtest"), runs_dir=str(RUN), out_path=a.out,
        benchmarks=["SPY"], heading=a.heading, acronym=a.acronym, show_max_articles=False,
        handoff_date=a.handoff, compare_backtest_dir=_cmp_dir)
    print(f"  {a.acronym} refreshed to {starter['end_date']} -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
