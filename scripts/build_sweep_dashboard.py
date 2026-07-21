#!/usr/bin/env python3
"""Build docs/sweep_pwr.html — the zero-cost PARAMETER SWEEP dashboard.

The optimizer knobs (concentration_cap, risk_aversion lambda, optimizer_lookback) only touch the
mean-variance REPLAY, not the curator, so we can grid them over the FIXED 2-year curation set for $0
(no LLM). Each config is scored on risk-adjusted, benchmark-relative metrics; the ranking is
deterministic, the t-stat tests significance, a block-bootstrap gives an error bar, and an H1/H2 split
flags whether the winner is a period artifact. Every row is an IN-SAMPLE hypothesis to forward-test.

Usage: python scripts/build_sweep_dashboard.py [--runs-dir data/curator_runs/gkg-2yr-weekly]
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402

CAPS = [0.5, 0.8, 0.9, 1.0]
LAMBDAS = [0.5, 1.0, 2.0]
LOOKBACKS = [30, 60, 90]          # calendar days
ANCHORS = ["SPY", "AGG", "IAU"]
CURRENT = (0.8, 2.0, 30)          # the live investor_profile.md config
BLUE, GREEN, RED, GREY = "#1f77b4", "#2b8a3e", "#c92a2a", "#adb5bd"


def _metrics(totals: pd.Series, spy: pd.Series, ann_ret: float, max_dd: float) -> dict:
    """Risk-adjusted, benchmark-relative metrics from a config's equity curve + the SPY curve."""
    days = max((totals.index[-1] - totals.index[0]).days, 1)
    calmar = ann_ret / abs(max_dd) if max_dd < 0 else float("nan")
    r = totals.pct_change().dropna()
    s = spy.reindex(totals.index).ffill().pct_change().reindex(r.index)
    act = (r - s).dropna()
    ir = tstat = float("nan")
    if len(act) > 2 and act.std() > 0:
        ppy = len(act) / (days / 365.25)
        ir = act.mean() / act.std() * np.sqrt(ppy)
        tstat = act.mean() / act.std() * np.sqrt(len(act))
    spy_ann = (spy.iloc[-1] / spy.iloc[0]) ** (365.25 / days) - 1.0
    alpha = ann_ret - spy_ann
    # rolling ~6-month (126 trading-day) hit-rate vs SPY
    win = min(126, max(20, len(totals) // 4))
    tv, sv = totals.values, spy.reindex(totals.index).ffill().values
    wins = tot = 0
    for i in range(len(tv) - win):
        if tv[i] > 0 and sv[i] > 0:
            tot += 1
            wins += (tv[i + win] / tv[i]) > (sv[i + win] / sv[i])
    hit = wins / tot if tot else float("nan")
    # block-bootstrap CI on annualized return (block=10 days, 400 resamples)
    rr = totals.pct_change().dropna().values
    rng = np.random.default_rng(7)
    blk, n = 10, len(rr)
    anns = []
    if n > blk:
        nb = n // blk
        for _ in range(400):
            idx = rng.integers(0, n - blk, nb)
            path = np.concatenate([rr[j:j + blk] for j in idx])
            g = float(np.prod(1 + path))
            anns.append(g ** (365.25 / days) - 1.0)
    ci = (float(np.percentile(anns, 5)), float(np.percentile(anns, 95))) if anns else (float("nan"), float("nan"))
    # H1 vs H2 IR (stability)
    mid = totals.index[len(totals) // 2]

    def _ir_half(lo, hi):
        a = act[(act.index > lo) & (act.index <= hi)]
        return (a.mean() / a.std() * np.sqrt(len(a) / (max((hi - lo).days, 1) / 365.25))) if len(a) > 5 and a.std() > 0 else float("nan")
    ir_h1 = _ir_half(totals.index[0], mid)
    ir_h2 = _ir_half(mid, totals.index[-1])
    return {"calmar": calmar, "ir": ir, "tstat": tstat, "alpha": alpha, "hit": hit,
            "ci_lo": ci[0], "ci_hi": ci[1], "ir_h1": ir_h1, "ir_h2": ir_h2}


def build(runs_dir: str, out: Path) -> None:
    rows, spy_curve = [], None
    for cap in CAPS:
        for lam in LAMBDAS:
            for lb in LOOKBACKS:
                res = portfolio.curator_backtest(
                    runs_dir=runs_dir, out_dir=f"/tmp/_sweep/{cap}_{lam}_{lb}",
                    max_weight=cap, risk_aversion=lam, benchmarks=["SPY"],
                    lookback_years_override=lb / 365.0, always_include=ANCHORS)
                snaps = pd.read_csv(Path(f"/tmp/_sweep/{cap}_{lam}_{lb}") / "snapshots.csv", parse_dates=["date"])
                totals = snaps.groupby("date")["total_value"].first().sort_index()
                if spy_curve is None:
                    spy_curve = portfolio._fetch_benchmark_curves(["SPY"], totals.index[0], totals.index[-1],
                                                                  float(totals.iloc[0]))["SPY"]
                m = _metrics(totals, spy_curve, res["annualized_return"], res["max_drawdown"])
                rows.append({"cap": cap, "lam": lam, "lb": lb, "ret": res["realized_return"],
                             "ann": res["annualized_return"], "dd": res["max_drawdown"], **m,
                             "cur": (cap, lam, lb) == CURRENT})
    spy_ret = float(spy_curve.iloc[-1] / spy_curve.iloc[0] - 1.0)

    # best per metric (higher is better except dd where less-negative is better)
    best = {k: max(rows, key=lambda r: (r[k] if r[k] == r[k] else -9e9))
            for k in ("ir", "calmar", "alpha", "ann", "hit")}
    best["dd"] = max(rows, key=lambda r: r["dd"])   # least-negative drawdown

    def star(r, k):
        return " ★" if best.get(k) is r else ""

    def td(v, fmt, cls="", extra=""):
        return f"<td style='padding:5px 10px;text-align:right;{cls}'>{extra}{fmt.format(v) if v == v else 'n/a'}</td>"
    trs = ""
    for r in sorted(rows, key=lambda r: -(r["ir"] if r["ir"] == r["ir"] else -9e9)):
        bg = "background:#fff7e6;" if r["cur"] else ""
        cfg = f"{r['cap']:.2f} / {r['lam']:.1f} / {r['lb']}d" + (" ← current" if r["cur"] else "")
        stable = "yes" if (r["ir_h1"] == r["ir_h1"] and r["ir_h2"] == r["ir_h2"] and r["ir_h1"] > 0 and r["ir_h2"] > 0) else "no"
        trs += (
            f"<tr style='{bg}border-bottom:1px solid #eee;'>"
            f"<td style='padding:5px 10px;white-space:nowrap;'>{cfg}</td>"
            f"{td(r['ir'], '{:+.2f}', 'font-weight:600;', star(r,'ir'))}"
            f"{td(r['tstat'], '{:+.1f}')}"
            f"{td(r['calmar'], '{:.2f}', '', star(r,'calmar'))}"
            f"{td(r['alpha']*100, '{:+.0f}%', '', star(r,'alpha'))}"
            f"{td(r['ann']*100, '{:+.0f}%', '', star(r,'ann'))}"
            f"{td(r['dd']*100, '{:.0f}%', '', star(r,'dd'))}"
            f"{td(r['ret']*100, '{:+.0f}%')}"
            f"{td(r['hit']*100, '{:.0f}%', '', star(r,'hit'))}"
            f"<td style='padding:5px 10px;text-align:right;color:#888;'>[{r['ci_lo']*100:+.0f}, {r['ci_hi']*100:+.0f}]%</td>"
            f"<td style='padding:5px 10px;text-align:center;color:{GREEN if stable=='yes' else RED};'>{stable}</td>"
            "</tr>")

    # frontier scatter: ann return vs |max drawdown|, colored by IR, current config ringed
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[abs(r["dd"]) * 100 for r in rows], y=[r["ann"] * 100 for r in rows], mode="markers",
        marker={"size": [16 if r["cur"] else 10 for r in rows],
                "color": [r["ir"] for r in rows], "colorscale": "Viridis", "showscale": True,
                "colorbar": {"title": "IR"}, "line": {"width": [3 if r["cur"] else 0 for r in rows], "color": "#e03131"}},
        text=[f"cap {r['cap']} / λ {r['lam']} / {r['lb']}d<br>IR {r['ir']:+.2f}, Calmar {r['calmar']:.2f}" for r in rows],
        hovertemplate="%{text}<br>ann %{y:.0f}%, maxDD -%{x:.0f}%<extra></extra>"))
    fig.update_layout(template="seaborn", height=440, margin={"t": 20, "l": 60, "r": 20},
                      xaxis={"title": "max drawdown (|%|) — risk →"}, yaxis={"title": "annualized return %"})
    scatter = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    nav = ('<nav style="font-size:14px;color:#555;margin:0 0 1em;padding-bottom:.5em;border-bottom:1px solid #eee;">'
           '<a href="https://github.com/joehahn/portfolio-wave-rider/blob/main/README.md">README</a>'
           ' &middot; <a href="retrieval_pwr.html">retriever DB</a>'
           ' &middot; <a href="pool_browser.html">pool browser</a>'
           ' &middot; <a href="backtest_gkg_2yr_weekly.html">curator DB</a></nav>')
    _fmt = lambda xs: ", ".join(str(x) for x in xs)  # noqa: E731
    grid_html = (
        '<h2>Parameter settings</h2>'
        f'<p style="color:#555;max-width:860px;">The three swept knobs (every combination = {len(rows)} '
        'configs) and the values considered. All other optimizer / backtest params are held at the '
        '<code>investor_profile.md</code> values — these knobs only re-weight the <b>same</b> curations.</p>'
        '<table style="font-size:13px;margin-bottom:.6em;"><thead><tr>'
        '<th style="text-align:left">parameter</th><th style="text-align:left">values swept</th>'
        '<th style="text-align:left">current (profile)</th></tr></thead><tbody>'
        f'<tr><td style="text-align:left">concentration_cap</td><td style="text-align:left">{_fmt(CAPS)}</td><td style="text-align:left">{CURRENT[0]}</td></tr>'
        f'<tr><td style="text-align:left">risk_aversion (λ)</td><td style="text-align:left">{_fmt(LAMBDAS)}</td><td style="text-align:left">{CURRENT[1]}</td></tr>'
        f'<tr><td style="text-align:left">optimizer_lookback (days)</td><td style="text-align:left">{_fmt(LOOKBACKS)}</td><td style="text-align:left">{CURRENT[2]}</td></tr>'
        '</tbody></table>'
        '<p style="color:#888;font-size:12px;max-width:860px;">Held constant (from the profile): rebalance '
        'weekly, max_watchlist_size 5, risk-free 4%, execution lag 1 trading day, anchors SPY/AGG/IAU.</p>')
    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>PWR — parameter sweep</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;margin:0 auto;
padding:0 1.5em;color:#222;line-height:1.5}}h1,h2{{color:#111}}table{{border-collapse:collapse;font-size:13px;width:100%}}
th{{text-align:right;padding:6px 10px;border-bottom:2px solid #ccc;white-space:nowrap}}th:first-child{{text-align:left}}
.built{{position:absolute;top:8px;right:16px;font-size:12px;color:#888}}
</style></head><body><div class="built">dashboard built {ts}</div>{nav}
<h1>Parameter sweep — zero-cost optimizer knobs</h1>
<p style="color:#555;max-width:860px;">{len(rows)} configs = concentration_cap × risk_aversion (λ) × optimizer_lookback,
replayed on the <b>fixed 2-year curation set</b> ({runs_dir.split('/')[-1]}). These knobs touch only the
mean-variance replay, not the curator, so the whole grid costs <b>$0</b> (no LLM). Ranked by
<b>Information Ratio</b> (annualized active return ÷ tracking error vs SPY — consistency of beating the
benchmark). SPY returned {spy_ret*100:+.0f}% over the window. ★ = best in column.</p>
<p style="color:#b45309;max-width:860px;"><b>All in-sample.</b> These rank candidate configs to
<b>forward-test</b>; they don't prove an optimum. Read the <b>IR t-stat</b> (|t|&gt;2 ≈ real vs luck),
the bootstrap <b>CI</b> (error bar on annualized return), and <b>H1/H2 stable</b> (does the edge hold in
both halves) before trusting any row.</p>
{grid_html}
<h2>1. Frontier — return vs drawdown (color = IR, red ring = current config)</h2>
{scatter}
<h2>2. All configs (ranked by IR)</h2>
<table><thead><tr>
<th>cap / λ / lookback</th><th>IR</th><th>t-stat</th><th>Calmar</th><th>alpha</th><th>ann</th>
<th>maxDD</th><th>total</th><th>hit-rate</th><th>ann CI [5,95]</th><th>H1/H2 stable</th></tr></thead>
<tbody>{trs}</tbody></table>
<p style="color:#888;font-size:12px;margin-top:1em;">IR = ann active return / tracking error vs SPY ·
Calmar = ann / |maxDD| · alpha = ann − SPY ann · hit-rate = share of rolling 6-mo windows beating SPY ·
CI = block-bootstrap 5–95% on annualized return · stable = IR &gt; 0 in both halves.</p>
</body></html>"""
    out.write_text(page)
    top = max(rows, key=lambda r: r["ir"] if r["ir"] == r["ir"] else -9e9)
    print(f"wrote {out}  ({len(rows)} configs)")
    print(f"  best IR: cap {top['cap']} / λ {top['lam']} / {top['lb']}d -> IR {top['ir']:+.2f} "
          f"t={top['tstat']:+.1f} Calmar {top['calmar']:.2f} ann {top['ann']*100:+.0f}% dd {top['dd']*100:.0f}%")
    cur = next(r for r in rows if r["cur"])
    print(f"  current (0.8/2.0/30): IR {cur['ir']:+.2f} t={cur['tstat']:+.1f} Calmar {cur['calmar']:.2f} "
          f"ann {cur['ann']*100:+.0f}% dd {cur['dd']*100:.0f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="data/curator_runs/gkg-2yr-weekly")
    ap.add_argument("--out", default=str(ROOT / "docs" / "sweep_pwr.html"))
    a = ap.parse_args()
    build(a.runs_dir, Path(a.out))
