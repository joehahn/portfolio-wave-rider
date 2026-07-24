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

import dash_nav  # shared cross-page nav (Forward | Backtest groups)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio  # noqa: E402

CAPS = [0.5, 0.67, 0.8, 0.9, 1.0]
LAMBDAS = [0.5, 0.75, 1.0, 1.5, 2.0]
LOOKBACKS = [14, 30, 60, 90, 120, 150]          # calendar days
ANCHORS = ["SPY", "AGG", "IAU"]

# LLM curator comparison (section 3): (label, run_dir, provider, $in/M, $out/M). Agreement is measured
# against the reference (first row). Add a row per model run you want to compare.
LLM_RUNS = [   # row 0 = the DEFAULT curator. The multi-LLM comparison (Sonnet gkg-2yr-weekly + deepseek
               # gkg-3yr-deepseek) is preserved in archived/sweep_pwr-with-LLM-comparison.html; those runs
               # were retired from local storage. Re-add rows + re-run to refresh the comparison on this window.
    ("moonshotai/kimi-k2.5 (default)", "data/curator_runs/gkg-3yr-final", "OpenRouter", 0.57, 2.85),
]
CURRENT = (0.8, 2.0, 30)          # the live investor_profile.md config
BLUE, GREEN, RED, GREY = "#1f77b4", "#2b8a3e", "#c92a2a", "#adb5bd"

# max_watchlist_size sweep (section 6): unlike cap/lambda/lookback, this knob changes the CURATOR's
# decisions, so each cap is a separate RE-CURATION (LLM cost), not a free replay. cap 5 = the canonical
# run; the rest are re-curated into gkg-3yr-mws{cap}. Tests whether more slots let the curator add NVDA.
MWS_SWEEP = [(3, "data/curator_runs/gkg-3yr-mws3"), (5, "data/curator_runs/gkg-3yr-final"),
             (8, "data/curator_runs/gkg-3yr-mws8"), (10, "data/curator_runs/gkg-3yr-mws10"),
             (12, "data/curator_runs/gkg-3yr-mws12"), (16, "data/curator_runs/gkg-3yr-mws16")]


def _mws_rows():
    """Per-cap: total return, #watchlist-changes, whether NVDA was ever added, and the final watchlist.
    Reads each cap run dir; caps whose run is missing/incomplete are marked pending."""
    import glob
    _fm = portfolio.load_financial_model()
    starter = list(_fm.get("starter_watchlist") or [])
    anchors = set(_fm.get("always_include") or [])
    rows = []
    for cap, rd in MWS_SWEEP:
        curs = sorted(glob.glob(str(ROOT / rd / "2*-curation.json")))
        bt = ROOT / rd / "_backtest" / "snapshots.csv"
        if len(curs) < 79 or not bt.exists():
            rows.append({"cap": cap, "pending": True}); continue
        sn = pd.read_csv(bt, parse_dates=["date"])
        tot = sn.groupby("date")["total_value"].first()
        ret = float(tot.iloc[-1] / tot.iloc[0] - 1.0)
        # The ACTUAL watchlist is what the validated backtest tracked (snapshots include every watchlist
        # ticker, even at 0 shares) — NOT a naive replay of proposed adds/removes, which double-counts
        # cap-rejected / unpaired proposals. Per-date ticker set (minus anchors) = the true watchlist.
        by_date = sn.groupby("date")["ticker"].apply(lambda s: frozenset(s) - anchors)
        nvda = any("NVDA" in wl for wl in by_date)           # NVDA actually ENTERED the watchlist (not just proposed)
        nchg = int((by_date != by_date.shift()).sum()) - 1   # times the actual watchlist changed
        picks = sorted(by_date.iloc[-1])                     # final validated watchlist (managed picks)
        # also surface NVDA that was proposed but rejected (never entered): a softer "curator wanted it" signal
        nvda_proposed = any("NVDA" in [a["ticker"] for a in json.loads(Path(f).read_text()).get("adds", [])]
                            for f in curs)
        rows.append({"cap": cap, "pending": False, "ret": ret, "nchg": nchg,
                     "nvda": nvda, "nvda_proposed": nvda_proposed, "wl": picks})
    return rows


