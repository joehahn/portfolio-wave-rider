#!/usr/bin/env python3
"""Build docs/compounder_diagnostics.html — the NON-BTS compounder plots.

Companion to build_compounder_sweep_preview.py (the BTS-style page). This holds the compounder-specific
diagnostics the standard BTS does not show: breadth vs drawdown, breadth vs return, giveback vs cap, the
trailing-year frontier, and two time series for the canonical config (concurrent breadth over time, and
anchor/cash weight over time). Also the SHARED helpers (enrich / canonical / write_page / palette) that the
BTS-page script imports, so the heavy metric enrichment lives in one place.

INTERIM + biased-lede (proto-mws{N} curations); the cross-config ranking is the robust part.

Usage: python scripts/build_compounder_diagnostics.py [--recompute]
"""
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src import portfolio  # noqa: E402
import build_sweep_dashboard as B  # noqa: E402  (canonical metric math)
import dash_nav  # noqa: E402

SWEEP = ROOT / "data" / "curator_runs" / "_param_sweep.json"
ENRICHED = ROOT / "data" / "curator_runs" / "_param_sweep_enriched.json"
PS = Path("/tmp/_ps")
OUT = ROOT / "docs" / "compounder_diagnostics.html"
MWS_COLORS = {4: "#17becf", 6: "#bcbd22", 8: "#2b8a3e", 12: "#e377c2", 16: "#ff7f0e", 20: "#1f77b4", 24: "#8c564b"}
ANCHOR_COLORS = {"SPY": "#1f77b4", "AGG": "#2b8a3e", "IAU": "#d4a017", "BIL": "#c92a2a"}


def canonical():
    """(canonical (mws,cap,lam,lb,mt) tuple, anchor list) from the live profile."""
    fm = portfolio.load_financial_model()
    return ((int(fm["max_watchlist_size"]), float(fm["concentration_cap"]), float(fm["risk_aversion"]),
             int(fm["optimizer_lookback_days"]), float(fm["min_trade_size_frac"])),
            [t.upper() for t in fm["always_include"]])


def enrich(recompute: bool = False):
    """Per-config full metric set from the cached /tmp/_ps snapshots, reusing the canonical BTS functions.
    Returns (rows, spy_total_return). Cached to ENRICHED so /tmp cleanup doesn't force a recompute."""
    grid = json.loads(SWEEP.read_text())
    if not recompute and ENRICHED.exists():
        c = json.loads(ENRICHED.read_text())
        if c.get("n") == len(grid):
            print(f"  loaded {len(c['rows'])} enriched configs from cache")
            return c["rows"], c["spy_ret"]
    spy, rows = None, []
    for i, g in enumerate(grid):
        sp = PS / f"{g['mws']}_{g['cap']}_{g['lam']}_{g['lb']}_{g['mt']}" / "snapshots.csv"
        if not sp.exists():
            continue
        snaps = pd.read_csv(sp, parse_dates=["date"])
        tot = snaps.groupby("date")["total_value"].first().sort_index()
        if len(tot) < 3:
            continue
        if spy is None:
            spy = portfolio._fetch_benchmark_curves(["SPY"], tot.index[0], tot.index[-1], float(tot.iloc[0]))["SPY"]
        ret = float(tot.iloc[-1] / tot.iloc[0] - 1.0)
        peak = tot.cummax(); dd = float(((tot - peak) / peak).min())
        yrs = max((tot.index[-1] - tot.index[0]).days / 365.25, 1e-9)
        ann = (1.0 + ret) ** (1.0 / yrs) - 1.0
        rows.append({**{k: g[k] for k in ("mws", "cap", "lam", "lb", "mt", "breadth_waves", "breadth_names")},
                     "ret": ret, "ann": ann, "dd": dd, **dict(zip(("l1", "l2"), B._churn_metrics(snaps))),
                     **B._metrics(tot, spy, ann, dd), **B._rotation_metrics(snaps)})
        if (i + 1) % 800 == 0:
            print(f"  enriched {i + 1}/{len(grid)}", file=sys.stderr)
    spy_ret = float(spy.iloc[-1] / spy.iloc[0] - 1.0) if spy is not None else float("nan")
    ENRICHED.write_text(json.dumps({"n": len(grid), "spy_ret": spy_ret, "rows": rows}))
    print(f"  enriched {len(rows)} configs -> cached")
    return rows, spy_ret


