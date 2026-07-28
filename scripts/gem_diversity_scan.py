#!/usr/bin/env python3
"""Rank the LOW-CHURN frontier configs by how many chart-7 gems they profit from (diversity) and the
return that spread produces. Writes data/curator_runs/_gem_diversity.json for the Sweeps DB section 7.

Reuses the per-config snapshots left in /tmp/_gems by gems_scan.py; regenerates any missing one. The
frontier filter (REC_MAX_DD/L1/L2) is imported from build_sweep_dashboard so it always matches table 4.
"""
import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402
_spec = importlib.util.spec_from_file_location("bsd", str(ROOT / "scripts" / "build_sweep_dashboard.py"))
bsd = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bsd)

GEMS = [g["ticker"] for g in json.load(open(ROOT / "data/curator_runs/_gems.json"))[:20]]
# map each gem ticker to the investor-profile wave that drives it (for the wave breakdown in the table)
WAVE_OF = {
    "RKLB": "rockets", "ASTS": "rockets",
    "QUBT": "quantum", "IONQ": "quantum", "RGTI": "quantum",
    "SMR": "nuclear", "NNE": "nuclear", "OKLO": "nuclear", "LEU": "nuclear",
    "CEG": "nuclear", "URA": "nuclear", "URNM": "nuclear",
    "KTOS": "defense", "LMT": "defense", "AVAV": "defense",
    "GOOGL": "AI", "AMZN": "AI", "NVDA": "AI", "AAPL": "AI",
    "SERV": "robotics", "LLY": "aging/health",
}
MWSDIR = dict(bsd.MWS_SWEEP)  # mws -> run dir
ANCH = bsd.ANCHORS
rows = {(r["mws"], r["cap"], r["lam"], r["lb"], r.get("mt", bsd.CURRENT_MT)): r
        for r in json.load(open(ROOT / "data/curator_runs/_sweep_cache.json"))["rows"]}  # mt in key: keep all bands
front = [r for r in rows.values()
         if abs(r["dd"]) * 100 < bsd.REC_MAX_DD and r["l1"] < bsd.REC_MAX_L1 and r["l2"] < bsd.REC_MAX_L2]
print(f"frontier configs (dd<{bsd.REC_MAX_DD}/L1<{bsd.REC_MAX_L1}/L2<{bsd.REC_MAX_L2}): {len(front)}")

out = []
for r in front:
    tag = f"{r['mws']}_{r['cap']}_{r['lam']}_{r['lb']}_{r.get('mt', bsd.CURRENT_MT)}"  # mt = 4th frontier axis
    # snapshots left on disk by the earlier scans: gems_scan -> /tmp/_gems, λ-extend -> /tmp/_lam
    # Reuse snapshots left by the sweep dashboard (/tmp/_sweep, same tag) or the earlier gems/λ scans; the tag
    # includes mt, so these are the exact per-config replays already on disk -- no regeneration needed.
    snp = next((p for p in (Path(f"/tmp/_sweep/{tag}/snapshots.csv"), Path(f"/tmp/_gems/{tag}/snapshots.csv"),
                            Path(f"/tmp/_lam/{tag}/snapshots.csv")) if p.exists()), None)
    if snp is None:                           # regenerate if neither tmp is present
        portfolio.curator_backtest(runs_dir=MWSDIR[r["mws"]], out_dir=f"/tmp/_gems/{tag}",
                                   max_weight=r["cap"], risk_aversion=r["lam"],
                                   risk_free_rate=bsd._RF, t_update_days=bsd._TU, benchmarks=[],
                                   lookback_years_override=r["lb"] / 365.0, always_include=ANCH,
                                   min_trade_frac=r.get("mt", bsd.CURRENT_MT))
        snp = Path(f"/tmp/_gems/{tag}/snapshots.csv")
    sn = pd.read_csv(snp, parse_dates=["date"])
    tot = sn.groupby("date")["total_value"].first()
    per = {}
    for T in GEMS:
        g = sn[sn.ticker == T].sort_values("date")
        if len(g) < 2 or (g["shares"] > 0).sum() == 0:
            continue
        val = g.set_index("date")["value"]; pr = g.set_index("date")["price"]; tt = tot.reindex(val.index)
        w = (val / tt).values; rr = pr.values[1:] / pr.values[:-1] - 1
        c = float(np.nansum(w[:-1] * rr)) * 100
        if abs(c) >= 0.05:
            per[T] = round(c, 1)
    pos = {k: v for k, v in per.items() if v > 0}
    waves = {}                                # combined positive contribution per wave
    for t, v in pos.items():
        w = WAVE_OF.get(t, "other")
        waves[w] = round(waves.get(w, 0.0) + v, 1)
    _ts = tot.sort_index()[::5]     # equity curve, downsampled to ~weekly for the portfolio-value-over-time plot
    out.append({"mws": r["mws"], "cap": r["cap"], "lam": r["lam"], "lb": r["lb"],
                "mt": r.get("mt", bsd.CURRENT_MT), "pf": r.get("pf"), "gb": r.get("gb"), "ret": r["ret"],
                "dd": r["dd"], "l1": r["l1"], "l2": r["l2"], "n_pos": len(pos), "n_waves": len(waves),
                "gem_ret": round(sum(pos.values()), 1),
                "pos": dict(sorted(pos.items(), key=lambda x: -x[1])),
                "waves": dict(sorted(waves.items(), key=lambda x: -x[1])),
                "curve": {"x": [d.strftime("%Y-%m-%d") for d in _ts.index],
                          "y": [round(float(v), 2) for v in _ts.values]}})
# rank: most gems contributing positively, then highest total return
out.sort(key=lambda x: (-x["n_pos"], -x["ret"]))
json.dump(out[:20], open(ROOT / "data/curator_runs/_gem_diversity.json", "w"), indent=1)
print("wrote data/curator_runs/_gem_diversity.json (top 20)")
for x in out[:8]:
    print(f"  ws{x['mws']} cap{x['cap']} λ{x['lam']} {x['lb']}d mt{x['mt']:g} | ret{x['ret']*100:.0f}% dd{x['dd']*100:.0f}% "
          f"L1{x['l1']:.0f} L2{x['l2']:.0f} | {x['n_pos']} gems +{x['gem_ret']:.0f}pp | {x['pos']}")
