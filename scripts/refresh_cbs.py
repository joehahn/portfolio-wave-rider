#!/usr/bin/env python3
"""Render-only daily refresh of the Curator Bootstrap (CBS) dashboard.

Re-replays the FIXED bootstrap curation JSONs through the latest prices and re-renders
docs/curator_bootstrap.html, so the forward equity curve LIVE-EXTENDS as each daily price
snapshot lands. This does NOT call the curator (no LLM, no cost): it only rolls the replay
window's end to today and re-runs the pure-math optimizer replay + dashboard render.

Called by scripts/price_snapshot.sh after the daily snapshot. To actually re-curate (feed new
bootstrap news through the LLM) use scripts/run_bootstrap_curator.py instead; that pins the
window back to the last curation date, and this script rolls it forward again on the next run.

Note: past the last bootstrap curation date the replay keeps mean-variance rebalancing the
FROZEN watchlist on its cadence (no new adds/removes until run_bootstrap_curator.py is re-run).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402

RUN = ROOT / "data" / "curator_runs" / "bootstrap-cbs"


def main() -> int:
    sp = RUN / "_starter.json"
    if not sp.exists():
        print("CBS not initialized (run run_bootstrap_curator.py first); skipping refresh", file=sys.stderr)
        return 0
    fm = portfolio.load_financial_model()
    anchors = fm.get("always_include") or ["SPY", "AGG", "IAU"]
    # Roll the replay window's end to today so the forward equity curve extends with new prices.
    starter = json.loads(sp.read_text())
    starter["end_date"] = datetime.now().strftime("%Y-%m-%d")
    sp.write_text(json.dumps(starter, indent=2))
    # Pure-math replay of the existing curation JSONs through the (now longer) window -- no LLM call.
    portfolio.curator_backtest(
        runs_dir=str(RUN), out_dir=str(RUN / "_backtest"), max_weight=float(fm["concentration_cap"]),
        risk_aversion=float(fm["risk_aversion"]), benchmarks=["SPY"],
        lookback_years_override=fm["optimizer_lookback_days"] / 365.0, always_include=anchors)
    portfolio.build_curator_dashboard(
        backtest_dir=str(RUN / "_backtest"), runs_dir=str(RUN), out_path="docs/curator_bootstrap.html",
        benchmarks=["SPY"], heading="Curator Bootstrap", acronym="CBS", show_max_articles=False,
        handoff_date="2026-07-22")
    print(f"  CBS refreshed to {starter['end_date']} -> docs/curator_bootstrap.html", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
