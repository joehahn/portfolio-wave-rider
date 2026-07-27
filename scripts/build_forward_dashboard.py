#!/usr/bin/env python3
"""FORWARD DASHBOARD (prototype) -> docs/forward_dashboard.html.

A FIRST-STAB synthesis of the live portfolio page (docs/index.html) and the Curator Bootstrap
(CBS) dashboard, intended to grow into this project's production forward-use dashboard. It shows
the user's REAL forward portfolio -- value over time vs SPY, current allocation by wave, and the
live curator's decision log with news -- framed with CBS-style summary cards.

Reads real live data only (no re-curation, no optimizer -- a pure render):
  - data/snapshots.csv               real $ value per ticker per day
  - data/thesis_baseline.json        inception date (chart anchor)
  - data/curator_runs/live/*.json    live curator decisions (adds/removes + rationale + news)

This is a prototype: deeper CBS analytics (allocation-over-time, per-article / per-author
attribution, buy-and-hold baseline) are deliberately left for the next iteration.
"""
import argparse
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import dash_nav  # noqa: E402
from src import portfolio  # noqa: E402

# A small wave -> color palette so the allocation bars read by thesis wave, not by ticker.
WAVE_COLORS = {
    "ai": "#4C78A8", "quantum": "#72B7B2", "space": "#9D755D", "nuclear": "#E45756",
    "robotics": "#F58518", "defense": "#54A24B", "geopolitical": "#B279A2",
    "healthcare": "#FF9DA6", "aging": "#FF9DA6", "general_markets": "#BAB0AC",
}


def _card(label, value, sub="", color="#111"):
    return (f'<div style="display:inline-block;min-width:150px;margin:0 .6em .7em 0;padding:.7em 1em;'
            f'border:1px solid #e5e5e5;border-radius:8px;background:#fafafa;vertical-align:top">'
            f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.04em">{label}</div>'
            f'<div style="font-size:22px;font-weight:600;color:{color};margin-top:.15em">{value}</div>'
            f'<div style="font-size:12px;color:#777;margin-top:.1em">{sub}</div></div>')


def _sign_color(x):
    return "#2b8a3e" if x >= 0 else "#c92a2a"