def _churn_metrics(snaps: pd.DataFrame):
    """Two annualized measures of how much a config reshuffles the portfolio. A trade = a change in a
    ticker's SHARE count between snapshots (weight DRIFT between rebalances isn't a trade). Per rebalance,
    the signed weight-change vector from trades is dw_i = (dshares_i * price_i) / total_value. Then:
      L1 (turnover): sum_i |dw_i| per rebalance, halved (one-way), annualized -> %/yr. Industry standard.
      L2 (course-correction): sqrt(sum_i dw_i^2) per rebalance (Euclidean step length in weight-space),
         annualized -> the total path length the portfolio was dragged along per year. Emphasizes
         CONCENTRATED single-name rotations more than L1 does.
    Returns (L1_ann_pct, L2_ann_pct)."""
    sh = snaps.pivot_table(index="date", columns="ticker", values="shares", fill_value=0.0).sort_index()
    px = snaps.pivot_table(index="date", columns="ticker", values="price", fill_value=0.0).sort_index()
    tot = snaps.groupby("date")["total_value"].first().sort_index()
    dw = (sh.diff() * px).div(tot.replace(0, np.nan), axis=0).fillna(0.0)     # signed dweight per ticker/date
    l1 = float(dw.abs().sum(axis=1).sum())                  # sum |dw|, whole window (two-way)
    l2 = float(np.sqrt((dw ** 2).sum(axis=1)).sum())        # sum of Euclidean step lengths (L2 path length)
    years = max((tot.index[-1] - tot.index[0]).days / 365.25, 1e-9)
    return 100.0 * (l1 / 2.0) / years, 100.0 * l2 / years   # L1 one-way %/yr, L2 path-length /yr (x100)


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
    key = hashlib.md5(_json.dumps([CAPS, LAMBDAS, LOOKBACKS, list(CURRENT), runs_dir, "churn-v2-l1l2"]).encode()).hexdigest()
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
                    _l1, _l2 = _churn_metrics(snaps)
                    rows.append({"cap": cap, "lam": lam, "lb": lb, "ret": res["realized_return"],
                                 "ann": res["annualized_return"], "dd": res["max_drawdown"],
                                 "l1": _l1, "l2": _l2, **m,
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
    _mws_fixed = int(portfolio.load_financial_model().get("max_watchlist_size", 5))  # held constant in plots 1-3
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[abs(r["dd"]) * 100 for r in rows], y=[r["ann"] * 100 for r in rows], mode="markers",
        marker={"size": [16 if r["cur"] else 10 for r in rows],
                "color": [r["ir"] for r in rows], "colorscale": "Viridis", "showscale": True,
                "colorbar": {"title": "IR"}, "line": {"width": [3 if r["cur"] else 0 for r in rows], "color": "#e03131"}},
        text=[f"cap {r['cap']} / λ {r['lam']} / {r['lb']}d · watchlist {_mws_fixed}"
              f"<br>IR {r['ir']:+.2f}, Calmar {r['calmar']:.2f}" for r in rows],
        hovertemplate="%{text}<br>ann %{y:.0f}%, maxDD -%{x:.0f}%<extra></extra>"))
    fig.update_layout(template="seaborn", height=440, margin={"t": 20, "l": 60, "r": 20},
                      xaxis={"title": "max drawdown (|%|) — risk →"}, yaxis={"title": "annualized return %"})
    scatter = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    # plots 2 & 3: return vs churn, two norms. Same configs as plot 1; UPPER-LEFT (high return, low churn)
    # = the stable, less-overfit ideal. L1 = turnover (industry standard); L2 = course-correction (Euclidean
    # path length through weight-space, emphasizes concentrated rotations).
    def _churn_scatter(field, xtitle):
        f = go.Figure()
        f.add_trace(go.Scatter(
            x=[r[field] for r in rows], y=[r["ann"] * 100 for r in rows], mode="markers",
            marker={"size": [16 if r["cur"] else 10 for r in rows],
                    "color": [r["ir"] for r in rows], "colorscale": "Viridis", "showscale": True,
                    "colorbar": {"title": "IR"}, "line": {"width": [3 if r["cur"] else 0 for r in rows], "color": "#e03131"}},
            text=[f"cap {r['cap']} / λ {r['lam']} / {r['lb']}d · watchlist {_mws_fixed}"
                  f"<br>IR {r['ir']:+.2f}, {field.upper()} {r[field]:.0f}" for r in rows],
            hovertemplate="%{text}<br>ann %{y:.0f}%, " + field.upper() + " %{x:.0f}<extra></extra>"))
        f.update_layout(template="seaborn", height=440, margin={"t": 20, "l": 60, "r": 20},
                        xaxis={"title": xtitle}, yaxis={"title": "annualized return %"})
        return f.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
    l1_scatter = _churn_scatter("l1", "L1 churn — annualized one-way turnover (%/yr) → more trading")
    l2_scatter = _churn_scatter("l2", "L2 course-correction — annualized weight-space path length → more rotation")
    _cur_l1 = next((r["l1"] for r in rows if r["cur"]), float("nan"))
    _cur_l2 = next((r["l2"] for r in rows if r["cur"]), float("nan"))
    import numpy as _np2
    _l1l2_corr = float(_np2.corrcoef([r["l1"] for r in rows], [r["l2"] for r in rows])[0, 1])

    nav = dash_nav.render("sweep_pwr.html", built=False)   # this page renders its own "dashboard built" stamp
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
        f'<tr><td style="text-align:left">max_watchlist_size <span style="color:#9a6a00;">(re-curation, §9)</span></td>'
        f'<td style="text-align:left">{_fmt([c for c, _ in MWS_SWEEP])}</td><td style="text-align:left">{_mws_fixed}</td></tr>'
        '</tbody></table>'
        f'<p style="color:#888;font-size:12px;max-width:860px;">The first three (cap / λ / lookback) are FREE '
        f'math-replay knobs — every combination = {len(rows)} configs on the <b>same</b> curations (plots 1-4). '
        '<b>max_watchlist_size is different</b>: it changes the CURATOR\'s decisions, so each value is a separate '
        'non-zero-cost RE-CURATION (section 9 + plot 5), not a replay. Held constant elsewhere (from the '
        f'profile): rebalance weekly, max_watchlist_size {_mws_fixed}, risk-free 4%, execution lag 1 trading '
        'day, anchors SPY/AGG/IAU.</p>')

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
                        f'<td {_lc}>{r["prov"]}</td><td {_lc} colspan="11"><i>run in progress…</i></td></tr>')
            continue
        bg = "background:#fff7e6;" if "reference" in r["label"] else ""
        llm_trs += (
            f'<tr style="{bg}border-bottom:1px solid #eee;"><td {_lc}><b>{r["label"]}</b></td><td {_lc}>{r["prov"]}</td>'
            + _c2(r["cost"], "${:,.2f}") + _c2(r["time_min"], "{:.0f} min")
            + _c2(r["json"] * 100, "{:.0f}%") + _c2(r["agree"] * 100, "{:.0f}%")
            + f'<td {_lc}>{r["nadd"]} / {r["nrem"]}</td>' + _c2(r["ret"] * 100, "{:+.0f}%")
            + _c2(r["ir"], "{:+.2f}") + _c2(r["tstat"], "{:+.1f}") + _c2(r["sharpe"], "{:.2f}")
            + _c2(r["calmar"], "{:.2f}") + _c2(r["dd"] * 100, "{:.0f}%") + "</tr>")
    llm_html = (
        '<h2>6. LLM comparison — curator model (same pools + profile config)</h2>'
        '<p style="color:#555;max-width:920px;">Every model reads the <b>same</b> news pools and replays at '
        'the profile config (cap 0.8 / λ 2.0 / 30d); the only variable is the curator LLM. The decision '
        'columns are the ones that matter: <b>agree</b> = share of weeks the model made the identical '
        'add/remove call as the <b>default</b> model (top row, kimi), <b>valid-JSON</b> = share of calls that parsed, '
        '<b>$/run</b> = curator LLM cost of a full 157-week curate, and <b>curator time</b> = wall-clock of '
        'those 157 calls (≈ per-call latency × 157; excludes GKG ingest + optimizer replay). Backtest '
        'columns are secondary (in-sample / leaky). A cheap model that tracks the default makes the whole '
        'non-zero-cost sweep affordable.</p>'
        f'<table><thead><tr><th style="text-align:left">model</th><th style="text-align:left">provider</th>'
        f'<th {_lc}>$/run</th><th {_lc}>curator time</th><th {_lc}>valid-JSON</th><th {_lc}>agree vs default</th><th {_lc}>adds/removes</th>'
        f'<th {_lc}>total</th><th {_lc}>IR</th><th {_lc}>t-stat</th><th {_lc}>Sharpe</th><th {_lc}>Calmar</th><th {_lc}>maxDD</th>'
        f'</tr></thead><tbody>{llm_trs}</tbody></table>')

    # plot 4: equity-curve race per LLM + buy/hold + SPY (no rebalance markers, per request)
    import plotly.graph_objects as go
    _curved = [r for r in _llm if r.get("curve_x")]
    _ref = _curved[0] if _curved else None
    _xr = [_curved[0]["curve_x"][0], _curved[0]["curve_x"][-1]] if _curved else None  # shared x-range for 4 & 5
    _MARG = {"t": 20, "l": 72, "r": 135}   # identical margins so plots 4 & 5 line up; r leaves room for legend
    _LEG = {"x": 1.02, "xanchor": "left", "y": 1, "yanchor": "top"}
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
    _fig4.update_layout(template="seaborn", height=460, margin=_MARG, hovermode="x unified", legend=_LEG)
    _fig4.update_xaxes(range=_xr)
    _fig4.update_yaxes(title_text="portfolio value ($)", type="log",
                       tickvals=[10000, 30000, 100000, 300000, 1000000],
                       ticktext=["$10K", "$30K", "$100K", "$300K", "$1M"])
    llm4_html = (('<h2>7. Portfolio value over time — by curator LLM (vs buy/hold and SPY)</h2>'
                  '<p style="color:#555;max-width:920px;">Each LLM\'s realized portfolio value on the same pools '
                  'and profile config, alongside the equal-weight buy/hold starter and SPY. Same idea as the '
                  'curator DB\'s plot 1, without the rebalance markers.</p>'
                  + _fig4.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))
                 if _curved else "")

    # plot 5 (new): equity-curve race by max_watchlist_size (one curve per re-curated cap) + buy/hold + SPY.
    _mws_pal = ["#d97706", "#9467bd", "#d62728", "#8c564b", "#e377c2", "#17becf"]
    _mwsfig = go.Figure()
    _mws_curves = 0
    for _i, (cap, rd) in enumerate(MWS_SWEEP):
        _sp = ROOT / rd / "_backtest" / "snapshots.csv"
        if not _sp.exists():
            continue
        _tt = pd.read_csv(_sp, parse_dates=["date"]).groupby("date")["total_value"].first().sort_index()
        _mwsfig.add_trace(go.Scatter(x=[d.strftime("%Y-%m-%d") for d in _tt.index], y=list(_tt.values),
                                     mode="lines", name=f"size {cap}" + (" (current)" if cap == _mws_fixed else ""),
                                     line={"color": _mws_pal[_i % len(_mws_pal)], "width": 2.2}))
        _mws_curves += 1
    if _ref and _ref.get("bnh_x"):
        _mwsfig.add_trace(go.Scatter(x=_ref["bnh_x"], y=_ref["bnh_y"], mode="lines", name="Buy-and-hold",
                                     line={"color": "#3b82f6", "width": 1.8}))
    if _ref and _ref.get("spy_y"):
        _mwsfig.add_trace(go.Scatter(x=_ref["curve_x"], y=_ref["spy_y"], mode="lines", name="SPY benchmark",
                                     line={"color": "#10b981", "width": 1.5, "dash": "dot"}))
    _mwsfig.update_layout(template="seaborn", height=460, margin=_MARG, hovermode="x unified", legend=_LEG)
    _mwsfig.update_xaxes(range=_xr)
    _mwsfig.update_yaxes(title_text="portfolio value ($)", type="log",
                         tickvals=[10000, 30000, 100000, 300000, 1000000],
                         ticktext=["$10K", "$30K", "$100K", "$300K", "$1M"])
    mws_equity_html = (('<h2>5. Portfolio value over time — by max_watchlist_size (vs buy/hold and SPY)</h2>'
                        '<p style="color:#555;max-width:920px;">Each re-curated watchlist cap\'s realized '
                        'portfolio value on the same pools and starter — the equity-curve view of the '
                        'section-9 sweep. Tighter (smaller) watchlists concentrate into the top picks; wider '
                        'ones dilute across more next-wave names.</p>'
                        + _mwsfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))
                       if _mws_curves else "")

    # section 5: blind rationale-soundness judge (leak-free). Reads data/curator_runs/_judge_scores.json
    # produced by scripts/judge_curations.py; if absent, the section is simply omitted.
    llm5_html = ""
    _jf = ROOT / "data" / "curator_runs" / "_judge_scores.json"
    if _jf.exists():
        J = json.loads(_jf.read_text())
        _cr = J["criteria"]
        _crlabel = {"on_thesis": "on-thesis", "evidence_supports": "evidence", "real_catalyst": "catalyst",
                    "disciplined": "discipline", "valid_ticker": "valid-ticker"}
        _mods = sorted(J["models"].items(), key=lambda kv: -(kv[1]["mean_overall"] or 0))  # best reasoning first
        _jtrs = ""
        for i, (label, s) in enumerate(_mods):
            bg = "background:#eef7ee;" if i == 0 else ("background:#fafafa;" if i % 2 else "")
            _jtrs += (f'<tr style="{bg}border-bottom:1px solid #eee;"><td {_lc}><b>{label.split("/")[-1]}</b></td>'
                      + _c2(s["mean_overall"], "{:.2f}") + f'<td {_lc}>{s["n"]}</td>'
                      + _c2(s["add_mean"], "{:.2f}") + _c2(s["rem_mean"], "{:.2f}")
                      + "".join(_c2((s[k] or 0) * 100, "{:.0f}%") for k in _cr) + "</tr>")
        llm5_html = (
            '<h2>8. Rationale-soundness — blind judge (leak-free)</h2>'
            '<p style="color:#555;max-width:920px;">Every backtest column above (return, IR, Sharpe, Calmar, '
            't-stat) is <b>in-sample</b> &mdash; the curator could have memorized which 2023&ndash;2026 names '
            'later won, so those numbers can\'t honestly rank <i>reasoning</i>. This section does: an '
            f'independent judge (<b>{J["judge_model"]}</b>, not one of the curators) reads each add/remove '
            '<b>blind</b> &mdash; model identity stripped, decisions shuffled, and the ticker\'s later price '
            '<b>never shown</b> &mdash; and grades only whether the stated rationale + cited news justified the '
            'call at the time. Five criteria (each pass/fail) + a holistic <b>overall</b> 1&ndash;5. '
            f'{J["n_decisions"]} decisions judged, ~${J["cost_usd"]:.2f}. Ranked by mean overall &mdash; this is '
            'the one ranking here that owes nothing to hindsight.</p>'
            f'<table><thead><tr><th style="text-align:left">curator</th><th {_lc}>overall (1-5)</th>'
            f'<th {_lc}>n</th><th {_lc}>add</th><th {_lc}>remove</th>'
            + "".join(f'<th {_lc}>{_crlabel[k]}</th>' for k in _cr)
            + f'</tr></thead><tbody>{_jtrs}</tbody></table>'
            '<p style="color:#666;font-size:12px;max-width:920px;line-height:1.6;margin:.5em 0 .3em;">'
            '<b>overall</b> = mean 1&ndash;5 soundness · <b>add/remove</b> = mean overall split by action · the '
            'five % columns = share of that curator\'s decisions passing each criterion: <b>on-thesis</b> (maps '
            'to a named wave), <b>evidence</b> (cited news actually supports the claim), <b>catalyst</b> '
            '(concrete milestone, not noise), <b>discipline</b> (buildup-not-crest; a remove is justified, not '
            'churn), <b>valid-ticker</b> (real investable US listing, not a name/delisted/false-match).</p>')
    # section 6: max_watchlist_size sweep (non-zero-cost: each cap is a re-curation). Does more room let
    # the curator add NVDA? Table (cap / return / #changes / NVDA? / final watchlist) + a return-vs-cap bar.
    _mws = _mws_rows()
    _done = [r for r in _mws if not r.get("pending")]
    if _done:
        import plotly.graph_objects as _mgo
        _mfig = _mgo.Figure(_mgo.Bar(
            x=[r["cap"] for r in _done], y=[r["ret"] * 100 for r in _done],
            marker_color=[RED if r["nvda"] else BLUE for r in _done],
            text=["NVDA added" if r["nvda"] else "" for r in _done], textposition="outside",
            hovertemplate="cap %{x}: %{y:+.0f}%<extra></extra>"))
        _mfig.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 60, "r": 20},
                            xaxis={"title": "max_watchlist_size", "dtick": 1},
                            yaxis={"title": "curator total return %"})
        _mbar = _mfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
    else:
        _mbar = ""
    _mtr = ""
    for r in _mws:
        if r.get("pending"):
            _mtr += (f'<tr style="color:#999;"><td {_lc}>{r["cap"]}</td>'
                     f'<td {_lc} colspan="4"><i>re-curation in progress…</i></td></tr>')
            continue
        _nv = ('<b style="color:#c92a2a;">yes</b>' if r["nvda"]
               else ('<span style="color:#9a6a00;">proposed, rejected</span>' if r.get("nvda_proposed") else 'no'))
        _star = " (current)" if r["cap"] == 5 else ""
        _mtr += (f'<tr style="border-bottom:1px solid #eee;"><td {_lc}><b>{r["cap"]}</b>{_star}</td>'
                 f'<td>{r["ret"] * 100:+.0f}%</td><td>{r["nchg"]}</td><td {_lc}>{_nv}</td>'
                 f'<td {_lc}>{", ".join(r["wl"])}</td></tr>')
    mws_html = (
        '<h2>9. max_watchlist_size sweep — does more room let the curator add NVDA?</h2>'
        '<p style="color:#555;max-width:920px;">Unlike the cap/&lambda;/lookback knobs above (free math '
        'replays on one curation set), <b>max_watchlist_size changes the curator\'s decisions</b>, so each '
        'cap is a separate re-curation (~$0.40 LLM each) on the same news pools and AAPL/GOOGL/AMZN starter. '
        'The question: with the 5-slot cap loosened, does the curator ever add NVDA, or does it keep '
        'diversifying into next-waves regardless of room? <b>Red bar = NVDA entered the watchlist.</b></p>'
        + _mbar
        + '<table style="margin-top:.6em;"><thead><tr>'
        f'<th {_lc}>max_watchlist_size</th><th>curator return</th><th>watchlist changes</th>'
        f'<th {_lc}>NVDA added?</th><th {_lc}>final watchlist (managed picks)</th>'
        f'</tr></thead><tbody>{_mtr}</tbody></table>')

    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    # Title date range: same snapshots.csv the Curator Backtest reads, so all three DBs show one range.
    _snap = ROOT / runs_dir / "_backtest" / "snapshots.csv"
    _sd = sorted({ln.split(",", 1)[0] for ln in _snap.read_text().splitlines()[1:] if ln}) if _snap.exists() else []
    _range = (f' <span style="font-size:0.55em;color:#666;font-weight:400;">&mdash; {_sd[0]} to {_sd[-1]}</span>'
              if _sd else "")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Backtest parameter sweeps</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;margin:0 auto;
