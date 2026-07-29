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
    # 3) flagship CBT from the mws20 run (look-ahead-clean note)
    note = (f"mws{fm['max_watchlist_size']} / cap{fm['concentration_cap']} / λ{fm['risk_aversion']} / "
            f"{fm['optimizer_lookback_days']}d / mt{fm['min_trade_size_frac']}, anchors {anchors}. "
            f"Look-ahead-CLEAN: curated on geosplit pools with archived Wayback ledes where available (~46%) "
            f"else title-only, biased live ledes ignored. New quality-gate curator prompt. In-sample backtest.")
    portfolio.build_curator_dashboard(
        backtest_dir="data/curator_runs/proto-mws20/_backtest", runs_dir="data/curator_runs/proto-mws20",
        out_path="docs/backtest_gkg_3yr_kimi.html", benchmarks=["SPY"], config_note=note,
        heading="Curator Backtest", acronym="CBT", show_max_articles=False)
    sn = json.loads((ROOT / "data/curator_runs/proto-mws20/_backtest/summary.json").read_text()) \
        if (ROOT / "data/curator_runs/proto-mws20/_backtest/summary.json").exists() else {}
    print("  flagship CBT rebuilt -> docs/backtest_gkg_3yr_kimi.html")
    # 4) geosplit _authors.json from the pool bylines, so the RBT's author plot (9) populates
    geo = ROOT / "data" / "curator_runs" / "gkg-3yr-geosplit"
    au = {}
    for pf in geo.glob("*-pool.json"):
        for a in json.loads(pf.read_text()).get("articles", []):
            if a.get("url") and (a.get("author") or "").strip():
                au.setdefault(a["url"], a["author"])
    (geo / "_authors.json").write_text(json.dumps(au, indent=1))
    print(f"  geosplit _authors.json: {len(au)} bylines")
    print("DONE _backtest + CBT + authors; now run build_retrieval_dashboard.py and build_sweep_dashboard.py --recompute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
