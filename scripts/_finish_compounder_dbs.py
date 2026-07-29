#!/usr/bin/env python3
"""One-off driver: after the 7-mws re-curation, build _backtest for each proto run, rebuild the flagship CBT
from the mws20 run, and (separately) the caller runs build_sweep_dashboard. INTERIM compounder pipeline.
Delete after the clean Wayback rebuild."""
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402

MWS = [4, 6, 8, 12, 16, 20, 24]


def main() -> int:
    # guard: every run must be a complete 79-date re-curation
    for m in MWS:
        n = len(glob.glob(str(ROOT / f"data/curator_runs/proto-mws{m}/*-curation.json")))
        if n < 79:
            print(f"ABORT: proto-mws{m} has {n}/79 curations", file=sys.stderr)
            return 1
    fm = portfolio.load_financial_model()
    anchors = [t.upper() for t in fm["always_include"]]
    tu = int(portfolio.load_backtest_config()["t_update_days"])
    kw = dict(max_weight=float(fm["concentration_cap"]), risk_aversion=float(fm["risk_aversion"]),
              risk_free_rate=float(fm["risk_free_rate"]), t_update_days=tu, benchmarks=["SPY"],
              lookback_years_override=fm["optimizer_lookback_days"] / 365.0, always_include=anchors,
              min_trade_frac=float(fm["min_trade_size_frac"]))
    # 1) _backtest for each proto run at the canonical config (sections 10-11 + title range read it)
    for m in MWS:
        rd = f"data/curator_runs/proto-mws{m}"
        portfolio.curator_backtest(runs_dir=rd, out_dir=f"{rd}/_backtest", **kw)
        print(f"  _backtest built: proto-mws{m}", file=sys.stderr)
    # 2) move the stale gem JSONs aside so BTS section 5 shows a clean placeholder (not old-config gems)
    for f in ("_gems.json", "_gem_diversity.json"):
        p = ROOT / "data" / "curator_runs" / f
        if p.exists():
            p.rename(p.with_suffix(p.suffix + ".preproto"))
            print(f"  moved aside: {f}", file=sys.stderr)
    # 3) flagship CBT from the mws20 run (interim, biased-lede note)
    note = (f"INTERIM (biased-lede): mws{fm['max_watchlist_size']} / cap{fm['concentration_cap']} / "
            f"λ{fm['risk_aversion']} / {fm['optimizer_lookback_days']}d / mt{fm['min_trade_size_frac']}, "
            f"anchors {anchors}. Curated on geosplit titles + look-ahead-BIASED live ledes; returns are "
            f"OPTIMISTIC pending the clean Wayback re-curation. New quality-gate curator prompt.")
    portfolio.build_curator_dashboard(
        backtest_dir="data/curator_runs/proto-mws20/_backtest", runs_dir="data/curator_runs/proto-mws20",
        out_path="docs/backtest_gkg_3yr_kimi.html", benchmarks=["SPY"], config_note=note,
        heading="Curator Backtest", acronym="CBT", show_max_articles=False)
    sn = json.loads((ROOT / "data/curator_runs/proto-mws20/_backtest/summary.json").read_text()) \
        if (ROOT / "data/curator_runs/proto-mws20/_backtest/summary.json").exists() else {}
    print("  flagship CBT rebuilt -> docs/backtest_gkg_3yr_kimi.html")
    print("DONE _backtest + CBT; now run: build_sweep_dashboard.py --recompute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
