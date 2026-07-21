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
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402

CAPS = [0.5, 0.67, 0.8, 0.9, 1.0]
LAMBDAS = [0.5, 0.75, 1.0, 1.5, 2.0]
LOOKBACKS = [14, 30, 60, 90, 120, 150]          # calendar days
ANCHORS = ["SPY", "AGG", "IAU"]

# LLM curator comparison (section 3): (label, run_dir, provider, $in/M, $out/M). Agreement is measured
# against the reference (first row). Add a row per model run you want to compare.
LLM_RUNS = [
    ("claude-sonnet-5 (reference)", "data/curator_runs/gkg-2yr-weekly", "Anthropic", 2.0, 10.0),
    ("deepseek/deepseek-v4-flash", "data/curator_runs/gkg-3yr-deepseek", "OpenRouter", 0.09, 0.19),
    ("moonshotai/kimi-k2.5", "data/curator_runs/gkg-3yr-kimi", "OpenRouter", 0.57, 2.85),
]
CURRENT = (0.8, 2.0, 30)          # the live investor_profile.md config
BLUE, GREEN, RED, GREY = "#1f77b4", "#2b8a3e", "#c92a2a", "#adb5bd"


def _metrics(totals: pd.Series, spy: pd.Series, ann_ret: float, max_dd: float) -> dict:
    """Risk-adjusted, benchmark-relative metrics from a config's equity curve + the SPY curve."""
    days = max((totals.index[-1] - totals.index[0]).days, 1)
    calmar = ann_ret / abs(max_dd) if max_dd < 0 else float("nan")
    r = totals.pct_change().dropna()
    s = spy.reindex(totals.index).ffill().pct_change().reindex(r.index)
    act = (r - s).dropna()
    ir = tstat = sharpe = float("nan")
    if len(r) > 2 and r.std() > 0:
        ppy = len(r) / (days / 365.25)
        # Sharpe = (excess-over-risk-free return) / total volatility, annualized (risk-free 4%/yr)
        sharpe = (r.mean() - 0.04 / ppy) / r.std() * np.sqrt(ppy)
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
    return {"calmar": calmar, "ir": ir, "tstat": tstat, "sharpe": sharpe, "alpha": alpha, "hit": hit,
            "ci_lo": ci[0], "ci_hi": ci[1], "ir_h1": ir_h1, "ir_h2": ir_h2}