def main(out_path: str) -> int:
    import plotly.graph_objects as go

    snaps = pd.read_csv(ROOT / "data" / "snapshots.csv", parse_dates=["date"])
    tv = snaps.groupby("date")["value"].sum().sort_index()          # real portfolio $ over time
    if len(tv) < 2:
        print("not enough snapshots to render the forward dashboard", file=sys.stderr)
        return 1
    d0, d1 = tv.index[0], tv.index[-1]
    start_val, cur_val = float(tv.iloc[0]), float(tv.iloc[-1])
    port_ret = (cur_val / start_val - 1) * 100

    # SPY benchmark, aligned to the snapshot dates and normalized to the same starting dollars.
    spy_norm = spy_ret = None
    try:
        px = portfolio.fetch_prices(["SPY"], period="1y")
        spy = (px["SPY"] if "SPY" in getattr(px, "columns", []) else px.squeeze()).dropna()
        spy.index = pd.to_datetime(spy.index)
        spy_al = spy.reindex(tv.index, method="ffill")
        spy_norm = spy_al / spy_al.iloc[0] * start_val
        spy_ret = (spy_al.iloc[-1] / spy_al.iloc[0] - 1) * 100
    except Exception as e:  # noqa: BLE001 -- benchmark is optional; portfolio still renders
        print(f"SPY overlay skipped: {e}", file=sys.stderr)

    # Inception anchor (thesis baseline date) for the chart marker.
    inception = None
    try:
        inception = json.loads((ROOT / "data" / "thesis_baseline.json").read_text()).get("date")
    except Exception:  # noqa: BLE001
        pass

    # ---- summary cards (real portfolio) ----
    alpha_sub = f"SPY {spy_ret:+.1f}%" if spy_ret is not None else "SPY n/a"
    latest = snaps[snaps["date"] == snaps["date"].max()]
    held = latest[latest["value"] > 0]
    cards = "".join([
        _card("Portfolio value", f"${cur_val:,.0f}", f"as of {d1.date()}"),
        _card("Total return", f"{port_ret:+.1f}%", f"${cur_val - start_val:+,.0f} since {d0.date()}",
              _sign_color(port_ret)),
        _card("vs SPY", f"{port_ret - spy_ret:+.1f} pp" if spy_ret is not None else "n/a", alpha_sub,
              _sign_color(port_ret - spy_ret) if spy_ret is not None else "#111"),
        _card("Holdings", f"{held['ticker'].nunique()}", "tickers with a position"),
        _card("Since inception", inception or str(d0.date()), "thesis set"),
    ])

    # ---- chart 1: portfolio value vs SPY ----
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(tv.index), y=list(tv.values), name="Portfolio",
                             line=dict(color="#1f6fb2", width=2.5)))
    if spy_norm is not None:
        fig.add_trace(go.Scatter(x=list(spy_norm.index), y=list(spy_norm.values), name="SPY (norm.)",
                                 line=dict(color="#999", width=1.5, dash="dash")))
    if inception:
        try:
            fig.add_vline(x=pd.Timestamp(inception), line=dict(color="#bbb", width=1, dash="dot"))
        except Exception:  # noqa: BLE001
            pass
    fig.update_layout(height=430, margin=dict(l=60, r=20, t=10, b=40),
                      yaxis_title="Portfolio value ($)", xaxis_title="",
                      legend=dict(orientation="h", y=1.08, x=0), plot_bgcolor="white",
                      font=dict(size=13))
    fig.update_yaxes(gridcolor="#eee", tickprefix="$", separatethousands=True)
    fig.update_xaxes(gridcolor="#f4f4f4")

    # ---- chart 2: current allocation by wave ----
    wave_map = portfolio._effective_ticker_wave()
    alloc = held.groupby("ticker")["value"].sum().sort_values(ascending=False)
    tks = list(alloc.index)
    waves = [wave_map.get(t, "general_markets") for t in tks]
    colors = [WAVE_COLORS.get(w, "#BAB0AC") for w in waves]
    total = alloc.sum()
    fig2 = go.Figure(go.Bar(
        x=tks, y=list(alloc.values), marker_color=colors,
        text=[f"{v / total * 100:.0f}%" for v in alloc.values], textposition="outside",
        customdata=waves, hovertemplate="%{x} (%{customdata})<br>$%{y:,.0f}<extra></extra>"))
    fig2.update_layout(height=340, margin=dict(l=60, r=20, t=10, b=40),
                       yaxis_title="Position value ($)", plot_bgcolor="white", font=dict(size=13),
                       showlegend=False)
    fig2.update_yaxes(gridcolor="#eee", tickprefix="$", separatethousands=True)

    # ---- curation log (real live decisions, newest first) ----
    def _evidence(ev):
        out = []
        for e in ev or []:
            if isinstance(e, dict):
                url, summ = e.get("url", ""), e.get("summary", "")
            else:
                url, summ = str(e), ""
            if url:
                host = url.split("/")[2] if "://" in url else url
                out.append(f'<a href="{url}" style="color:#1f6fb2;text-decoration:none">{summ or host}</a>')
        return " &middot; ".join(out)

    log_blocks = []
    for f in sorted(glob.glob(str(ROOT / "data" / "curator_runs" / "live" / "2*-curation.json")), reverse=True):
        d = json.loads(Path(f).read_text())
        rows = []
        for a in d.get("adds", []):
            rows.append(
                f'<div style="margin:.35em 0"><span style="color:#2b8a3e;font-weight:600">+ {a.get("ticker")}</span> '
                f'<span style="color:#999;font-size:12px">[{a.get("wave_bucket", "")}]</span><br>'
                f'<span style="color:#444;font-size:13px">{a.get("rationale", "")}</span>'
                + (f'<br><span style="font-size:12px">{_evidence(a.get("news_evidence"))}</span>'
                   if a.get("news_evidence") else "") + "</div>")
        for r in d.get("removes", []):
            rows.append(
                f'<div style="margin:.35em 0"><span style="color:#c92a2a;font-weight:600">&minus; {r.get("ticker")}</span> '
                f'<span style="color:#444;font-size:13px">{r.get("rationale", "")}</span></div>')
        if not rows:
            rows.append('<div style="color:#999;font-size:13px">no changes</div>')
        log_blocks.append(
            f'<div style="border-left:3px solid #e5e5e5;padding:.2em 0 .2em 1em;margin:.8em 0">'
            f'<div style="font-weight:600;color:#111">{d.get("as_of_date")}</div>' + "".join(rows) + "</div>")
    curation_html = ("".join(log_blocks) if log_blocks
                     else '<p style="color:#999">No live curator decisions logged yet.</p>')

    ch1 = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})
    ch2 = fig2.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Forwardtest Dashboard</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5}h1,h2{color:#111}'
        '.built{position:absolute;top:8px;right:16px;font-size:12px;color:#888}</style></head><body>'
        f'<div class="built">dashboard built {ts}</div>'
        + dash_nav.render("forward_dashboard.html", built=False)
        + '<h1>Forwardtest Dashboard</h1>'
        f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{d0.date()} to {d1.date()} '
        '&middot; real forward portfolio (prototype)</p>'
        '<p style="color:#555;max-width:900px">The live curated portfolio run forward on real money: value '
        'vs SPY, current allocation by thesis wave, and the curator&#39;s decision log. A first-stab merge of '
        'the live portfolio page and the Curator Bootstrap (CBS) analytics &mdash; a prototype of the eventual '
        'production forward-use dashboard.</p>'
        f'<div style="margin:1em 0 1.5em">{cards}</div>'
        '<h2>Portfolio value over time</h2>'
        '<p style="color:#666;font-size:13px;max-width:900px">Real portfolio dollars (blue) vs SPY normalized to '
        'the same start (grey dashed). Dotted line marks the thesis inception.</p>'
        + ch1
        + '<h2>Current allocation by wave</h2>'
        '<p style="color:#666;font-size:13px;max-width:900px">Latest snapshot, each position colored by its thesis '
        'wave; labels show portfolio share.</p>'
        + ch2
        + '<h2>Curation log</h2>'
        '<p style="color:#666;font-size:13px;max-width:900px">Every live curator decision, newest first, with the '
        'rationale and the news it cited.</p>'
        + curation_html
        + '</body></html>')

    out = Path(out_path)
    out.write_text(page)
    print(f"wrote {out}  (port {port_ret:+.1f}% vs SPY {spy_ret:+.1f}%)" if spy_ret is not None
          else f"wrote {out}  (port {port_ret:+.1f}%)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the PWR Forward Dashboard (prototype).")
    ap.add_argument("--out", default=str(ROOT / "docs" / "forward_dashboard.html"))
    sys.exit(main(ap.parse_args().out))
