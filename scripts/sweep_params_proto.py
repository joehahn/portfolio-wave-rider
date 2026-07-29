#!/usr/bin/env python3
"""Free optimizer-param sweep over the 7 titles+live mws curation sets (proto-mwsN).

For every (mws, cap, lambda, lookback, min_trade) the mean-variance replay is a $0 local re-solve on the
already-produced curations. Computes, per config: full-window and trailing-year return + giveback (worst
peak->trough), and BREADTH -- the average number of distinct WAVES and of positions held >5% over the
trailing year, where each held ticker's wave is its curator-assigned wave_bucket (so the 4 geo sub-waves
count separately). Ranks by drawdown-aware return and by breadth. Writes data/curator_runs/_param_sweep.json.
"""
import glob
import json
import os
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402

CAPS = [0.333, 0.5, 0.667, 0.8, 1.0]
LAMS = [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
LBS = [14, 30, 60, 90]
MTS = [0.0, 0.05, 0.10, 0.15, 0.20]
MWS = [4, 6, 8, 12, 16, 20, 24]

_FM = portfolio.load_financial_model()
_RF = float(_FM["risk_free_rate"])
_TU = int(portfolio.load_backtest_config()["t_update_days"])
_ANC = _FM["always_include"]


def _wave_map(mws):
    """ticker -> curator wave_bucket, from proto-mwsN's curations (latest label wins)."""
    m = {}
    for f in sorted(glob.glob(str(ROOT / f"data/curator_runs/proto-mws{mws}/*-curation.json"))):
        for a in json.load(open(f)).get("adds", []):
            if a.get("ticker") and a.get("wave_bucket"):
                m[str(a["ticker"]).upper()] = a["wave_bucket"]
    return m


WAVE_MAPS = {mws: _wave_map(mws) for mws in MWS}


def run_one(cfg):
    mws, cap, lam, lb, mt = cfg
    tag = f"{mws}_{cap}_{lam}_{lb}_{mt}"
    try:
        portfolio.curator_backtest(
            runs_dir=str(ROOT / f"data/curator_runs/proto-mws{mws}"), out_dir=f"/tmp/_ps/{tag}",
            max_weight=cap, risk_aversion=lam, risk_free_rate=_RF, t_update_days=_TU, benchmarks=[],
            lookback_years_override=lb / 365.0, always_include=_ANC, min_trade_frac=mt)
        sn = pd.read_csv(f"/tmp/_ps/{tag}/snapshots.csv", parse_dates=["date"])
    except Exception:
        return None
    tot = sn.groupby("date")["total_value"].first().sort_index()
    if len(tot) < 2:
        return None
    peak = tot.cummax()
    gb = float(((tot - peak) / peak.replace(0, np.nan)).min())
    ret = float(tot.iloc[-1] / tot.iloc[0] - 1.0)
    cutoff = tot.index[-1] - pd.Timedelta(days=365)
    rt = tot[tot.index >= cutoff]
    ret1 = float(rt.iloc[-1] / rt.iloc[0] - 1.0) if len(rt) > 1 else float("nan")
    pk = rt.cummax()
    gb1 = float(((rt - pk) / pk.replace(0, np.nan)).min()) if len(rt) > 1 else float("nan")
    # breadth over the trailing year: avg # distinct waves and # positions held >5%
    wm = WAVE_MAPS[mws]
    r1 = sn[sn["date"] >= cutoff].copy()
    r1["w"] = r1["value"] / r1["total_value"].replace(0, np.nan)
    waves, names = [], []
    for _d, g in r1.groupby("date"):
        held = g[g["w"] > 0.05]
        names.append(int(len(held)))
        waves.append(len({wm.get(str(t).upper(), "general_markets") for t in held["ticker"]}))
    return {"mws": mws, "cap": cap, "lam": lam, "lb": lb, "mt": mt, "ret": ret, "gb": gb,
            "ret1": ret1, "gb1": gb1,
            "breadth_waves": float(np.mean(waves)) if waves else 0.0,
            "breadth_names": float(np.mean(names)) if names else 0.0}


def main():
    os.makedirs("/tmp/_ps", exist_ok=True)
    cfgs = [(m, c, l, lb, mt) for m in MWS for c in CAPS for l in LAMS for lb in LBS for mt in MTS]
    print(f"running {len(cfgs)} configs ...", file=sys.stderr)
    with Pool(processes=max(2, (os.cpu_count() or 4) - 2)) as pool:
        rows = [r for r in pool.map(run_one, cfgs, chunksize=8) if r]
    for r in rows:
        r["cal1"] = r["ret1"] / max(abs(r["gb1"]), 0.02)   # trailing-year return per unit round-trip
    (ROOT / "data/curator_runs/_param_sweep.json").write_text(json.dumps(rows, indent=1))
    print(f"DONE {len(rows)}/{len(cfgs)} configs -> data/curator_runs/_param_sweep.json", file=sys.stderr)

    def show(title, key, n=10):
        print(f"\n=== {title} ===", file=sys.stderr)
        for r in sorted(rows, key=lambda r: -r[key])[:n]:
            print(f"  mws{r['mws']} cap{r['cap']}/λ{r['lam']}/{r['lb']}d/mt{r['mt']}: "
                  f"1y {r['ret1']*100:+.0f}% (gb {r['gb1']*100:.0f}%) cal {r['cal1']:.1f} | "
                  f"breadth {r['breadth_waves']:.1f} waves / {r['breadth_names']:.1f} names | 3y {r['ret']*100:+.0f}%",
                  file=sys.stderr)
    show("Top by trailing-year return / |giveback| (drawdown-aware)", "cal1")
    show("Top by breadth (avg concurrent waves)", "breadth_waves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