padding:0 1.5em;color:#222;line-height:1.5}}h1,h2{{color:#111}}table{{border-collapse:collapse;font-size:13px;width:100%}}
th{{text-align:right;padding:6px 10px;border-bottom:2px solid #ccc;white-space:nowrap}}th:first-child{{text-align:left}}
.built{{position:absolute;top:8px;right:16px;font-size:12px;color:#888}}
</style></head><body><div class="built">dashboard built {ts}</div>{nav}
<h1>Backtest parameter sweeps{_range}</h1>
<p style="color:#555;max-width:860px;">{len(rows)} configs = concentration_cap × risk_aversion (λ) × optimizer_lookback,
replayed on the <b>fixed 3-year curation set of the default curator</b> ({runs_dir.split('/')[-1]}). These knobs touch only the
mean-variance replay, not the curator, so the whole grid costs <b>$0</b> (no LLM). Ranked by
<b>Information Ratio</b> (annualized active return ÷ tracking error vs SPY — consistency of beating the
benchmark). SPY returned {spy_ret*100:+.0f}% over the window. ★ = best in column.</p>
<p style="color:#b45309;max-width:860px;"><b>All in-sample.</b> These rank candidate configs to
<b>forward-test</b>; they don't prove an optimum. Read the <b>IR t-stat</b> (|t|&gt;2 ≈ real vs luck),
the bootstrap <b>CI</b> (error bar on annualized return), and <b>H1/H2 stable</b> (does the edge hold in
both halves) before trusting any row.</p>
{grid_html}
<h2>1. Return vs drawdown (color = IR, red ring = current config)</h2>
{scatter}
<h2>2. Return vs L1 churn</h2>
<p style="color:#555;max-width:920px;">Same configs, risk axis replaced by <b>L1 churn = annualized one-way
turnover (%/yr)</b>: how much of the portfolio is traded per year (Σ|Δweight| from share changes; drift
between rebalances is not a trade). 100%/yr = the whole book turned over once a year. This is the
<b>industry-standard</b> churn measure. Trading is <b>free in an IRA</b>, so read it as
<b>stability/robustness</b>, not cost: lower churn = less noise-chasing / less overfit, so
<b>upper-left (high return, low churn) is the sweet spot</b>. Current config: {_cur_l1:.0f}%/yr.</p>
{l1_scatter}
<h2>3. Return vs L2 course correction</h2>
<p style="color:#555;max-width:920px;">Same idea, but churn is the <b>L2 (Euclidean) path length</b> the
portfolio was dragged along through weight-space (√Σ&nbsp;Δweight² per rebalance, summed &amp;
annualized) — your &ldquo;total course-correction&rdquo;. Vs L1, it weights <b>concentrated single-name
rotations</b> more heavily. It ranks these configs almost identically to L1 (correlation
{_l1l2_corr:.2f}), which is the useful takeaway: the two norms agree, so the churn ordering is robust.
Current config: {_cur_l2:.0f}/yr.</p>
{l2_scatter}
<h2>4. All configs (ranked by IR)</h2>
{rec_html}
<details style="margin:.4em 0 .8em;"><summary style="cursor:pointer;color:#0b7285;font-weight:600;">
Show the full ranked table ({len(rows)} configs) + column definitions</summary>
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
</details>
{mws_equity_html}
{llm_html}
{llm4_html}
{llm5_html}
{mws_html}
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
    ap.add_argument("--runs-dir", default="data/curator_runs/gkg-3yr-final")  # the DEFAULT curator's curations (kimi)
    ap.add_argument("--out", default=str(ROOT / "docs" / "sweep_pwr.html"))
    ap.add_argument("--recompute", action="store_true", help="re-run the 150 backtests (else use cache)")
    a = ap.parse_args()
    build(a.runs_dir, Path(a.out), recompute=a.recompute)