def write_page(out_path: Path, title: str, body_html: str) -> None:
    """Wrap body in the same document shell / font as the canonical BTS (sweep_pwr.html)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    nav = dash_nav.render("", built=False)
    out_path.write_text(dash_nav.stamp(
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>\n'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;'
        'margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5}h1,h2{color:#111}'
        'table{border-collapse:collapse;font-size:13px;width:100%}'
        'th{text-align:right;padding:6px 10px;border-bottom:2px solid #ccc;white-space:nowrap}'
        'th:first-child{text-align:left}.built{position:absolute;top:8px;right:16px;font-size:12px;color:#888}'
        f'</style></head><body><div class="built">dashboard built {ts}</div>{nav}{body_html}</body></html>'))


def _wave_map():
    """ticker -> curator wave_bucket, from proto-mws20's curations (latest label wins)."""
    m = {}
    for f in sorted(glob.glob(str(ROOT / "data/curator_runs/proto-mws20/*-curation.json"))):
        for a in json.load(open(f)).get("adds", []):
            if a.get("ticker") and a.get("wave_bucket"):
                m[str(a["ticker"]).upper()] = a["wave_bucket"]
    return m


def main() -> int:
    rows, spy_ret = enrich("--recompute" in sys.argv)
    CAN, anchors = canonical()
    is_can = lambda r: (r["mws"], r["cap"], r["lam"], r["lb"], r["mt"]) == CAN  # noqa: E731
    canon = next((r for r in rows if is_can(r)), None)
    caps = sorted({r["cap"] for r in rows})

    # ---------- cap-coloured scatter helper ----------
    def by_cap(xfn, yfn, xtitle, ytitle, xpct, ypct, h=440):
        f = go.Figure()
        for c in caps:
            sub = [r for r in rows if r["cap"] == c]
            f.add_trace(go.Scatter(x=[xfn(r) for r in sub], y=[yfn(r) for r in sub], mode="markers", name=f"{c}",
                        marker={"size": 6, "opacity": 0.55},
                        text=[f"mws{r['mws']} cap{r['cap']}/λ{r['lam']}/{r['lb']}d/mt{r['mt']}"
                              f"<br>ann {r['ann']*100:+.0f}% · breadth {r['breadth_waves']:.1f}w" for r in sub],
                        hovertemplate="%{text}<extra></extra>"))
        if canon is not None:
            f.add_trace(go.Scatter(x=[xfn(canon)], y=[yfn(canon)], mode="markers", name="canonical",
                        marker={"symbol": "star", "size": 20, "color": "#111", "line": {"width": 1.5, "color": "#fff"}},
                        hovertemplate="CANONICAL<extra></extra>"))
        f.update_layout(template="seaborn", height=h, margin={"t": 20, "l": 66, "r": 120},
                        xaxis={"title": xtitle, "tickformat": ".0%" if xpct else None},
                        yaxis={"title": ytitle, "tickformat": ".0%" if ypct else None},
                        legend={"title": {"text": "cap"}, "x": 1.02, "xanchor": "left", "y": 1})
        return f.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    p_bgb = by_cap(lambda r: r["breadth_waves"], lambda r: r["gb_1y"], "avg concurrent waves held (breadth) →",
                   "trailing-year giveback →", False, True)
    p_bret = by_cap(lambda r: r["breadth_waves"], lambda r: r["ann"], "avg concurrent waves held (breadth) →",
                    "annualized return →", False, True)
    p_gbcap = by_cap(lambda r: r["cap"], lambda r: r["gb"], "concentration_cap →", "full-window giveback →", False, True)
    p_tyf = by_cap(lambda r: r["gb_1y"], lambda r: r["ret_1y"], "trailing-year giveback — risk →",
                   "trailing-year return →", True, True)

    # ---------- canonical time series: breadth + anchor weight over the window ----------
    ts_breadth = ts_anchor = '<p style="color:#999;">canonical snapshot not found in /tmp/_ps</p>'
    csp = PS / f"{CAN[0]}_{CAN[1]}_{CAN[2]}_{CAN[3]}_{CAN[4]}" / "snapshots.csv"
    if csp.exists():
        sn = pd.read_csv(csp, parse_dates=["date"])
        sn["w"] = sn["value"] / sn["total_value"].replace(0, np.nan)
        wm = _wave_map()
        held = sn[sn["w"] > 0.05]
        nwave = held.groupby("date")["ticker"].apply(lambda s: len({wm.get(str(t).upper(), "general_markets") for t in s}))
        nname = held.groupby("date")["ticker"].nunique()
        fb = go.Figure()
        fb.add_trace(go.Scatter(x=[d.strftime("%Y-%m-%d") for d in nname.index], y=list(nname.values),
                     mode="lines", name="names held >5%", line={"color": "#1f77b4", "width": 1.8}))
        fb.add_trace(go.Scatter(x=[d.strftime("%Y-%m-%d") for d in nwave.index], y=list(nwave.values),
                     mode="lines", name="distinct waves held >5%", line={"color": "#d97706", "width": 2.2}))
        fb.update_layout(template="seaborn", height=380, margin={"t": 20, "l": 55, "r": 160}, hovermode="x unified",
                         yaxis={"title": "count"}, legend={"x": 1.02, "xanchor": "left", "y": 1})
        ts_breadth = fb.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
        # anchor weight over time (does the optimizer ever fund cash / go defensive?)
        fa = go.Figure()
        for a in anchors:
            aw = sn[sn["ticker"] == a].set_index("date")["w"].reindex(nname.index).fillna(0.0)
            fa.add_trace(go.Scatter(x=[d.strftime("%Y-%m-%d") for d in aw.index], y=[v * 100 for v in aw.values],
                         mode="lines", name=a, line={"color": ANCHOR_COLORS.get(a, "#888"), "width": 1.8}))
        fa.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 55, "r": 140}, hovermode="x unified",
                         yaxis={"title": "anchor weight (%)"}, legend={"x": 1.02, "xanchor": "left", "y": 1})
        ts_anchor = fa.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})

    body = (
        '<h1 style="margin:.2em 0;">Compounder diagnostics — non-BTS plots (interim)</h1>'
        '<p style="color:#b45309;max-width:940px;background:#fffbeb;border:1px solid #fde68a;padding:.6em .8em;'
        'border-radius:6px;"><b>Interim, biased-lede.</b> Compounder-specific views the standard BTS does not '
        'show, on the 5,600-config gridsearch (proto-mws{N} curations, look-ahead-biased live ledes). The '
        'standard risk/return/churn plots are on the <a href="sweep_compounder_preview.html">BTS page</a>.</p>'
        f'<p style="color:#555;max-width:940px;">Anchors {anchors}. Canonical (★): mws{CAN[0]} / cap{CAN[1]} / '
        f'λ{CAN[2]} / {CAN[3]}d / mt{CAN[4]}.</p>'
        '<h2>1. Does breadth buy shallower drawdowns?</h2><p style="color:#555;max-width:920px;">Avg concurrent '
        'waves held &gt;5% vs trailing-year giveback, <b>coloured by cap</b>. Lower cap forces breadth up and '
        'pushes the giveback toward zero &mdash; the compounder thesis in one plot.</p>' + p_bgb
        + '<h2>2. Is breadth free? Breadth vs return</h2><p style="color:#555;max-width:920px;">Same x-axis '
        'against annualized return. If the cloud is flat, spreading across waves costs little return; if it '
        'slopes down, breadth trades return for safety.</p>' + p_bret
        + '<h2>3. Giveback vs concentration_cap</h2><p style="color:#555;max-width:920px;">The cap&rarr;drawdown '
        'lever directly: lower cap forces diversification and shallower full-window givebacks.</p>' + p_gbcap
        + '<h2>4. Trailing-year return vs giveback</h2><p style="color:#555;max-width:920px;">The recent, '
        'forward-relevant slice (not swamped by the 2023-24 run-up), coloured by cap.</p>' + p_tyf
        + '<h2>5. Canonical: concurrent breadth over time</h2><p style="color:#555;max-width:920px;">How many '
        'distinct waves and names the canonical config actually held &gt;5% through the window.</p>' + ts_breadth
        + '<h2>6. Canonical: anchor / cash weight over time</h2><p style="color:#555;max-width:920px;">Weight the '
        'optimizer put in each safe-haven anchor. Flat-at-zero means it never went defensive on this window '
        '(the drawdowns here are rotational, not broad risk-off &mdash; why BIL stays near 0).</p>' + ts_anchor)
    write_page(OUT, "Compounder diagnostics", body)
    print(f"wrote {OUT}  ({len(rows)} configs; canonical {'FOUND' if canon else 'MISSING'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