def _llm_rows(cfg):
    """One metrics row per curator LLM in LLM_RUNS. cfg = (cap, λ, lookback_days) held constant so the
    only variable is the LLM. Decision agreement + valid-JSON are the LLM-specific signals; the backtest
    metrics use the same profile config for every model. Runs not yet finished are marked pending."""
    import glob

    def _decisions(d):
        o = {}
        for f in glob.glob(str(ROOT / d / "2*-curation.json")):
            try:
                c = json.loads(Path(f).read_text())
            except Exception:  # noqa: BLE001
                continue
            o[Path(f).name[:10]] = (tuple(sorted(x.get("ticker", "") for x in c.get("adds", []))),
                                    tuple(sorted(x.get("ticker", "") for x in c.get("removes", []))))
        return o

    ref = _decisions(LLM_RUNS[0][1])
    cap, lam, lb = cfg
    out = []
    for label, d, prov, pin, pout in LLM_RUNS:
        rd = ROOT / d
        r = {"label": label, "prov": prov, "pending": not (rd / "_backtest" / "snapshots.csv").exists()}
        if r["pending"]:
            out.append(r)
            continue
        dec = _decisions(d)
        n = len(dec) or 1
        both = set(dec) & set(ref)
        r["agree"] = (sum(1 for x in both if dec[x] == ref[x]) / len(both)) if both else float("nan")
        r["nadd"] = sum(len(v[0]) for v in dec.values())
        r["nrem"] = sum(len(v[1]) for v in dec.values())
        r["json"] = (n - len(glob.glob(str(rd / "_parse_fail" / "*.txt")))) / n
        tin = tout = nl = 0
        for lf in glob.glob(str(rd / "_log" / "*-curator.json")):
            try:
                u = json.loads(Path(lf).read_text()).get("usage", {})
                tin += u.get("in", 0); tout += u.get("out", 0); nl += 1
            except Exception:  # noqa: BLE001
                pass
        r["cost"] = (((tin / nl) * pin + (tout / nl) * pout) / 1e6 * n) if nl else float("nan")  # avg/call x n
        # curator wall time: median gap between consecutive curation writes x calls (median ignores the
        # resume gap, so it estimates the true per-call latency even for a run that was resumed).
        fs = sorted(glob.glob(str(rd / "2*-curation.json")), key=os.path.getmtime)
        gaps = sorted(os.path.getmtime(fs[i + 1]) - os.path.getmtime(fs[i]) for i in range(len(fs) - 1)) if len(fs) >= 3 else []
        r["secs_call"] = gaps[len(gaps) // 2] if gaps else float("nan")
        r["time_min"] = (r["secs_call"] * len(fs) / 60.0) if gaps else float("nan")
        _out = f"/tmp/_llm/{label.replace('/', '_').replace(' ', '')}"
        res = portfolio.curator_backtest(runs_dir=str(rd), out_dir=_out, max_weight=cap, risk_aversion=lam,
                                         benchmarks=["SPY"], lookback_years_override=lb / 365.0, always_include=ANCHORS)
        snaps = pd.read_csv(Path(_out) / "snapshots.csv", parse_dates=["date"])
        totals = snaps.groupby("date")["total_value"].first().sort_index()
        spy = portfolio._fetch_benchmark_curves(["SPY"], totals.index[0], totals.index[-1], float(totals.iloc[0]))["SPY"]
        r.update(ret=res["realized_return"], ann=res["annualized_return"], dd=res["max_drawdown"],
                 **_metrics(totals, spy, res["annualized_return"], res["max_drawdown"]))
        r["curve_x"] = [d.strftime("%Y-%m-%d") for d in totals.index]
        r["curve_y"] = [float(v) for v in totals.values]
        # per-date weight vector (value / total) for the portfolio-similarity plot
        _wm = snaps.pivot_table(index="date", columns="ticker", values="value", aggfunc="first").fillna(0.0)
        _wm = _wm.div(_wm.sum(axis=1).replace(0, 1.0), axis=0)
        if not out:
            r["_wm"] = _wm                                   # reference weights (kept for comparison)
        else:
            _ref_wm = out[0].get("_wm")
            if _ref_wm is not None:
                _cd = _wm.index.intersection(_ref_wm.index)
                _cols = _wm.columns.union(_ref_wm.columns)
                _a = _wm.reindex(index=_cd, columns=_cols, fill_value=0.0).values
                _b = _ref_wm.reindex(index=_cd, columns=_cols, fill_value=0.0).values
                r["sim_x"] = [d.strftime("%Y-%m-%d") for d in _cd]
                r["sim_y"] = [float(v) for v in np.minimum(_a, _b).sum(axis=1)]
        if not out:  # first finished (reference) run carries the SHARED SPY + buy/hold curves for plot 4
            r["spy_y"] = [float(v) for v in spy.reindex(totals.index).ffill().values]
            try:
                bl = pd.read_csv(Path(_out) / "baselines_totals.csv", parse_dates=["date"])
                bnh = bl.set_index("date")["eq_total"].dropna()
                r["bnh_x"] = [d.strftime("%Y-%m-%d") for d in bnh.index]
                r["bnh_y"] = [float(v) for v in bnh.values]
            except Exception:  # noqa: BLE001
                r["bnh_x"] = r["bnh_y"] = None
        out.append(r)
    return out


def build(runs_dir: str, out: Path, recompute: bool = False) -> None:
    # The 150 backtest replays are the slow part; the metrics are DETERMINISTIC for a given (grid, curation
    # set), so cache them. A text/layout-only edit then re-renders instantly (--recompute forces a sweep).
    import hashlib
    import json as _json
    cache_p = ROOT / "data" / "curator_runs" / "_sweep_cache.json"
    key = hashlib.md5(_json.dumps([CAPS, LAMBDAS, LOOKBACKS, list(CURRENT), runs_dir]).encode()).hexdigest()
    rows = spy_ret = None
    if not recompute and cache_p.exists():
        try:
            c = _json.loads(cache_p.read_text())
            if c.get("key") == key:
                rows, spy_ret = c["rows"], c["spy_ret"]
                print(f"  loaded {len(rows)} configs from cache (--recompute to re-sweep)")
        except Exception:  # noqa: BLE001
            pass
    if rows is None:
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
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(_json.dumps({"key": key, "spy_ret": spy_ret, "rows": rows}))

    # best per metric (higher is better except dd where less-negative is better)
    best = {k: max(rows, key=lambda r: (r[k] if r[k] == r[k] else -9e9))
            for k in ("ir", "calmar", "sharpe", "alpha", "ann", "hit")}
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
            f"{td(r['sharpe'], '{:.2f}', '', star(r,'sharpe'))}"
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

    # recommended-settings table: the live config (keep) vs the best-in-sample (forward-test, don't chase)
    _cur_row = next(r for r in rows if r["cur"])
    _best_ir = best["ir"]
    _cfg = lambda r: f"{r['cap']:.2f} / {r['lam']:.2f} / {r['lb']}d"  # noqa: E731
    _lc = 'style="text-align:left"'
    rec_html = (
        '<h3 style="margin:.6em 0 .2em;">Recommended settings</h3>'
        f'<table style="font-size:13px;margin-bottom:.6em;"><thead><tr><th {_lc}>config (cap/λ/lookback)</th>'
        f'<th {_lc}>IR</th><th {_lc}>Sharpe</th><th {_lc}>maxDD</th><th {_lc}>recommendation</th></tr></thead><tbody>'
        f'<tr><td {_lc}><b>{_cfg(_cur_row)}</b> — current / live</td><td {_lc}>{_cur_row["ir"]:+.2f}</td>'
        f'<td {_lc}>{_cur_row["sharpe"]:.2f}</td><td {_lc}>{_cur_row["dd"]*100:.0f}%</td>'
        f'<td {_lc}><b>Keep this.</b> Conservative cap + short momentum window; solid and H1/H2-stable. The '
        'sweep&#39;s &ldquo;best&rdquo; keeps drifting to the longest lookback as the grid grows '
        '(30&rarr;90&rarr;120d) = overfitting the in-sample path, so do NOT chase it. Change only on '
        'forward evidence.</td></tr>'
        f'<tr><td {_lc}>{_cfg(_best_ir)} — best in-sample IR</td><td {_lc}>{_best_ir["ir"]:+.2f}</td>'
        f'<td {_lc}>{_best_ir["sharpe"]:.2f}</td><td {_lc}>{_best_ir["dd"]*100:.0f}%</td>'
        f'<td {_lc}>Highest in-sample risk-adjusted return, but it sits at the lookback grid EDGE (a classic '
        'overfit tell). A candidate to <b>forward-test</b>, not to adopt live yet.</td></tr></tbody></table>')

    # section 3: per-LLM comparison (same pools + profile config; only the curator model varies)
    def _c2(v, fmt):
        return f'<td {_lc}>{fmt.format(v) if v == v else "n/a"}</td>'
    _llm = _llm_rows(CURRENT)
    llm_trs = ""
    for r in _llm:
        if r.get("pending"):
            llm_trs += (f'<tr style="border-bottom:1px solid #eee;color:#999;"><td {_lc}>{r["label"]}</td>'
                        f'<td {_lc}>{r["prov"]}</td><td {_lc} colspan="10"><i>run in progress…</i></td></tr>')
            continue
        bg = "background:#fff7e6;" if "reference" in r["label"] else ""
        llm_trs += (
            f'<tr style="{bg}border-bottom:1px solid #eee;"><td {_lc}><b>{r["label"]}</b></td><td {_lc}>{r["prov"]}</td>'
            + _c2(r["cost"], "${:,.2f}") + _c2(r["time_min"], "{:.0f} min")
            + _c2(r["json"] * 100, "{:.0f}%") + _c2(r["agree"] * 100, "{:.0f}%")
            + f'<td {_lc}>{r["nadd"]} / {r["nrem"]}</td>' + _c2(r["ret"] * 100, "{:+.0f}%")
            + _c2(r["ir"], "{:+.2f}") + _c2(r["sharpe"], "{:.2f}") + _c2(r["calmar"], "{:.2f}")
            + _c2(r["dd"] * 100, "{:.0f}%") + "</tr>")
    llm_html = (
        '<h2>3. LLM comparison — curator model (same pools + profile config)</h2>'
        '<p style="color:#555;max-width:920px;">Every model reads the <b>same</b> news pools and replays at '
        'the profile config (cap 0.8 / λ 2.0 / 30d); the only variable is the curator LLM. The decision '
        'columns are the ones that matter: <b>agree</b> = share of weeks the model made the identical '
        'add/remove call as the reference (top row), <b>valid-JSON</b> = share of calls that parsed, '
        '<b>$/run</b> = curator LLM cost of a full 157-week curate, and <b>curator time</b> = wall-clock of '
        'those 157 calls (≈ per-call latency × 157; excludes GKG ingest + optimizer replay). Backtest '
        'columns are secondary (in-sample / leaky). A cheap model that tracks the reference makes the whole '
        'non-zero-cost sweep affordable.</p>'
        f'<table><thead><tr><th style="text-align:left">model</th><th style="text-align:left">provider</th>'
        f'<th {_lc}>$/run</th><th {_lc}>curator time</th><th {_lc}>valid-JSON</th><th {_lc}>agree vs ref</th><th {_lc}>adds/removes</th>'
        f'<th {_lc}>total</th><th {_lc}>IR</th><th {_lc}>Sharpe</th><th {_lc}>Calmar</th><th {_lc}>maxDD</th>'
        f'</tr></thead><tbody>{llm_trs}</tbody></table>')

    # plot 4: equity-curve race per LLM + buy/hold + SPY (no rebalance markers, per request)
    import plotly.graph_objects as go
    _curved = [r for r in _llm if r.get("curve_x")]
    _ref = _curved[0] if _curved else None
    # LLM palette avoids blue/green (reserved for buy/hold + SPY, matching the curator DB's plot 1)
    _pal = ["#d97706", "#9467bd", "#d62728", "#8c564b", "#e377c2"]
    _fig4 = go.Figure()
    for _i, r in enumerate(_curved):
        _fig4.add_trace(go.Scatter(x=r["curve_x"], y=r["curve_y"], mode="lines", name=r["label"].split(" (")[0],
                                   line={"color": _pal[_i % len(_pal)], "width": 2.2}))
    if _ref and _ref.get("bnh_x"):
        _fig4.add_trace(go.Scatter(x=_ref["bnh_x"], y=_ref["bnh_y"], mode="lines", name="Buy-and-hold",
                                   line={"color": "#3b82f6", "width": 1.8}))
    if _ref and _ref.get("spy_y"):
        _fig4.add_trace(go.Scatter(x=_ref["curve_x"], y=_ref["spy_y"], mode="lines", name="SPY benchmark",
                                   line={"color": "#10b981", "width": 1.5, "dash": "dot"}))
    _fig4.update_layout(template="seaborn", height=460, margin={"t": 20, "l": 72, "r": 20}, hovermode="x unified")
    _fig4.update_yaxes(title_text="portfolio value ($)", type="log",
                       tickvals=[10000, 30000, 100000, 300000, 1000000],
                       ticktext=["$10K", "$30K", "$100K", "$300K", "$1M"])
    llm4_html = (('<h2>4. Portfolio value over time — by curator LLM (vs buy/hold and SPY)</h2>'
                  '<p style="color:#555;max-width:920px;">Each LLM\'s realized portfolio value on the same pools '
                  'and profile config, alongside the equal-weight buy/hold starter and SPY. Same idea as the '
                  'curator DB\'s plot 1, without the rebalance markers.</p>'
                  + _fig4.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))
                 if _curved else "")

    # plot 5: portfolio similarity over time (overlap coefficient vs the reference LLM)
    _simmed = [r for r in _llm if r.get("sim_x")]
    llm5_html = ""
    if _simmed and _ref:
        _fig5 = go.Figure()
        for _i, r in enumerate(_simmed):
            _fig5.add_trace(go.Scatter(
                x=r["sim_x"], y=r["sim_y"], mode="lines",
                name=f"{r['label'].split(' (')[0]} vs {_ref['label'].split(' (')[0]}",
                line={"color": _pal[(_i + 1) % len(_pal)], "width": 2}))
        _fig5.update_layout(template="seaborn", height=380, margin={"t": 20, "l": 60, "r": 20},
                            yaxis={"title": "portfolio overlap (Σ min weight)", "range": [0, 1.02]},
                            hovermode="x unified")
        llm5_html = ('<h2>5. Portfolio similarity over time — overlap vs the reference curator</h2>'
                     '<p style="color:#555;max-width:920px;">Per-date portfolio <b>overlap coefficient</b> '
                     '&Sigma; min(weight) between each LLM\'s holdings and the reference (Sonnet): 1.0 = '
                     'identical holdings &amp; weights, 0 = disjoint. Shows WHEN a cheap model\'s portfolio '
                     'diverges (vs the single agreement % in table 3).</p>'
                     + _fig5.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))
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
{rec_html}
<p style="color:#666;font-size:12px;margin:.4em 0 .6em;max-width:920px;line-height:1.6;"><b>Column meanings:</b><br>
<b>cap / λ / lookback</b> — the config: concentration cap (max weight per position) · risk-aversion λ · optimizer lookback (days of prices used to estimate μ/Σ).<br>
<b>IR</b> — Information Ratio = annualized active return ÷ tracking error vs SPY. Consistency of beating SPY; this is the ranking column.<br>
<b>t-stat</b> — statistical significance of the IR (= IR·√years). |t|&gt;2 ≈ the edge is real rather than luck.<br>
<b>Sharpe</b> — (annualized return − 4% risk-free) ÷ total volatility. Standalone risk-adjusted return (per unit of total risk).<br>
<b>Calmar</b> — annualized return ÷ |max drawdown|. Return earned per unit of worst peak-to-trough loss.<br>
<b>alpha</b> — annualized return − SPY's annualized return (excess over the benchmark).<br>
<b>ann</b> — annualized return · <b>maxDD</b> — deepest peak-to-trough drawdown · <b>total</b> — total return over the whole window.<br>
<b>hit-rate</b> — share of rolling 6-month windows in which the config beat SPY.<br>
<b>ann CI [5,95]</b> — block-bootstrap 5–95% confidence interval on the annualized return (the error bar).<br>
<b>H1/H2 stable</b> — whether IR &gt; 0 in <i>both</i> halves of the window (a yes means the edge isn't a one-half artifact).</p>
<table><thead><tr>
<th>cap / λ / lookback</th><th>IR</th><th>t-stat</th><th>Sharpe</th><th>Calmar</th><th>alpha</th><th>ann</th>
<th>maxDD</th><th>total</th><th>hit-rate</th><th>ann CI [5,95]</th><th>H1/H2 stable</th></tr></thead>
<tbody>{trs}</tbody></table>
{llm_html}
{llm4_html}
{llm5_html}
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
    ap.add_argument("--recompute", action="store_true", help="re-run the 150 backtests (else use cache)")
    a = ap.parse_args()
    build(a.runs_dir, Path(a.out), recompute=a.recompute)
