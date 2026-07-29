#!/usr/bin/env python3
"""Build docs/sweep_compounder_preview.html — an INTERIM BTS for the compounder era.

The OLD BTS plot types (return-vs-drawdown / L1 / L2 clouds coloured by watchlist size, the recommended-
settings frontier table, and the equity-curve race) computed on the NEW 5,600-config gridsearch
(mws×cap×λ×lookback×min_trade over the proto-mws{N} curations). The metric MATH is the canonical BTS's own
functions (imported from build_sweep_dashboard) so the numbers match what sweep_pwr.html would show.

The compounder-specific / non-BTS plots live in a SEPARATE dashboard (build_compounder_diagnostics.py ->
compounder_diagnostics.html). No re-replay here: reads the enriched metric cache produced by
build_compounder_diagnostics.enrich() (or produces it), and reads per-config equity curves from /tmp/_ps.

INTERIM + biased-lede: absolute returns are optimistic; the cross-config RANKING is robust. After the clean
Wayback re-curation, the official sweep_pwr.html is rebuilt on clean mws20 and replaces this.

Usage: python scripts/build_compounder_sweep_preview.py [--recompute]
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src import portfolio  # noqa: E402
import build_sweep_dashboard as B  # noqa: E402  (reuse the canonical frontier thresholds)
import build_compounder_diagnostics as D  # noqa: E402  (shared enrich() + palette + shell)
import dash_nav  # noqa: E402

OUT = ROOT / "docs" / "sweep_compounder_preview.html"


def main() -> int:
    rows, spy_ret = D.enrich("--recompute" in sys.argv)
    CAN, anchors = D.canonical()
    is_can = lambda r: (r["mws"], r["cap"], r["lam"], r["lb"], r["mt"]) == CAN  # noqa: E731
    canon = next((r for r in rows if is_can(r)), None)
    tag_of = lambda r: f"{r['mws']}_{r['cap']}_{r['lam']}_{r['lb']}_{r['mt']}"  # noqa: E731
    mws_present = sorted({r["mws"] for r in rows})

    # ---------- BTS clouds: annualized return vs (drawdown | L1 | L2), coloured by watchlist size ----------
    def cloud(xfn, xtitle, xhover):
        f = go.Figure()
        for _m in mws_present:
            sub = [r for r in rows if r["mws"] == _m]
            f.add_trace(go.Scatter(
                x=[xfn(r) for r in sub], y=[r["ann"] * 100 for r in sub], mode="markers",
                name=f"{_m}" + (" ★" if _m == CAN[0] else ""),
                marker={"size": [15 if is_can(r) else 7 for r in sub], "color": D.MWS_COLORS.get(_m, "#adb5bd"),
                        "opacity": 0.7, "line": {"width": [3 if is_can(r) else 0 for r in sub], "color": "#e03131"}},
                text=[f"mws{r['mws']} · cap{r['cap']}/λ{r['lam']}/{r['lb']}d/mt{r['mt']}"
                      f"<br>IR {r['ir']:+.2f}, Calmar {r['calmar']:.2f}, Sharpe {r['sharpe']:.2f}"
                      + (" · CANONICAL" if is_can(r) else "") for r in sub],
                hovertemplate="%{text}<br>ann %{y:.0f}%, " + xhover + "<extra></extra>"))
        if canon is not None:
            f.add_trace(go.Scatter(x=[xfn(canon)], y=[canon["ann"] * 100], mode="markers", name="canonical",
                        marker={"symbol": "star", "size": 20, "color": "#111", "line": {"width": 1.5, "color": "#fff"}},
                        hovertemplate=f"CANONICAL mws{CAN[0]} cap{CAN[1]}/λ{CAN[2]}/{CAN[3]}d/mt{CAN[4]}<extra></extra>"))
        f.update_layout(template="seaborn", height=460, margin={"t": 20, "l": 62, "r": 140},
                        xaxis={"title": xtitle}, yaxis={"title": "annualized return %"},
                        legend={"title": {"text": "watchlist<br>size (★=canon)"}, "x": 1.02, "xanchor": "left", "y": 1})
        return f.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    p_dd = cloud(lambda r: abs(r["dd"]) * 100, "max drawdown (|%|) — risk →", "maxDD -%{x:.0f}%")
    p_l1 = cloud(lambda r: r["l1"], "L1 churn — annualized one-way turnover (%/yr) →", "L1 %{x:.0f}")
    p_l2 = cloud(lambda r: r["l2"], "L2 course-correction — weight-space path length/yr →", "L2 %{x:.0f}")

    # ---------- recommended-settings frontier (canonical BTS filters + columns) ----------
    _lc = 'style="text-align:left"'
    _MET = [("IR", "{:+.2f}", lambda r: r["ir"]), ("t-stat", "{:+.1f}", lambda r: r["tstat"]),
            ("Sharpe", "{:.2f}", lambda r: r["sharpe"]), ("Calmar", "{:.2f}", lambda r: r["calmar"]),
            ("ann", "{:+.0f}%", lambda r: r["ann"] * 100), ("maxDD", "{:.0f}%", lambda r: r["dd"] * 100)]
    passed = [r for r in rows if abs(r["dd"]) * 100 < B.REC_MAX_DD and r["l1"] < B.REC_MAX_L1 and r["l2"] < B.REC_MAX_L2]
    passed.sort(key=lambda r: -(r["ir"] if r["ir"] == r["ir"] else -9e9))
    colbest = {m: max(passed, key=lambda r, fn=fx: (fn(r) if fn(r) == fn(r) else -9e9)) for m, _, fx in _MET} if passed else {}

    def cells(r):
        s = ""
        for m, fmt, fx in _MET:
            v = fx(r); s += f'<td {_lc}>{fmt.format(v) if v == v else "n/a"}{" ★" if colbest.get(m) is r else ""}</td>'
        return s
    hdr = (f'<tr><th {_lc}>#</th><th {_lc}>mws</th><th {_lc}>cap</th><th {_lc}>λ</th><th {_lc}>lookback</th>'
           f'<th {_lc}>min_trade</th>' + "".join(f'<th {_lc}>{m}</th>' for m, _, _ in _MET)
           + f'<th {_lc}>L1</th><th {_lc}>L2</th></tr>')
    trs = ""
    for i, r in enumerate(passed[:40]):
        hl = "background:#fff7e6;" if is_can(r) else ""
        live = " &larr; canonical" if is_can(r) else ""
        trs += (f'<tr style="{hl}border-bottom:1px solid #eee;"><td {_lc}>{i+1}</td><td {_lc}>{r["mws"]}</td>'
                f'<td {_lc}>{r["cap"]}</td><td {_lc}>{r["lam"]}</td><td {_lc}>{r["lb"]}d</td>'
                f'<td {_lc}>{r["mt"]:g}{live}</td>{cells(r)}<td {_lc}>{r["l1"]:.0f}</td><td {_lc}>{r["l2"]:.0f}</td></tr>')
    can_pass = canon is not None and canon in passed
    rec_html = (
        f'<p style="color:#555;font-size:12px;max-width:940px;">Same frontier filter as the canonical BTS: keep only '
        f'<b>|maxDD| &lt; {B.REC_MAX_DD:.0f}% AND L1 &lt; {B.REC_MAX_L1:.0f} AND L2 &lt; {B.REC_MAX_L2:.0f}</b>, sorted by IR. '
        f'<b>{len(passed)} of {len(rows)}</b> configs survive. ★ = best in column among survivors. The canonical '
        + ("<b>passes</b> and is highlighted." if can_pass else "does <b>not</b> pass this churn/drawdown filter.")
        + ' Top 40 shown.</p>'
        f'<table style="font-size:12px;"><thead>{hdr}</thead><tbody>{trs}</tbody></table>')

    # ---------- equity-curve race: canonical + the frontier's top-IR configs ----------
    sel = ([canon] if canon else []) + [r for r in passed[:6] if not is_can(r)]
    ef = go.Figure()
    pal = ["#111", "#d97706", "#9467bd", "#d62728", "#8c564b", "#e377c2", "#17becf"]
    for i, r in enumerate(sel):
        sp = D.PS / tag_of(r) / "snapshots.csv"
        if not sp.exists():
            continue
        tot = pd.read_csv(sp, parse_dates=["date"]).groupby("date")["total_value"].first().sort_index()
        lab = ("CANONICAL " if is_can(r) else "") + f"mws{r['mws']} cap{r['cap']}/λ{r['lam']}/{r['lb']}d"
        ef.add_trace(go.Scatter(x=[d.strftime("%Y-%m-%d") for d in tot.index[::5]], y=[float(v) for v in tot.values[::5]],
                     mode="lines", name=lab, line={"color": pal[i % len(pal)], "width": 3 if is_can(r) else 1.8}))
    ef.update_layout(template="seaborn", height=460, margin={"t": 20, "l": 72, "r": 230}, hovermode="x unified",
                     legend={"x": 1.02, "xanchor": "left", "y": 1})
    ef.update_yaxes(title_text="portfolio value ($)", type="log",
                    tickvals=[10000, 30000, 100000, 300000, 1000000], ticktext=["$10K", "$30K", "$100K", "$300K", "$1M"])
    eq_html = ef.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})

    can_txt = ("not in grid" if canon is None else
               f"mws{CAN[0]} / cap{CAN[1]} / λ{CAN[2]} / {CAN[3]}d / mt{CAN[4]} &mdash; ann {canon['ann']*100:+.0f}%, "
               f"maxDD {canon['dd']*100:.0f}%, IR {canon['ir']:+.2f}, Calmar {canon['calmar']:.2f}")
    body = (
        '<h1 style="margin:.2em 0;">Backtest sweeps (BTS) — compounder preview (interim)</h1>'
        '<p style="color:#b45309;max-width:940px;background:#fffbeb;border:1px solid #fde68a;padding:.6em .8em;'
        'border-radius:6px;"><b>Interim, biased-lede.</b> The BTS plots computed with the canonical BTS metric '
        'functions on the 5,600-config compounder gridsearch (mws×cap×λ×lookback×min_trade over the '
        '<code>proto-mws{N}</code> curations, built on look-ahead-<b>biased live ledes</b>). Absolute returns are '
        'OPTIMISTIC; the cross-config <b>ranking</b> is robust. Compounder-specific plots are in the separate '
        '<a href="compounder_diagnostics.html">compounder diagnostics</a> dashboard. Official '
        '<a href="sweep_pwr.html">sweep_pwr.html</a> is rebuilt on clean Wayback curations after the ingest.</p>'
        f'<p style="color:#555;max-width:940px;">{len(rows)} configs. Anchors {anchors}. SPY returned '
        f'{spy_ret*100:+.0f}% over the window. Canonical (★): {can_txt}.</p>'
        '<h2>1. Return vs drawdown</h2><p style="color:#555;max-width:920px;">Annualized return vs max drawdown, '
        'one point per cap/λ/lookback/mt config, <b>coloured by watchlist size</b>. Upper-left is best.</p>' + p_dd
        + '<h2>2. Return vs L1 churn</h2><p style="color:#555;max-width:920px;">Annualized return vs annualized '
        'one-way turnover. Upper-left = high return, low trading.</p>' + p_l1
        + '<h2>3. Return vs L2 course-correction</h2><p style="color:#555;max-width:920px;">Return vs weight-space '
        'path length (emphasizes concentrated single-name rotations more than L1).</p>' + p_l2
        + '<h2>4. Recommended settings (frontier)</h2>' + rec_html
        + '<h2>5. Portfolio value over time — canonical + top frontier configs</h2>'
        '<p style="color:#555;max-width:920px;">Equity curves ($50K start, log axis) for the canonical (black) and '
        'the highest-IR frontier survivors.</p>' + eq_html)
    D.write_page(OUT, "Backtest sweeps — compounder preview", body)
    print(f"wrote {OUT}  ({len(rows)} configs; canonical {'FOUND' if canon else 'MISSING'}; {len(passed)} survivors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
