#!/usr/bin/env python3
"""Build docs/sweep_compounder_preview.html — an INTERIM zero-cost parameter-sweep view for the compounder
era, rendered from data/curator_runs/_param_sweep.json (the 5,600-config mws×cap×λ×lookback×min_trade grid
that sweep_params_proto.py replayed over the proto-mws{N} curations).

This is the compounder-era analog of the official BTS (docs/sweep_pwr.html). It is SEPARATE and INTERIM
because the proto curations were built on look-ahead-BIASED live ledes, so absolute returns are optimistic;
the cross-config RANKING is the robust part. After the clean Wayback re-curation, the official sweep_pwr.html
is rebuilt on clean mws20 curations and replaces this. No re-replay here: pure render of the cached grid.

Usage: python scripts/build_compounder_sweep_preview.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402
import dash_nav  # noqa: E402

SWEEP = ROOT / "data" / "curator_runs" / "_param_sweep.json"
OUT = ROOT / "docs" / "sweep_compounder_preview.html"


def main() -> int:
    rows = json.loads(SWEEP.read_text())
    # window length (years) from the proto run's starter, so annualized return carries no magic constant
    _st = json.loads((ROOT / "data/curator_runs/proto-mws20/_starter.json").read_text())
    _d0, _d1 = (datetime.strptime(_st[k], "%Y-%m-%d") for k in ("start_date", "end_date"))
    _yrs = max((_d1 - _d0).days / 365.25, 1e-9)
    for r in rows:
        r["cal1"] = r["ret1"] / max(abs(r["gb1"]), 0.02)   # trailing-year return per unit round-trip
        r["ann"] = (1.0 + r["ret"]) ** (1.0 / _yrs) - 1.0   # annualized (linear-plottable, matches canonical BTS)

    fm = portfolio.load_financial_model()
    CAN = (int(fm["max_watchlist_size"]), float(fm["concentration_cap"]), float(fm["risk_aversion"]),
           int(fm["optimizer_lookback_days"]), float(fm["min_trade_size_frac"]))
    anchors = [t.upper() for t in fm["always_include"]]

    def is_canon(r):
        return (r["mws"], r["cap"], r["lam"], r["lb"], r["mt"]) == CAN
    canon = next((r for r in rows if is_canon(r)), None)

    # ---- palette by watchlist size (matches the spirit of the official BTS mws coloring) ----
    MWS_COLORS = {4: "#17becf", 6: "#bcbd22", 8: "#2b8a3e", 12: "#e377c2", 16: "#ff7f0e",
                  20: "#1f77b4", 24: "#8c564b"}
    mws_present = sorted({r["mws"] for r in rows})

    def scatter(xfn, yfn, xtitle, ytitle, colorfn, colortitle, ylog=False, xpct=True, ypct=True):
        f = go.Figure()
        # color groups
        groups = sorted({colorfn(r) for r in rows})
        for g in groups:
            sub = [r for r in rows if colorfn(r) == g]
            col = MWS_COLORS.get(g, "#adb5bd") if colortitle.startswith("watchlist") else None
            f.add_trace(go.Scatter(
                x=[xfn(r) for r in sub], y=[yfn(r) for r in sub], mode="markers", name=str(g),
                marker={"size": 6, "opacity": 0.55, **({"color": col} if col else {})},
                text=[f"mws{r['mws']} · cap{r['cap']}/λ{r['lam']}/{r['lb']}d/mt{r['mt']}"
                      f"<br>3y {r['ret']*100:+.0f}% · 1y {r['ret1']*100:+.0f}% · gb1 {r['gb1']*100:.0f}%"
                      f"<br>breadth {r['breadth_waves']:.1f} waves / {r['breadth_names']:.1f} names" for r in sub],
                hovertemplate="%{text}<extra></extra>"))
        if canon is not None:   # star the canonical compounder on top
            f.add_trace(go.Scatter(
                x=[xfn(canon)], y=[yfn(canon)], mode="markers", name="canonical",
                marker={"symbol": "star", "size": 20, "color": "#111", "line": {"width": 1.5, "color": "#fff"}},
                text=[f"CANONICAL · mws{canon['mws']} cap{canon['cap']}/λ{canon['lam']}/{canon['lb']}d/mt{canon['mt']}"],
                hovertemplate="%{text}<extra></extra>"))
        f.update_layout(template="seaborn", height=460, margin={"t": 20, "l": 70, "r": 140},
                        xaxis={"title": xtitle, "tickformat": ".0%" if xpct else None},
                        yaxis={"title": ytitle, "tickformat": ".0%" if (ypct and not ylog) else None,
                               "type": "log" if ylog else "linear"},
                        legend={"title": {"text": colortitle}, "x": 1.02, "xanchor": "left", "y": 1})
        return f.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    # Plot 1: annualized return vs full max-giveback, colored by watchlist size (the return/drawdown frontier).
    p1 = scatter(lambda r: r["gb"], lambda r: r["ann"], "max giveback (full window) — risk →",
                 "annualized return →", lambda r: r["mws"], "watchlist<br>size", ylog=False, ypct=True)
    # Plot 2: trailing-year return vs trailing-year giveback, colored by cap (the drawdown-aware, recent slice).
    p2 = scatter(lambda r: r["gb1"], lambda r: r["ret1"], "trailing-year giveback — risk →",
                 "trailing-year return →", lambda r: r["cap"], "concentration<br>cap")
    # Plot 3: does breadth buy shallower drawdowns? avg concurrent waves vs trailing-year giveback, by cap.
    p3 = scatter(lambda r: r["breadth_waves"], lambda r: r["gb1"], "avg concurrent waves held (breadth) →",
                 "trailing-year giveback →", lambda r: r["cap"], "concentration<br>cap", xpct=False)

    # ---- ranking tables ----
    _lc = 'style="text-align:left"'

    def table(ranked, title, note):
        hdr = (f'<tr><th {_lc}>#</th><th {_lc}>mws</th><th {_lc}>cap</th><th {_lc}>λ</th><th {_lc}>lookback</th>'
               f'<th {_lc}>min_trade</th><th {_lc}>3y ret</th><th {_lc}>1y ret</th><th {_lc}>1y giveback</th>'
               f'<th {_lc}>cal (1y)</th><th {_lc}>breadth (waves/names)</th></tr>')
        body = ""
        for i, r in enumerate(ranked):
            hl = "background:#fff7e6;" if is_canon(r) else ""
            live = " &larr; canonical" if is_canon(r) else ""
            body += (f'<tr style="{hl}border-bottom:1px solid #eee;"><td {_lc}>{i+1}</td>'
                     f'<td {_lc}>{r["mws"]}</td><td {_lc}>{r["cap"]}</td><td {_lc}>{r["lam"]}</td>'
                     f'<td {_lc}>{r["lb"]}d</td><td {_lc}>{r["mt"]:g}{live}</td>'
                     f'<td {_lc}>{r["ret"]*100:+.0f}%</td><td {_lc}>{r["ret1"]*100:+.0f}%</td>'
                     f'<td {_lc}>{r["gb1"]*100:.0f}%</td><td {_lc}>{r["cal1"]:.1f}</td>'
                     f'<td {_lc}>{r["breadth_waves"]:.1f} / {r["breadth_names"]:.1f}</td></tr>')
        return (f'<h2>{title}</h2><p style="color:#555;max-width:900px;">{note}</p>'
                f'<table style="font-size:12.5px;"><thead>{hdr}</thead><tbody>{body}</tbody></table>')

    # compounder neighborhood only (wide mws, low cap, short lookback, low band) for the tables
    nb = [r for r in rows if r["mws"] in (12, 16, 20, 24) and r["cap"] in (0.333, 0.5)
          and r["lb"] in (14, 30) and r["mt"] in (0.0, 0.05)]
    t_dd = table(sorted(nb, key=lambda r: -r["cal1"])[:12], "Top 12 by drawdown-aware trailing year (cal = 1y ret / |1y giveback|)",
                 "The compounder neighborhood (mws 12-24, cap 0.333-0.5, lookback 14-30d, min_trade 0-0.05) "
                 "ranked by trailing-year return per unit round-trip. This is the corner the canonical sits in.")
    t_ret = table(sorted(nb, key=lambda r: -r["ret"])[:12], "Top 12 by 3-year return (compounding)",
                  "Same neighborhood ranked by raw 3-year return. Note the return-max corner (low λ) runs a "
                  "deeper giveback and less breadth than the drawdown-aware canonical.")

    can_txt = ("not present in grid" if canon is None else
               f"mws{CAN[0]} / cap{CAN[1]} / λ{CAN[2]} / {CAN[3]}d / mt{CAN[4]} &mdash; "
               f"3y {canon['ret']*100:+.0f}%, 1y {canon['ret1']*100:+.0f}%, 1y giveback {canon['gb1']*100:.0f}%, "
               f"breadth {canon['breadth_waves']:.1f} waves")

    nav = dash_nav.render("", built=False)   # the doc shell renders its own "dashboard built" stamp
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = ('<h1 style="margin:.2em 0;">Parameter sweep — compounder preview (interim)</h1>'
            f'<p style="color:#b45309;max-width:900px;background:#fffbeb;border:1px solid #fde68a;'
            'padding:.6em .8em;border-radius:6px;">'
            '<b>Interim, biased-lede.</b> This renders the 5,600-config zero-cost grid '
            '(mws×cap×λ×lookback×min_trade) replayed over the <code>proto-mws{N}</code> curations, which were '
            'built on look-ahead-<b>biased live ledes</b>. Absolute returns are OPTIMISTIC; the cross-config '
            '<b>ranking</b> is the robust signal. The official <a href="sweep_pwr.html">sweep_pwr.html</a> is '
            'rebuilt on clean Wayback curations after the ingest and replaces this.</p>'
            f'<p style="color:#555;max-width:900px;">Anchors {anchors}. Canonical (star): {can_txt}.</p>'
            '<h2>1. Return vs drawdown frontier (by watchlist size)</h2>'
            '<p style="color:#555;max-width:900px;">3-year return against worst full-window giveback. Up-left '
            'is better (more return, shallower drawdown). Colored by watchlist size.</p>' + p1
            + '<h2>2. Trailing-year return vs giveback (by cap)</h2>'
            '<p style="color:#555;max-width:900px;">The recent, forward-relevant slice (not dominated by the '
            '2023-24 run-up). Lower concentration_cap (darker) clusters at shallower giveback.</p>' + p2
            + '<h2>3. Does breadth buy shallower drawdowns?</h2>'
            '<p style="color:#555;max-width:900px;">Average number of concurrent waves held &gt;5% against '
            'trailing-year giveback. Low cap pushes breadth up and giveback toward zero &mdash; the compounder '
            'thesis in one plot (though the deepest-return configs still round-trip).</p>' + p3
            + t_dd + t_ret)
    # Wrap in the SAME document shell / font as the canonical BTS (docs/sweep_pwr.html) so the styling matches.
    page = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Parameter sweep — compounder preview</title>\n'
            '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;'
            'margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5}h1,h2{color:#111}'
            'table{border-collapse:collapse;font-size:13px;width:100%}'
            'th{text-align:right;padding:6px 10px;border-bottom:2px solid #ccc;white-space:nowrap}'
            'th:first-child{text-align:left}.built{position:absolute;top:8px;right:16px;font-size:12px;color:#888}'
            f'</style></head><body><div class="built">dashboard built {ts}</div>{nav}{body}</body></html>')
    OUT.write_text(page)
    print(f"wrote {OUT}  ({len(rows)} configs; canonical {'FOUND' if canon else 'MISSING'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
