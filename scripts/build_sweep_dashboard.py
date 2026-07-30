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

# INTERIM COMPOUNDER REPOINT (titles+live geosplit proto runs, pending Wayback). Revert CAPS / MWS_SWEEP /
# NLB_SWEEP / LLM_RUNS / --runs-dir to the gkg-3yr clean runs after the clean re-curation.
CAPS = [0.1, 0.25, 0.333, 0.5, 0.667, 0.8, 1.0]   # includes the canonical 0.333
LAMBDAS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]   # FUTURE: prepend 0.1 when you want it swept (deferred per user)
LOOKBACKS = [14, 30, 60, 90, 120, 150]          # calendar days (7d dropped: infeasible — ~5 trading days can't
                                                #  estimate a covariance for a ~20-asset universe; all lb7 configs skip)
_FM = portfolio.load_financial_model()                       # single profile read (anchors + CURRENT below)
ANCHORS = _FM.get("always_include") or ["SPY", "AGG", "IAU"]  # from the profile
_RF = float(_FM["risk_free_rate"])                           # rf + exec-lag from the profile (NOT swept params)
_TU = int(portfolio.load_backtest_config()["t_update_days"])
TRACK_TICKERS = ["QUBT", "RKLB", "NVDA"]     # section 6: flag whether each mws curation ever added these

# "Recommended settings" = the risk/churn-constrained frontier read off plots 1-3: keep only configs with
# shallow drawdown AND low churn on BOTH norms, then eyeball the survivors for the best return metrics.
REC_MAX_DD, REC_MAX_L1, REC_MAX_L2 = 55.0, 1500.0, 2000.0   # |maxDD|% , L1 turnover , L2 path-length ceilings
REC_MIN_ANN = 80.0   # annualized-return floor (%) — the "must make real money" gate; a GATE, not the sort key

# LLM curator comparison (section 3): (label, run_dir, provider, $in/M, $out/M). Agreement is measured
# against the reference (first row). Add a row per model run you want to compare.
LLM_RUNS = [   # row 0 = the DEPLOYED default (kimi); "agree" is measured against it. All three are the SAME
               # geosplit pools / dates / canonical mws16 config, reasoning-OFF — only the curator model varies.
               # Primary ranking is the blind Opus judge (section 12 table), NOT agreement.
    ("moonshotai/kimi-k2.5", "data/curator_runs/proto-mws16", "OpenRouter", 0.57, 2.85),
    ("claude-sonnet-5", "data/curator_runs/proto-sonnet", "Anthropic", 3.0, 15.0),
    ("deepseek/deepseek-v4-flash", "data/curator_runs/proto-deepseek", "OpenRouter", 0.27, 0.40),
    ("claude-opus-4-8", "data/curator_runs/proto-opus", "Anthropic", 5.0, 25.0),
]
CURRENT = (float(_FM["concentration_cap"]), float(_FM["risk_aversion"]),
           int(_FM["optimizer_lookback_days"]))   # the live investor_profile.md config (cap / λ / lookback-days)
MIN_TRADE_FRACS = [0.0, 0.025, 0.05, 0.10, 0.15, 0.20]   # no-trade-band sweep (min rebalancing trade / book)
CURRENT_MT = float(_FM["min_trade_size_frac"])   # live no-trade band; the 4th swept grid axis
# The min_trade axis is swept on ONE fixed anchor mws (DECOUPLED from the canonical), so changing the canonical
# never triggers a re-sweep — the dashboard just re-flags the new live config from the already-cached grid.
MT_SWEEP_MWS = 20   # the min_trade-anchor watchlist size (already fully swept); NOT tied to the profile canonical
BLUE, GREEN, RED, GREY = "#1f77b4", "#2b8a3e", "#c92a2a", "#adb5bd"

# max_watchlist_size sweep (section 6): unlike cap/lambda/lookback, this knob changes the CURATOR's
# decisions, so each cap is a separate RE-CURATION (LLM cost), not a free replay. cap 5 = the canonical
# run; the rest are re-curated into gkg-3yr-mws{cap}. Tests whether more slots let the curator add NVDA.
MWS_SWEEP = [(4, "data/curator_runs/proto-mws4"), (6, "data/curator_runs/proto-mws6"),
             (8, "data/curator_runs/proto-mws8"), (12, "data/curator_runs/proto-mws12"),
             (16, "data/curator_runs/proto-mws16"), (20, "data/curator_runs/proto-mws20"),
             (24, "data/curator_runs/proto-mws24")]   # INTERIM: geosplit titles+live proto runs

# news_lookback_days sweep (section 7): also a CURATOR-param sweep (re-curation), but title-only (no Wayback/
# live fetch) so it curates on the preserved GKG titles at zero network cost. Each window = a separate
# re-curation into gkg-3yr-nlb{N}, all at the canonical mws=6 / cap1.0 / λ2.0 / 150d config.
NLB_SWEEP = []   # INTERIM: no news_lookback re-curations on the geosplit proto runs yet (section 12 omitted)


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
        # A run counts as complete only if it's on the CURRENT design (curations carry the _retries marker);
        # a 79/79 run WITHOUT it is stale pre-retry data awaiting re-curation, so treat it as pending.
        _fresh = bool(curs) and ("_retries" in json.loads(Path(curs[-1]).read_text()))
        if len(curs) < 79 or not bt.exists() or not _fresh:
            rows.append({"cap": cap, "pending": True}); continue
        sn = pd.read_csv(bt, parse_dates=["date"])
        tot = sn.groupby("date")["total_value"].first()
        ret = float(tot.iloc[-1] / tot.iloc[0] - 1.0)
        # The ACTUAL watchlist is what the validated backtest tracked (snapshots include every watchlist
        # ticker, even at 0 shares) — NOT a naive replay of proposed adds/removes, which double-counts
        # cap-rejected / unpaired proposals. Per-date ticker set (minus anchors) = the true watchlist.
        by_date = sn.groupby("date")["ticker"].apply(lambda s: frozenset(s) - anchors)
        # per tracked ticker: did it actually ENTER the validated watchlist (not just get proposed)?
        entered = {t: any(t in wl for wl in by_date) for t in TRACK_TICKERS}
        picks = sorted(by_date.iloc[-1])                     # final validated watchlist (managed picks)
        # Replay the proposals with THIS cap's validation to count applied vs rejected (a reject = a
        # double-add, an add to a full watchlist with no room, or a stale remove). High rejects = the
        # curator's decisions are being blocked (the symptom that exposed the cap-override bug).
        wl_r = list(starter); n_add = n_rem = n_rej = n_ret = 0
        proposed = {t: False for t in TRACK_TICKERS}
        for f in curs:
            cj = json.loads(Path(f).read_text())
            n_ret += int(cj.get("_retries", 0))              # reject-and-retry rounds the harness fired
            adds = [a["ticker"] for a in cj.get("adds", [])]; rems = [r["ticker"] for r in cj.get("removes", [])]
            for t in TRACK_TICKERS:
                if t in adds:
                    proposed[t] = True
            for t in rems:
                if t in wl_r: wl_r.remove(t); n_rem += 1
                else: n_rej += 1
            for t in adds:
                if t not in wl_r and len(wl_r) < cap: wl_r.append(t); n_add += 1
                else: n_rej += 1
        rows.append({"cap": cap, "pending": False, "ret": ret, "n_add": n_add, "n_rem": n_rem,
                     "n_rej": n_rej, "n_ret": n_ret, "nvda": entered["NVDA"],
                     "flags": {t: {"entered": entered[t], "proposed": proposed[t]} for t in TRACK_TICKERS},
                     "wl": picks})
    return rows


def _nlb_rows():
    """Per news_lookback_days window (title-only re-curations at the canonical mws=6/cap1.0/λ2.0/150d config):
    total return, IR (success), L1/L2 (churn), which tracked tickers entered, and the FULL list of tickers
    funded over the 3 years (breadth/diversity). Marks the optimal window (highest IR)."""
    import glob
    anchors = set((portfolio.load_financial_model().get("always_include") or []))
    rows = []
    spy = None
    for nlb, rd in NLB_SWEEP:
        curs = sorted(glob.glob(str(ROOT / rd / "2*-curation.json")))
        bt = ROOT / rd / "_backtest" / "snapshots.csv"
        _fresh = bool(curs) and ("_retries" in json.loads(Path(curs[-1]).read_text()))
        if len(curs) < 79 or not bt.exists() or not _fresh:
            rows.append({"nlb": nlb, "pending": True}); continue
        sn = pd.read_csv(bt, parse_dates=["date"])
        tot = sn.groupby("date")["total_value"].first().sort_index()
        ret = float(tot.iloc[-1] / tot.iloc[0] - 1.0)
        if spy is None:
            spy = portfolio._fetch_benchmark_curves(["SPY"], tot.index[0], tot.index[-1], float(tot.iloc[0]))["SPY"]
        peak = tot.cummax(); dd = float(((tot - peak) / peak).min())
        yrs = max((tot.index[-1] - tot.index[0]).days / 365.25, 1e-9); ann = (1 + ret) ** (1 / yrs) - 1
        m = _metrics(tot, spy, ann, dd); l1, l2 = _churn_metrics(sn)
        by_date = sn.groupby("date")["ticker"].apply(lambda s: frozenset(s) - anchors)
        entered = {t: any(t in wl for wl in by_date) for t in TRACK_TICKERS}
        funded = sorted(set(sn[sn["shares"] > 0]["ticker"]) - anchors)   # every ticker ever funded, 3yr union
        rows.append({"nlb": nlb, "pending": False, "ret": ret, "ir": m["ir"], "l1": l1, "l2": l2,
                     "flags": {t: {"entered": entered[t]} for t in TRACK_TICKERS}, "funded": funded})
    # optimal = highest IR among completed windows (best risk-adjusted return; for this data it also leads
    # raw return and ticker breadth with near-lowest churn).
    _done = [r for r in rows if not r.get("pending")]
    if _done:
        _best = max(_done, key=lambda r: r["ir"])
        for r in _done:
            r["optimal"] = (r is _best)
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


def _rotation_metrics(snaps: pd.DataFrame, recent_days: int = 365) -> dict:
    """Rotation-quality metrics over the FULL window and the trailing `recent_days` (forward-relevant slice):
      pf   = profit factor = (sum of positive daily position P&L) / |sum of negative daily P&L|, where daily
             P&L per ticker = shares_{t-1} * (price_t - price_{t-1}). >1 = captured more dollars on up-moves
             than given back on down-moves (rotation EFFICIENCY -- not drawdown; a high-pf config can still
             round-trip). Capped at 99 when there are ~no losing days.
      gb   = giveback = worst peak-to-trough of the equity curve (<=0). Round-trip SEVERITY.
      *_1y = pf/gb recomputed over the trailing year, plus the trailing-year total return -- the slice that
             is not dominated by the 2023-24 run-up, so it actually reflects recent rotation behavior."""
    def _pf(df):
        P = L = 0.0
        for _tk, s in df.groupby("ticker"):
            s = s.sort_values("date")
            d = (s["shares"].shift(1) * s["price"].diff()).fillna(0.0)
            P += float(d[d > 0].sum()); L += float(-d[d < 0].sum())
        return min(P / L, 99.0) if L > 1e-6 else 99.0

    def _giveback(tot):
        if len(tot) < 2:
            return float("nan")
        peak = tot.cummax()
        return float(((tot - peak) / peak.replace(0, np.nan)).min())

    tot = snaps.groupby("date")["total_value"].first().sort_index()
    cutoff = tot.index[-1] - pd.Timedelta(days=recent_days)
    rt = tot[tot.index >= cutoff]
    r1 = snaps[snaps["date"] >= cutoff]
    return {
        "pf": _pf(snaps), "gb": _giveback(tot),
        "pf_1y": _pf(r1), "gb_1y": _giveback(rt),
        "ret_1y": float(rt.iloc[-1] / rt.iloc[0] - 1.0) if len(rt) > 1 else float("nan"),
    }


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
                                         risk_free_rate=_RF, t_update_days=_TU,
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
    import glob as _glob
    import hashlib
    import json as _json
    # Plots 1-3 overlay the free grid on EVERY re-curated max_watchlist_size (each a distinct curation draw),
    # so each mws value = its own 150-config replay. cap/λ/lookback stay FREE math replays; mws is a curator
    # param, so we only include a run dir once its re-curation is COMPLETE (same curation count as canonical) —
    # an in-progress dir (e.g. cap 16 mid-sweep) is skipped until done, then folded in on the next rebuild.
    _mws_fixed = int(portfolio.load_financial_model().get("max_watchlist_size", 5))  # canonical / live watchlist
    _n_canon = len(_glob.glob(str(ROOT / runs_dir / "2*-curation.json")))
    READY_MWS = [(m, d) for (m, d) in MWS_SWEEP
                 if _n_canon > 0 and len(_glob.glob(str(ROOT / d / "2*-curation.json"))) == _n_canon]
    cache_p = ROOT / "data" / "curator_runs" / "_sweep_cache.json"
    # CURRENT / _mws_fixed are DELIBERATELY excluded from the key: the grid metrics (ir/ret/dd/l1/l2) are
    # config-independent, so a live-config change reuses the cache and only re-flags `cur` (set post-load below).
    # min_trade is swept only on the canonical mws (_mws_fixed), so both the frac list and which mws is
    # canonical enter the key; non-canonical mws replay at the live CURRENT_MT (held, not swept).
    # PER-CONFIG cache: each config's metrics are keyed on its own (mws,cap,λ,lookback,mt) tuple, so
    # EXPANDING the grid (new caps/λ/lookbacks/min_trades) recomputes ONLY the new configs and reuses every
    # already-cached one (an INCREMENTAL sweep). `formula`+`runs_dir` invalidate the whole cache if the metric
    # code or the underlying curations change; a legacy cache (missing those fields) is trusted once. --recompute
    # forces a full re-sweep. `cur`/CURRENT are excluded (display flag, re-set post-load below).
    FORMULA_VERSION = "clean-geosplit-v1"
    _cached, _skipped, spy_ret = {}, set(), None
    if not recompute and cache_p.exists():
        try:
            c = _json.loads(cache_p.read_text())
            if c.get("runs_dir", runs_dir) == runs_dir and c.get("formula", FORMULA_VERSION) == FORMULA_VERSION:
                spy_ret = c.get("spy_ret")
                for r in c.get("rows", []):
                    _cached[(r["mws"], r["cap"], r["lam"], r["lb"], r.get("mt", CURRENT_MT))] = r
                _skipped = {tuple(k) for k in c.get("skipped", [])}   # remember infeasible corners
        except Exception:  # noqa: BLE001
            pass
    rows_all, spy_curve, _n_new, _n_skip = [], None, 0, 0
    for _m, _mdir in READY_MWS:
        # min_trade_size_frac is the 4th swept axis but ONLY on the fixed anchor MT_SWEEP_MWS (decoupled from the
        # canonical, so a canonical change never re-sweeps); every other mws replays at the live CURRENT_MT (held).
        _mts = MIN_TRADE_FRACS if _m == MT_SWEEP_MWS else [CURRENT_MT]
        for cap in CAPS:
            for lam in LAMBDAS:
                for lb in LOOKBACKS:
                    for mt in _mts:
                        _ck = (_m, cap, lam, lb, mt)
                        if _ck in _cached:                 # incremental: reuse an already-swept config
                            rows_all.append(_cached[_ck])
                            continue
                        if _ck in _skipped:                # known-infeasible corner: don't re-attempt each render
                            _n_skip += 1
                            continue
                        _tag = f"{_m}_{cap}_{lam}_{lb}_{mt}"
                        try:
                            res = portfolio.curator_backtest(
                                runs_dir=_mdir, out_dir=f"/tmp/_sweep/{_tag}",
                                max_weight=cap, risk_aversion=lam, risk_free_rate=_RF, t_update_days=_TU,
                                benchmarks=["SPY"], lookback_years_override=lb / 365.0, always_include=ANCHORS,
                                min_trade_frac=mt)
                            snaps = pd.read_csv(Path(f"/tmp/_sweep/{_tag}") / "snapshots.csv", parse_dates=["date"])
                        except Exception:  # noqa: BLE001 — infeasible corner (e.g. cap 0.1 with too few names to
                            _skipped.add(_ck)   # reach 100%, or a 7d lookback with degenerate covariance): remember
                            _n_skip += 1        # it so future renders skip it instead of re-attempting
                            continue
                        totals = snaps.groupby("date")["total_value"].first().sort_index()
                        if spy_curve is None:   # identical window across all mws dirs -> compute SPY once
                            spy_curve = portfolio._fetch_benchmark_curves(["SPY"], totals.index[0], totals.index[-1],
                                                                          float(totals.iloc[0]))["SPY"]
                        m = _metrics(totals, spy_curve, res["annualized_return"], res["max_drawdown"])
                        _l1, _l2 = _churn_metrics(snaps)
                        rows_all.append({"mws": _m, "cap": cap, "lam": lam, "lb": lb, "mt": mt,
                                         "ret": res["realized_return"],
                                         "ann": res["annualized_return"], "dd": res["max_drawdown"],
                                         "l1": _l1, "l2": _l2, **m, **_rotation_metrics(snaps),
                                         "cur": (cap, lam, lb) == CURRENT and mt == CURRENT_MT and _m == _mws_fixed})
                        _n_new += 1
    if spy_ret is None and spy_curve is not None:
        spy_ret = float(spy_curve.iloc[-1] / spy_curve.iloc[0] - 1.0)
    print(f"  incremental sweep: {_n_new} new computed, {len(rows_all) - _n_new} reused, "
          f"{_n_skip} skipped (infeasible corner) -> {len(rows_all)} configs")
    cache_p.parent.mkdir(parents=True, exist_ok=True)
    cache_p.write_text(_json.dumps({"formula": FORMULA_VERSION, "runs_dir": runs_dir,
                                    "spy_ret": spy_ret, "rows": rows_all,
                                    "skipped": [list(k) for k in _skipped]}))
    # `cur` marks the live-config point. It is a DISPLAY flag, not grid data (which is config-independent
    # and cached), so set it here — this reflects the current profile / CURRENT even on a cache hit.
    for r in rows_all:
        r["cur"] = ((r["cap"], r["lam"], r["lb"]) == CURRENT and r.get("mt", CURRENT_MT) == CURRENT_MT
                    and r["mws"] == _mws_fixed)
    # The ranking table / recommended-settings / plots 4+ stay on the CANONICAL watchlist (mws == _mws_fixed);
    # only plots 1-3 use the full rows_all overlay across every ready max_watchlist_size.
    rows = [r for r in rows_all if r["mws"] == _mws_fixed]
    _grid3d = len(CAPS) * len(LAMBDAS) * len(LOOKBACKS)   # per-mws cloud size (cap×λ×lookback, one min_trade)
    _plotted = len(sorted({r["mws"] for r in rows_all})) * _grid3d   # points shown in plots 1-3 (mt pinned to live)


    # plots 1-3: ann return vs (drawdown | L1 | L2), one colored cloud PER max_watchlist_size (each its own
    # 150-config free-grid replay), current config red-ringed. Coloring by mws (not IR) makes the curation-draw
    # spread visible: cap/λ/lookback move a point WITHIN a cloud deterministically; jumping clouds also changes
    # the underlying curation, so cross-color separation mixes the mws effect with kimi draw noise.
    import plotly.graph_objects as go
    # discrete, distinct palette keyed by watchlist size (grey fallback for any unlisted size); 5 = live = green
    MWS_COLORS = {2: "#8c564b", 3: "#1f77b4", 4: "#17becf", 5: "#2b8a3e", 6: "#bcbd22",
                  7: "#ff7f0e", 8: "#d62728", 10: "#9467bd", 12: "#e377c2",
                  16: "#ff7f0e", 20: "#9467bd", 24: "#8c564b"}   # compounder sizes (were grey fallback)
    _mws_present = sorted({r["mws"] for r in rows_all})

    def _mws_scatter(xfn, xtitle, xhover, first):
        f = go.Figure()
        for _m in _mws_present:
            # min_trade is swept only on the canonical mws; pin it to the live value here so each mws is one
            # 3-D cloud (cap/λ/lookback), not a denser mt-smeared blob for the canonical size.
            sub = [r for r in rows_all if r["mws"] == _m and r.get("mt", CURRENT_MT) == CURRENT_MT]
            f.add_trace(go.Scatter(
                x=[xfn(r) for r in sub], y=[r["ann"] * 100 for r in sub], mode="markers",
                name=f"{_m}" + (" ★" if _m == _mws_fixed else ""),
                marker={"size": [16 if r["cur"] else 8 for r in sub],
                        "color": MWS_COLORS.get(_m, "#adb5bd"), "opacity": 0.75,
                        "line": {"width": [3 if r["cur"] else 0 for r in sub], "color": "#e03131"}},
                text=[f"watchlist {_m} · cap {r['cap']} / λ {r['lam']} / {r['lb']}d"
                      f"<br>IR {r['ir']:+.2f}, Calmar {r['calmar']:.2f}"
                      + (" · CURRENT" if r["cur"] else "") for r in sub],
                hovertemplate="%{text}<br>ann %{y:.0f}%, " + xhover + "<extra></extra>"))
        # live-config overlay: a distinct black star drawn ON TOP so the current setting is unmistakable
        # (a red ring on one point inside its cloud is invisible at this density).
        _curr = next((r for r in rows_all if r["cur"]), None)
        if _curr is not None:
            f.add_trace(go.Scatter(
                x=[xfn(_curr)], y=[_curr["ann"] * 100], mode="markers", name="live config",
                marker={"symbol": "star", "size": 20, "color": "#111", "line": {"width": 1.5, "color": "#fff"}},
                text=[f"LIVE CONFIG · ws {_curr['mws']} · cap {_curr['cap']} / λ {_curr['lam']} / {_curr['lb']}d"
                      f"<br>IR {_curr['ir']:+.2f}, Calmar {_curr['calmar']:.2f}"],
                hovertemplate="%{text}<br>ann %{y:.0f}%, " + xhover + "<extra></extra>"))
        f.update_layout(template="seaborn", height=460, margin={"t": 20, "l": 60, "r": 140},
                        xaxis={"title": xtitle}, yaxis={"title": "annualized return %"},
                        legend={"title": {"text": "watchlist<br>size (★=live)"}, "x": 1.02, "xanchor": "left",
                                "y": 1, "yanchor": "top"})
        return f.to_html(full_html=False, include_plotlyjs=("cdn" if first else False),
                         config={"displayModeBar": False})
    scatter = _mws_scatter(lambda r: abs(r["dd"]) * 100, "max drawdown (|%|) — risk →", "maxDD -%{x:.0f}%", True)
    # plots 2 & 3: return vs churn, two norms. UPPER-LEFT (high return, low churn) = the stable, less-overfit
    # ideal. L1 = turnover (industry standard); L2 = course-correction (Euclidean path length, emphasizes
    # concentrated rotations).
    l1_scatter = _mws_scatter(lambda r: r["l1"], "L1 churn — annualized one-way turnover (%/yr) → more trading",
                              "L1 %{x:.0f}", False)
    l2_scatter = _mws_scatter(lambda r: r["l2"], "L2 course-correction — annualized weight-space path length → more rotation",
                              "L2 %{x:.0f}", False)
    _cur_l1 = next((r["l1"] for r in rows if r["cur"]), float("nan"))
    _cur_l2 = next((r["l2"] for r in rows if r["cur"]), float("nan"))
    import numpy as _np2
    _l1l2_corr = float(_np2.corrcoef([r["l1"] for r in rows], [r["l2"] for r in rows])[0, 1])

    nav = dash_nav.render("sweep_pwr.html", built=False)   # this page renders its own "dashboard built" stamp
    _fmt = lambda xs: ", ".join(str(x) for x in xs)  # noqa: E731
    grid_html = (
        '<h2>Parameter settings</h2>'
        f'<p style="color:#555;max-width:860px;">The four FREE swept knobs (every combination on the canonical '
        f'watchlist = {len(rows)} configs) and the values considered. min_trade_size_frac is swept only on the '
        f'canonical watchlist (mws&nbsp;{_mws_fixed}); every other size is held at its live value. All other '
        'optimizer / backtest params are held at the <code>investor_profile.md</code> values — these knobs only '
        're-weight the <b>same</b> curations.</p>'
        '<table style="font-size:13px;margin-bottom:.6em;"><thead><tr>'
        '<th style="text-align:left">parameter</th><th style="text-align:left">values swept</th>'
        '<th style="text-align:left">current (profile)</th></tr></thead><tbody>'
        f'<tr><td style="text-align:left">concentration_cap</td><td style="text-align:left">{_fmt(CAPS)}</td><td style="text-align:left">{CURRENT[0]}</td></tr>'
        f'<tr><td style="text-align:left">risk_aversion (λ)</td><td style="text-align:left">{_fmt(LAMBDAS)}</td><td style="text-align:left">{CURRENT[1]}</td></tr>'
        f'<tr><td style="text-align:left">optimizer_lookback (days)</td><td style="text-align:left">{_fmt(LOOKBACKS)}</td><td style="text-align:left">{CURRENT[2]}</td></tr>'
        f'<tr><td style="text-align:left">min_trade_size_frac <span style="color:#0b7285;">(canonical mws)</span></td>'
        f'<td style="text-align:left">{_fmt(MIN_TRADE_FRACS)}</td><td style="text-align:left">{CURRENT_MT:g}</td></tr>'
        f'<tr><td style="text-align:left">max_watchlist_size <span style="color:#9a6a00;">(re-curation, §11)</span></td>'
        f'<td style="text-align:left">{_fmt([c for c, _ in MWS_SWEEP])}</td><td style="text-align:left">{_mws_fixed}</td></tr>'
        # news_lookback_days is a re-curation sweep (§12); shown only when it is actually swept. Currently the
        # compounder run holds it at the profile value (no nlb re-curations yet), so the row is omitted.
        + (f'<tr><td style="text-align:left">news_lookback_days <span style="color:#9a6a00;">(re-curation, §12)</span></td>'
           f'<td style="text-align:left">{_fmt([n for n, _ in NLB_SWEEP])}</td>'
           f'<td style="text-align:left">{int(portfolio.load_financial_model().get("news_lookback_days", 21))}</td></tr>'
           if NLB_SWEEP else '')
        + '</tbody></table>')

    # recommended-settings: the risk/churn-constrained frontier from plots 1-3. Keep only configs with shallow
    # drawdown AND low churn on BOTH norms (|maxDD| < REC_MAX_DD, L1 < REC_MAX_L1, L2 < REC_MAX_L2), then list the
    # survivors in ONE table sorted by IR so the best return metrics rise to the top. ★ marks the best value in
    # each performance column AMONG the survivors (eyeball aid), not a separate ranking per metric.
    _cur_row = next(r for r in rows_all if r["cur"])
    _lc = 'style="text-align:left"'
    # (label, cell-format, value fn); maxDD ranks by least-negative (higher r['dd'] = shallower loss = better)
    # PLATEAU robustness: score each config by how well its GRID NEIGHBORS also do (same mws, one ±1 step in
    # cap / λ / lookback / min_trade). A lone in-sample spike (great alone, weak neighbors) ranks BELOW a broad
    # high plateau (robust to parameter mis-specification -> more likely to hold FORWARD). The anti-overfit lever
    # for choosing a config from a grid. plateau = ½·own IR + ½·mean(neighbor IR).
    _by_key = {(r["mws"], r["cap"], r["lam"], r["lb"], r.get("mt", CURRENT_MT)): r for r in rows_all}
    _axes = [CAPS, LAMBDAS, LOOKBACKS, MIN_TRADE_FRACS]   # positions 1..4 of the key (cap,λ,lb,mt)
    for r in rows_all:
        _key = (r["mws"], r["cap"], r["lam"], r["lb"], r.get("mt", CURRENT_MT))
        _nb = []
        for _ai, _vals in enumerate(_axes):
            _v = _key[_ai + 1]
            _pos = _vals.index(_v) if _v in _vals else -1
            if _pos < 0:
                continue
            for _j in (_pos - 1, _pos + 1):
                if 0 <= _j < len(_vals):
                    _nk = list(_key); _nk[_ai + 1] = _vals[_j]
                    _n = _by_key.get(tuple(_nk))
                    if _n is not None and _n["ir"] == _n["ir"]:
                        _nb.append(_n["ir"])
        _own = r["ir"] if r["ir"] == r["ir"] else None
        r["plateau"] = ((0.5 * _own + 0.5 * sum(_nb) / len(_nb)) if _nb else _own) if _own is not None else float("nan")
    _MET = [("plateau", "{:+.2f}", lambda r: r["plateau"]),
            ("IR", "{:+.2f}", lambda r: r["ir"]),
            ("Gain/Pain", "{:.2f}", lambda r: r.get("pf", float("nan")) - 1.0),  # GPR = profit factor - 1 (Schwager)
            ("t-stat", "{:+.1f}", lambda r: r["tstat"]),
            ("Sharpe", "{:.2f}", lambda r: r["sharpe"]),
            ("Calmar", "{:.2f}", lambda r: r["calmar"]),
            ("ann ret%", "{:+.0f}%", lambda r: r["ann"] * 100),
            ("maxDD", "{:.0f}%", lambda r: r["dd"] * 100)]
    _passed = [r for r in rows_all
               if abs(r["dd"]) * 100 < REC_MAX_DD and r["l1"] < REC_MAX_L1 and r["l2"] < REC_MAX_L2
               and r["ann"] * 100 > REC_MIN_ANN]
    _passed.sort(key=lambda r: -(r["plateau"] if r["plateau"] == r["plateau"] else -9e9))
    # best-in-column among survivors (all six value fns are "higher = better", maxDD included), for the ★
    _colbest = {_m: max(_passed, key=lambda r, fn=_fn: (fn(r) if fn(r) == fn(r) else -9e9))
                for _m, _, _fn in _MET} if _passed else {}

    def _cells(r):
        out = ""
        for _m, _f, _fn in _MET:
            _v = _fn(r)
            _vs = _f.format(_v) if _v == _v else "n/a"
            _st = " ★" if _colbest.get(_m) is r else ""
            out += f'<td {_lc}>{_vs}{_st}</td>'
        return out

    _hdr = (f'<tr><th {_lc}>#</th><th {_lc}>max_watchlist_size</th><th {_lc}>concentration_cap</th>'
            f'<th {_lc}>λ</th><th {_lc}>lookback</th><th {_lc}>min_trade</th>'
            + "".join(f'<th {_lc}>{_m}</th>' for _m, _, _ in _MET)
            + f'<th {_lc}>L1</th><th {_lc}>L2</th></tr>')
    _body = ""
    for _i, r in enumerate(_passed):
        _hl = "background:#fff7e6;" if r["cur"] else ""
        _live = " &larr; live" if r["cur"] else ""
        _body += (f'<tr style="{_hl}border-bottom:1px solid #eee;"><td {_lc}>{_i + 1}</td>'
                  f'<td {_lc}>{r["mws"]}</td><td {_lc}>{r["cap"]:.2f}</td><td {_lc}>{r["lam"]:.1f}</td>'
                  f'<td {_lc}>{r["lb"]}d</td><td {_lc}>{r.get("mt", CURRENT_MT):g}{_live}</td>{_cells(r)}'
                  f'<td {_lc}>{r["l1"]:.0f}</td><td {_lc}>{r["l2"]:.0f}</td></tr>')
    _mws_survivors = ", ".join(f"{_m}:{sum(1 for r in _passed if r['mws'] == _m)}"
                               for _m in sorted({r["mws"] for r in _passed})) if _passed else "none"
    _cur_fail = ([f'maxDD {abs(_cur_row["dd"])*100:.0f}% &ge; {REC_MAX_DD:.0f}'] * (abs(_cur_row["dd"]) * 100 >= REC_MAX_DD)
                 + [f'L1 {_cur_row["l1"]:.0f} &ge; {REC_MAX_L1:.0f}'] * (_cur_row["l1"] >= REC_MAX_L1)
                 + [f'L2 {_cur_row["l2"]:.0f} &ge; {REC_MAX_L2:.0f}'] * (_cur_row["l2"] >= REC_MAX_L2))
    _cur_pass = not _cur_fail
    rec_html = (
        f'<p style="color:#555;font-size:12px;max-width:940px;margin:.2em 0 .5em;">Read straight off plots 1-3: '
        f'keep only configs in the safe corner &mdash; <b>|maxDD| &lt; {REC_MAX_DD:.0f}% AND L1 &lt; {REC_MAX_L1:.0f} '
        f'AND L2 &lt; {REC_MAX_L2:.0f} AND ann &gt; {REC_MIN_ANN:.0f}%</b> (shallow drawdown, low churn, real return). '
        f'<b>{len(_passed)} of {len(rows_all)}</b> configs survive (by watchlist size &mdash; {_mws_survivors}), '
        'listed once, sorted by <b>plateau</b> = ½·own&nbsp;IR + ½·mean(grid-neighbor&nbsp;IR) over the config&#39;s '
        '&plusmn;1-step cap/&lambda;/lookback/min_trade neighbors. A lone in-sample spike (high IR, weak neighbors) '
        'sinks below a broad high <b>plateau</b> &mdash; the anti-overfit rank, since a robust region is likelier to '
        'hold FORWARD than a fragile peak. <b>Gain/Pain</b> = Schwager&#39;s Gain-to-Pain Ratio (total gain &divide; sum '
        'of all down-moves = profit&nbsp;factor&nbsp;&minus;&nbsp;1); unlike Calmar (worst single drawdown) it counts '
        '<b>every</b> loss, so it rewards greatest-gains-with-fewest-losses. ★ = best in that column among the survivors '
        '(eyeball the top plateau / IR / Gain-Pain / t-stat / Sharpe / Calmar / ann / shallowest-DD). <b>min_trade_size_frac</b> is swept as a '
        f'4th axis on the canonical watchlist (mws&nbsp;{_mws_fixed}); every other size is held at the live '
        f'{CURRENT_MT:g}. These stay in-sample &mdash; a forward-test shortlist, not an auto-switch. Note the '
        '<b>live config</b> (ws&nbsp;'
        f'{_cur_row["mws"]} {_cur_row["cap"]:.2f}/{_cur_row["lam"]:.1f}/{_cur_row["lb"]}d/mt&nbsp;{_cur_row.get("mt", CURRENT_MT):g}, dd '
        f'{abs(_cur_row["dd"])*100:.0f}% / L1&nbsp;{_cur_row["l1"]:.0f} / L2&nbsp;{_cur_row["l2"]:.0f}) '
        + ("<b>passes</b> and is listed below (highlighted)." if _cur_pass
           else f'is <b>excluded</b> &mdash; fails {", ".join(_cur_fail)}.') + '</p>'
        '<details style="margin:.2em 0 .6em;"><summary style="cursor:pointer;color:#0b7285;font-weight:600;">'
        f'Show the recommended-settings table ({len(_passed)} configs)</summary>'
        f'<table style="font-size:12.5px;margin:.3em 0 .4em;"><thead>{_hdr}</thead><tbody>{_body}</tbody></table>'
        '</details>')

    # section 12: curator-MODEL comparison. Same geosplit pools / dates / canonical mws16 config, reasoning-OFF
    # for every model; the ONLY variable is the curator LLM. Ordered by the blind Opus soundness judge (the one
    # leak-free ranking), with backtest KPIs (in-sample, secondary) + agreement + est cost alongside.
    def _c2(v, fmt):
        return f'<td {_lc}>{fmt.format(v) if v == v else "n/a"}</td>'
    _llm = _llm_rows(CURRENT)
    _llm_by = {r["label"]: r for r in _llm if not r.get("pending")}
    _price = {lab: (pin, pout) for lab, _d, _p, pin, pout in LLM_RUNS}   # for the est $/run column
    _DEPLOYED = "moonshotai/kimi-k2.5"                                   # the model the live curator uses
    # blind Opus soundness judge (the PRIMARY ranking): data/curator_runs/_judge_scores.json
    _J, _JCRIT = {}, ["on_thesis", "evidence_supports", "real_catalyst", "disciplined", "valid_ticker"]
    _jf = ROOT / "data" / "curator_runs" / "_judge_scores.json"
    _jmeta = {}
    if _jf.exists():
        _JJ = json.loads(_jf.read_text())
        _J, _JCRIT, _jmeta = _JJ["models"], _JJ["criteria"], _JJ
    _crlab = {"on_thesis": "on-thesis", "evidence_supports": "evidence", "real_catalyst": "catalyst",
              "disciplined": "discipline", "valid_ticker": "valid-ticker"}
    def _estcost(L):   # est LLM $/run: ~24k in / 1.3k out per call x 79 dates, at the model's OpenRouter/Anthropic rate
        pin, pout = _price.get(L, (float("nan"), float("nan")))
        return 79 * (24000 * pin + 1300 * pout) / 1e6 if pin == pin else float("nan")
    # scorecard order: judge soundness desc (fall back to return if a model lacks a judge score)
    _order = sorted(_llm_by, key=lambda L: -((_J.get(L, {}) or {}).get("mean_overall") or _llm_by[L].get("ret", -9)))
    # plot 12 order: cheapest curator first (left) -> most expensive (right)
    _cost_order = sorted(_llm_by, key=lambda L: _estcost(L) if _estcost(L) == _estcost(L) else 9e9)

    model_html = ""
    if len(_llm_by) >= 2:
        import plotly.graph_objects as _lgo
        # plot 12: total return vs curator model, x ordered by curator cost (deployed kimi flagged green)
        _lfig = _lgo.Figure(_lgo.Bar(
            x=[L.split("/")[-1] for L in _cost_order], y=[_llm_by[L]["ret"] * 100 for L in _cost_order],
            marker_color=[GREEN if L == _DEPLOYED else BLUE for L in _cost_order],
            text=["deployed" if L == _DEPLOYED else "" for L in _cost_order], textposition="outside",
            customdata=[_estcost(L) for L in _cost_order],
            hovertemplate="%{x}: %{y:+.0f}% return (~$%{customdata:.1f}/run)<extra></extra>"))
        _lfig.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 60, "r": 20},
                            xaxis={"title": "curator model (cheapest → most expensive)"},
                            yaxis={"title": "curator total return %"})
        _lbar = _lfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})

        _scorecard = ""
        for i, L in enumerate(_order):
            r = _llm_by[L]
            j = _J.get(L, {}) or {}
            est = _estcost(L)
            bg = "background:#eef7ee;" if i == 0 else ("background:#fafafa;" if i % 2 else "")
            tag = " <span style='color:#2b8a3e;'>(deployed)</span>" if L == _DEPLOYED else ""
            _scorecard += (
                f'<tr style="{bg}border-bottom:1px solid #eee;"><td {_lc}><b>{L.split("/")[-1]}</b>{tag}</td>'
                f'<td {_lc}>{r["prov"]}</td><td {_lc}>{r["nadd"]} / {r["nrem"]}</td>'
                + _c2(j.get("mean_overall"), "{:.2f}") + _c2(j.get("dispersion"), "±{:.2f}")
                + "".join(_c2((j.get(k) or 0) * 100, "{:.0f}%") if j else '<td {_lc}>n/a</td>' for k in _JCRIT)
                + _c2(r["agree"] * 100, "{:.0f}%")
                + _c2(r["ret"] * 100, "{:+.0f}%") + _c2(r["dd"] * 100, "{:.0f}%")
                + _c2(r["calmar"], "{:.2f}") + _c2(r["ir"], "{:+.2f}") + _c2(est, "~${:.1f}") + "</tr>")
        _njudged = _jmeta.get("n_decisions", "?")
        _panel = _jmeta.get("panel_judges", [])
        _panel_lbl = ", ".join(p.split("/")[-1] for p in _panel) or "3-judge panel"
        _xchk = (_jmeta.get("crosscheck_judge") or "opus").split("/")[-1]
        _agree = "identical" if _jmeta.get("panel_rank") == _jmeta.get("crosscheck_rank") else "similar"
        model_html = (
            '<h2>12. Total return vs curator model</h2>'
            '<p style="color:#555;max-width:940px;">The same news pools, dates, and canonical config '
            f'(mws&nbsp;{_mws_fixed} / cap&nbsp;{CURRENT[0]} / &lambda;&nbsp;{CURRENT[1]} / {CURRENT[2]}d), '
            'reasoning-OFF for every model &mdash; the <b>only</b> variable is the curator LLM. The bar is each '
            'model&#39;s in-sample backtest return (deployed kimi in green). But return here is <b>hindsight-'
            'leaky</b> (the curator could have memorized which 2023&ndash;2026 names later won), so the '
            'scorecard below ranks by the one metric that owes nothing to hindsight: blind soundness judging.</p>'
            + _lbar
            + '<h2 style="margin-top:1.4em;">Curator-model scorecard</h2>'
            '<p style="color:#555;max-width:940px;">Rows ordered by <b>soundness</b> = the mean of a '
            f'<b>3-judge non-family panel</b> ({_panel_lbl}) &mdash; none shares a vendor with any candidate, so '
            'no self-preference. Each judge reads every add/remove <b>blind</b> (model identity stripped, '
            'decisions shuffled, the ticker&#39;s later price never shown) and grades whether the rationale + '
            'cited news justified the call <i>at the time</i>: a holistic 1&ndash;5 plus five pass/fail criteria. '
            '<b>±disp</b> = stdev across the three judges (how much they disagree). This is leak-free. As a '
            f'cross-check, an independent <b>{_xchk}</b> judge (not in the panel) ranks the models '
            f'<b>{_agree}</b>ly. The backtest columns (return / maxDD / Calmar / IR) are <b>in-sample, '
            'secondary</b>; <b>agree</b> = share of weeks the model made the identical call as deployed kimi; '
            f'<b>~$/run</b> = estimated LLM cost of a full {len(_order) and 79}-date curate. {_njudged} decisions '
            f'judged x {len(_panel) + 1} judges'
            + (f', ~${_jmeta.get("cost_usd"):.2f}.' if _jmeta.get("cost_usd") else '.') + '</p>'
            '<div style="overflow-x:auto;"><table><thead><tr>'
            f'<th style="text-align:left">model</th><th style="text-align:left">provider</th>'
            f'<th {_lc}>decisions<br>(add/rem)</th><th {_lc}>soundness<br>(1-5)</th><th {_lc}>±disp</th>'
            + "".join(f'<th {_lc}>{_crlab[k]}</th>' for k in _JCRIT)
            + f'<th {_lc}>agree<br>vs kimi</th><th {_lc}>total<br>ret</th><th {_lc}>maxDD</th>'
            f'<th {_lc}>Calmar</th><th {_lc}>IR</th><th {_lc}>~$/run</th>'
            f'</tr></thead><tbody>{_scorecard}</tbody></table></div>'
            '<p style="color:#666;font-size:12px;max-width:940px;line-height:1.6;margin:.5em 0 .3em;">'
            'Criteria (share of a model&#39;s decisions passing each): <b>on-thesis</b> (maps to a named wave) · '
            '<b>evidence</b> (cited news actually supports the claim) · <b>catalyst</b> (concrete milestone, not '
            'noise) · <b>discipline</b> (buildup-not-crest; a remove is justified, not churn) · <b>valid-ticker</b> '
            '(real investable US listing). Soundness is the only leak-free ranking on this page.</p>')

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
    llm4_html = (('<h2>13. Portfolio value over time — by curator model (vs buy/hold and SPY)</h2>'
                  '<p style="color:#555;max-width:920px;">Each LLM\'s realized portfolio value on the same pools '
                  'and profile config, alongside the equal-weight buy/hold starter and SPY. Same idea as the '
                  'curator DB\'s plot 1, without the rebalance markers.</p>'
                  + _fig4.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))
                 if (_curved and len(LLM_RUNS) >= 2) else "")

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
    mws_equity_html = (('<h2>10. Portfolio value over time — by max_watchlist_size (vs buy/hold and SPY)</h2>'
                        '<p style="color:#555;max-width:920px;">Each re-curated watchlist cap\'s realized '
                        'portfolio value on the same pools and starter — the equity-curve view of the '
                        'section-9 sweep. Tighter (smaller) watchlists concentrate into the top picks; wider '
                        'ones dilute across more next-wave names.</p>'
                        + _mwsfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))
                       if _mws_curves else "")

    # (The standalone blind-judge section was folded into §12's Curator-model scorecard above, which now
    # carries the 3-judge panel soundness + dispersion + Opus cross-check; no separate section is rendered.)

    # section 6: max_watchlist_size sweep (non-zero-cost: each cap is a re-curation). Does more room let
    # the curator add NVDA? Table (cap / return / #changes / NVDA? / final watchlist) + a return-vs-cap bar.
    _mws = _mws_rows()
    _done = [r for r in _mws if not r.get("pending")]
    if _done:
        import plotly.graph_objects as _mgo
        _mfig = _mgo.Figure(_mgo.Bar(
            x=[r["cap"] for r in _done], y=[r["ret"] * 100 for r in _done],
            marker_color=[GREEN if r["cap"] == _mws_fixed else BLUE for r in _done],   # live mws highlighted
            text=["live" if r["cap"] == _mws_fixed else "" for r in _done], textposition="outside",
            hovertemplate="watchlist %{x}: %{y:+.0f}%<extra></extra>"))
        _mfig.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 60, "r": 20},
                            xaxis={"title": "max_watchlist_size", "dtick": 1},
                            yaxis={"title": "curator total return %"})
        _mbar = _mfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
    else:
        _mbar = ""
    def _flag_cell(fl):                        # yes (entered) / proposed-rejected / no, per tracked ticker
        if fl.get("entered"): return '<b style="color:#c92a2a;">yes</b>'
        if fl.get("proposed"): return '<span style="color:#9a6a00;">proposed, rejected</span>'
        return 'no'
    _mtr = ""
    for r in _mws:
        if r.get("pending"):
            _mtr += (f'<tr style="color:#999;"><td {_lc}>{r["cap"]}</td>'
                     f'<td {_lc} colspan="{6 + len(TRACK_TICKERS)}"><i>re-curation in progress…</i></td></tr>')
            continue
        _flags = "".join(f'<td {_lc}>{_flag_cell(r["flags"][t])}</td>' for t in TRACK_TICKERS)
        _star = " (current)" if r["cap"] == _mws_fixed else ""
        # flag a nonzero reject count in amber — healthy runs should have ~0 (the cap-bug symptom)
        _rej = (f'<span style="color:#9a6a00;">{r["n_rej"]}</span>' if r["n_rej"] > 2 else str(r["n_rej"]))
        _mtr += (f'<tr style="border-bottom:1px solid #eee;"><td {_lc}><b>{r["cap"]}</b>{_star}</td>'
                 f'<td>{r["ret"] * 100:+.0f}%</td><td>{r["n_add"]}</td><td>{r["n_rem"]}</td><td>{_rej}</td>'
                 f'<td>{r.get("n_ret", 0)}</td>{_flags}<td {_lc}>{", ".join(r["wl"])}</td></tr>')
    mws_html = (
        '<h2>11. max_watchlist_size sweep</h2>'
        '<p style="color:#555;max-width:920px;">Unlike the cap/&lambda;/lookback knobs above (free math '
        'replays on one curation set), <b>max_watchlist_size changes the curator\'s decisions</b>, so each '
        'cap is a separate re-curation (~$0.40 LLM each) on the same news pools and AAPL/GOOGL/AMZN starter. '
        'The bar is each curation&#39;s total return; the live watchlist size is flagged in green.</p>'
        + _mbar)

    # section 7: news_lookback_days sweep — title-only re-curation (no Wayback/live), curates the preserved
    # GKG titles at the canonical mws6/cap1.0/λ2.0/150d config. Bar = each window's return; ticker flags as §11.
    _nlb = _nlb_rows()
    _live_nlb = int(portfolio.load_financial_model().get("news_lookback_days", 21))
    _ndone = [r for r in _nlb if not r.get("pending")]
    _opt_nlb = next((r["nlb"] for r in _ndone if r.get("optimal")), None)
    if _ndone:
        import plotly.graph_objects as _ngo
        # green = optimal (best IR); the live window is red-outlined
        _nfig = _ngo.Figure(_ngo.Bar(
            x=[str(r["nlb"]) for r in _ndone], y=[r["ret"] * 100 for r in _ndone],
            marker={"color": [GREEN if r.get("optimal") else BLUE for r in _ndone],
                    "line": {"width": [3 if r["nlb"] == _live_nlb else 0 for r in _ndone], "color": "#e03131"}},
            text=["optimal" if r.get("optimal") else ("live" if r["nlb"] == _live_nlb else "") for r in _ndone],
            textposition="outside",
            hovertemplate="news_lookback %{x}d: %{y:+.0f}%<extra></extra>"))
        _nfig.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 60, "r": 20},
                            xaxis={"title": "news_lookback_days", "type": "category"},
                            yaxis={"title": "curator total return %"})
        _nbar = _nfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
    else:
        _nbar = ""
    _ntr = ""
    for r in _nlb:
        if r.get("pending"):
            _ntr += (f'<tr style="color:#999;"><td {_lc}>{r["nlb"]}</td>'
                     f'<td {_lc} colspan="{5 + len(TRACK_TICKERS)}"><i>title-only re-curation in progress…</i></td></tr>')
            continue
        _flags = "".join(f'<td {_lc}>{_flag_cell(r["flags"][t])}</td>' for t in TRACK_TICKERS)
        _tag = (" ★ optimal" if r.get("optimal") else "") + (" (live)" if r["nlb"] == _live_nlb else "")
        _bg = "background:#eaf7ea;" if r.get("optimal") else ("background:#fff7e6;" if r["nlb"] == _live_nlb else "")
        _ntr += (f'<tr style="{_bg}border-bottom:1px solid #eee;"><td {_lc}><b>{r["nlb"]}</b>{_tag}</td>'
                 f'<td>{r["ret"] * 100:+.0f}%</td><td>{r["ir"]:+.2f}</td><td>{r["l1"]:.0f}</td><td>{r["l2"]:.0f}</td>'
                 f'{_flags}<td {_lc}>{", ".join(r["funded"])}</td></tr>')
    nlb_html = "" if not NLB_SWEEP else (
        '<h2>12. news_lookback_days sweep</h2>'
        '<p style="color:#555;max-width:940px;">A CURATOR-param sweep (re-curation) at the canonical '
        'mws&nbsp;6 / cap&nbsp;1.0 / λ&nbsp;2.0 / 150d config. <b>All six windows (7 / 14 / 21 / 28 / 45 / 90d) now '
        'use fuller ledes</b> (Wayback + look-ahead-biased live-fallback), so this is a <b>clean single-variable '
        'news-window sweep</b> &mdash; the earlier title-only 90d confound is resolved. Columns: total return, '
        '<b>IR</b> (success = risk-adjusted return vs SPY), '
        '<b>L1/L2</b> churn, whether QUBT/RKLB/NVDA were added, and every ticker <b>funded over the 3 years</b> '
        '(breadth). The <b style="color:#2b8a3e;">green</b> bar / ★ row is the <b>optimal</b> window '
        + (f'(<b>{_opt_nlb}d</b>): highest IR, which here also has the highest return and the widest ticker spread '
           'at near-lowest churn. ' if _opt_nlb else '')
        + 'The live window is red-outlined. In-sample, so read it as relative signal on the '
        'news window, not a reason to switch off the live 14d without forward evidence.</p>'
        + _nbar
        + '<table style="margin-top:.6em;"><thead><tr>'
        f'<th {_lc}>news_lookback_days</th><th {_lc}>return</th><th {_lc}>IR</th><th {_lc}>L1</th><th {_lc}>L2</th>'
        + "".join(f'<th {_lc}>{t}?</th>' for t in TRACK_TICKERS)
        + f'<th {_lc}>tickers funded (3-yr)</th>'
        '</tr></thead><tbody>' + _ntr + '</tbody></table>')

    # sections 8-10: total return vs each FREE optimizer knob (lookback / cap / λ), holding the OTHER two + mws
    # at the live config. All are $0 math-replay slices of the cached grid (no re-curation).
    import plotly.graph_objects as _lgo

    def _free_bar(field, values, live_val, xtitle):
        fixed = {"mws": _mws_fixed, "cap": CURRENT[0], "lam": CURRENT[1], "lb": CURRENT[2], "mt": CURRENT_MT}
        del fixed[field]                       # `field` varies; the rest pinned to live (incl. min_trade)
        sub = {r[field]: r for r in rows_all if all(r.get(k) == v for k, v in fixed.items())}
        xs = [v for v in values if v in sub]
        _yv = [sub[v]["ret"] * 100 for v in xs]
        # dynamic y-range: zoom into the bar heights (not 0-based) so differences among high returns are legible.
        _lo, _hi = (min(_yv), max(_yv)) if _yv else (0.0, 1.0)
        _sp = _hi - _lo
        _yr = [max(0.0, _lo - 0.8 * _sp), _hi + (0.20 * _sp if _sp else max(abs(_hi) * 0.1, 1.0))]
        f = _lgo.Figure(_lgo.Bar(
            x=[str(v) for v in xs], y=_yv,
            marker_color=[GREEN if v == live_val else BLUE for v in xs],
            text=["live" if v == live_val else "" for v in xs], textposition="outside",
            hovertemplate="%{x}: %{y:+.0f}%<extra></extra>"))
        f.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 60, "r": 20},
                        xaxis={"title": xtitle, "type": "category"},
                        yaxis={"title": "total return %", "range": _yr})
        return f.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})

    _fixed_note = (f'watchlist size ({_mws_fixed})', f'cap ({CURRENT[0]})', f'&lambda; ({CURRENT[1]})',
                   f'lookback ({CURRENT[2]}d)')
    lb_html = (
        '<h2>6. Total return vs optimizer lookback (live config)</h2>'
        '<p style="color:#555;max-width:920px;">A FREE math-replay slice (no re-curation): hold the live '
        f'{_fixed_note[0]}, {_fixed_note[1]} and {_fixed_note[2]} fixed and vary only the optimizer lookback '
        '(trailing days of prices used to estimate μ/Σ). The <b style="color:#2b8a3e;">green</b> bar is the live '
        f'setting ({CURRENT[2]}d).</p>' + _free_bar("lb", LOOKBACKS, CURRENT[2], "optimizer_lookback (days)"))
    cap_html = (
        '<h2>7. Total return vs concentration_cap (live config)</h2>'
        '<p style="color:#555;max-width:920px;">Same slice, holding the live '
        f'{_fixed_note[0]}, {_fixed_note[2]} and {_fixed_note[3]} fixed and varying only the concentration_cap '
        '(the per-position max weight). The <b style="color:#2b8a3e;">green</b> bar is the live setting '
        f'({CURRENT[0]}).</p>' + _free_bar("cap", CAPS, CURRENT[0], "concentration_cap"))
    lam_html = (
        '<h2>8. Total return vs risk_aversion (live config)</h2>'
        '<p style="color:#555;max-width:920px;">Same slice, holding the live '
        f'{_fixed_note[0]}, {_fixed_note[1]} and {_fixed_note[3]} fixed and varying only the risk_aversion '
        '&lambda; (higher λ = more risk-averse = less concentrated). The <b style="color:#2b8a3e;">green</b> bar '
        f'is the live setting (λ&nbsp;{CURRENT[1]}).</p>' + _free_bar("lam", LAMBDAS, CURRENT[1], "risk_aversion (λ)"))

    # 9. min_trade_size_frac sweep: now a FIRST-CLASS grid axis (swept on the canonical watchlist in the cached
    # grid above), so this is a $0 slice of `rows` at the live cap/λ/lookback -- no separate replays.
    _LIVE_MT = CURRENT_MT
    # min_trade is swept on the fixed anchor MT_SWEEP_MWS (not the canonical), so pull the slice from there.
    _mt_by = {r["mt"]: r for r in rows_all if r["mws"] == MT_SWEEP_MWS and (r["cap"], r["lam"], r["lb"]) == CURRENT}
    _mt_rows = [{"mt": v, "ret": _mt_by[v]["ret"]} for v in MIN_TRADE_FRACS if v in _mt_by]
    _mtx = [str(r["mt"]) for r in _mt_rows]
    _rets = [r["ret"] * 100 for r in _mt_rows]
    # Zoom the y-axis into the bar heights (dynamic) instead of 0-based, so the differences are legible.
    _dmin, _dmax = min(_rets), max(_rets)
    _yrange = [max(0.0, _dmin - 0.8 * (_dmax - _dmin)), _dmax + 0.15 * (_dmax - _dmin)]
    _mtf_fig = _lgo.Figure(_lgo.Bar(
        x=_mtx, y=_rets,
        marker_color=[GREEN if r["mt"] == _LIVE_MT else BLUE for r in _mt_rows],
        text=["live" if r["mt"] == _LIVE_MT else "" for r in _mt_rows], textposition="outside",
        hovertemplate="mt %{x}: %{y:+.0f}%<extra></extra>"))
    _mtf_fig.update_layout(template="seaborn", height=360, margin={"t": 20, "l": 60, "r": 20},
                           xaxis={"title": "min_trade_size_frac", "type": "category"},
                           yaxis={"title": "total return %", "range": _yrange})
    min_trade_html = (
        '<h2>9. Total return vs min_trade_size_frac (live config)</h2>'
        '<p style="color:#555;max-width:920px;">A FREE math-replay slice (no re-curation): hold the live '
        f'cap ({CURRENT[0]}), &lambda; ({CURRENT[1]}), lookback ({CURRENT[2]}d) fixed and vary only the '
        '<b>no-trade band</b> &mdash; the smallest rebalancing trade the backtest executes, as a fraction of the '
        'book. A suppressed trade&#39;s dollars are redistributed across the trades that DO clear the band, so the '
        'book stays fully invested (Fidelity IRA = zero cost, so no cost model). The '
        f'<b style="color:#2b8a3e;">green</b> bar is the live setting ({_LIVE_MT}). The min_trade axis is swept on '
        f'the fixed mws&nbsp;{MT_SWEEP_MWS} anchor (representative; the effect is not watchlist-size-sensitive). A '
        'small band can slightly <i>improve</i> return (avoiding whipsaw); too large a band (&ge;0.15) starves '
        'the momentum signal.</p>'
        + _mtf_fig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))

    # section 7: backtest gems — for every ticker, the best $ P&L it achieved across ALL 1350 sweep settings,
    # and the setting that produced it. Built from data/curator_runs/_gems.json (scripts/gems_scan.py).
    import json as _gjson
    _gems_p = ROOT / "data" / "curator_runs" / "_gems.json"
    _gems = _gjson.loads(_gems_p.read_text())[:20] if _gems_p.exists() else []
    if _gems:
        _maxg = max((abs(g["gain"]) for g in _gems), default=1.0) or 1.0
        _grows = ""
        for _i, g in enumerate(_gems):
            _cur_hit = g["mws"] == _mws_fixed and (g["cap"], g["lam"], g["lb"]) == CURRENT
            _bg = "background:#fff7e6;" if _cur_hit else ""
            _pct = abs(g["gain"]) / _maxg * 100.0                       # inline data-bar, scaled to the top gain
            _bcol = GREEN if g["gain"] >= 0 else RED
            _bar = (f'<td><div style="width:210px;background:#eee;border-radius:2px;">'
                    f'<div style="background:{_bcol};height:13px;width:{_pct:.1f}%;border-radius:2px;"></div></div></td>')
            _grows += (f'<tr style="{_bg}border-bottom:1px solid #eee;"><td {_lc}>{_i + 1}</td>'
                       f'<td {_lc}><b>{g["ticker"]}</b></td><td {_lc}>${g["gain"]:,.0f}</td>'
                       f'<td {_lc}>{g["days_held"]}</td>'
                       f'<td {_lc}>ws{g["mws"]} &middot; cap{g["cap"]}/λ{g["lam"]}/{g["lb"]}d'
                       + (" &larr; live" if _cur_hit else "") + '</td>' + _bar + '</tr>')
        # gem/wave-diversity table (below the gems table): low-churn frontier configs ranked by how many gems
        # (across how many WAVES) they profit from — the criterion behind the canonical pick. From
        # scripts/gem_diversity_scan.py -> data/curator_runs/_gem_diversity.json.
        _divp = ROOT / "data" / "curator_runs" / "_gem_diversity.json"
        _div = _gjson.loads(_divp.read_text()) if _divp.exists() else []
        _div_html = ""
        if _div:
            _drows = ""
            for _i, x in enumerate(_div):
                _hit = (x["mws"] == _mws_fixed and (x["cap"], x["lam"], x["lb"]) == CURRENT
                        and x.get("mt", CURRENT_MT) == CURRENT_MT)   # match min_trade too, else all mt bands flag "live"
                _dbg = "background:#fff7e6;" if _hit else ""
                _wv = " &middot; ".join(f'{k} +{v:.0f}' for k, v in x["waves"].items())
                _drows += (f'<tr style="{_dbg}border-bottom:1px solid #eee;"><td {_lc}>{_i + 1}</td>'
                           f'<td {_lc}>ws{x["mws"]} &middot; cap{x["cap"]}/λ{x["lam"]}/{x["lb"]}d/mt{x.get("mt", CURRENT_MT):g}'
                           + (" &larr; live" if _hit else "") + '</td>'
                           f'<td {_lc}>{x["ret"] * 100:+.0f}%</td><td {_lc}>{x["dd"] * 100:.0f}%</td>'
                           f'<td {_lc}>{x["pf"]:.2f}</td><td {_lc}>{x["gb"] * 100:.0f}%</td>'
                           f'<td {_lc}>{x["l1"]:.0f}</td><td {_lc}>{x["l2"]:.0f}</td>'
                           f'<td {_lc}>{x["n_pos"]}</td><td {_lc}>{x["n_waves"]}</td>'
                           f'<td {_lc}>{_wv}</td></tr>')
            _div_html = (
                '<h3 style="margin:1.3em 0 .2em;">Low-churn settings by gem &amp; wave diversity</h3>'
                f'<p style="color:#555;max-width:940px;">Within the low-churn box (|maxDD| &lt; {REC_MAX_DD:.0f}% '
                f'AND L1 &lt; {REC_MAX_L1:.0f} AND L2 &lt; {REC_MAX_L2:.0f}), the settings whose gains come from the '
                '<b>most gem tickers</b> &mdash; ranked by gem count, then return &mdash; with the <b>waves</b> '
                'driving those gains. Broad wave coverage is more robust than a single-stock run. '
                '<b>P/|L|</b> = profit factor (up-day $ &divide; down-day $; &gt;1 = captured more on rises '
                'than given back on falls) and <b>giveback</b> = worst peak&rarr;trough; together they read '
                'rotation EFFICIENCY vs round-trip SEVERITY. Wave figures are percentage points of total '
                'return. NOTE: all 3-year and in-sample &mdash; the 2023-24 RKLB run-up dominates, so use the '
                'recent-year table below to judge rotation on the forward-relevant slice.</p>'
                + '<table style="font-size:12.5px;margin-top:.4em;"><thead><tr>'
                f'<th {_lc}>#</th><th {_lc}>setting</th><th {_lc}>return</th><th {_lc}>maxDD</th>'
                f'<th {_lc}>P/|L|</th><th {_lc}>giveback</th>'
                f'<th {_lc}>L1</th><th {_lc}>L2</th><th {_lc}>gems</th><th {_lc}>waves</th>'
                f'<th {_lc}>waves generating the gains (pp)</th></tr></thead><tbody>' + _drows + '</tbody></table>')

        # Recent-year (trailing 365d) rotation ranking: the SAME canonical-mws grid scored on ONLY the last
        # year -- the slice NOT dominated by the 2023-24 RKLB run-up -- ranked by 1y return / |1y giveback|
        # (return per unit round-trip). Churn is unfiltered here (zero-cost IRA). Answers "which config best
        # navigates recent, forward-relevant data" rather than "which rode the 2023-24 winners".
        # Shared "portfolio value over time" chart for a list of configs (each: cap/lam/lb/mt + curve{x,y})
        # plus the buy/hold + SPY baselines from _ref. Live config = thick red. Log axis, styled like plot 10.
        def _eqplot(items, title, caption):
            _its = [it for it in items if it.get("curve", {}).get("x")]
            if not _its:
                return ""
            _pal = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22",
                    "#7f7f7f", "#d97706"]
            _f = go.Figure()
            for _k, _it in enumerate(_its):
                _lv = ((_it["cap"], _it["lam"], _it["lb"]) == CURRENT and _it.get("mt", CURRENT_MT) == CURRENT_MT)
                _nm = (f'cap{_it["cap"]}/λ{_it["lam"]}/{_it["lb"]}d/mt{_it.get("mt", CURRENT_MT):g}'
                       + (" (live)" if _lv else ""))
                _f.add_trace(go.Scatter(x=_it["curve"]["x"], y=_it["curve"]["y"], mode="lines", name=_nm,
                             line={"color": ("#d62728" if _lv else _pal[_k % len(_pal)]),
                                   "width": (3.2 if _lv else 1.3)}, opacity=(1.0 if _lv else 0.6)))
            if _ref and _ref.get("bnh_x"):
                _f.add_trace(go.Scatter(x=_ref["bnh_x"], y=_ref["bnh_y"], mode="lines", name="Buy-and-hold",
                                        line={"color": "#3b82f6", "width": 2.2, "dash": "dash"}))
            if _ref and _ref.get("spy_y"):
                _f.add_trace(go.Scatter(x=_ref["curve_x"], y=_ref["spy_y"], mode="lines", name="SPY benchmark",
                                        line={"color": "#10b981", "width": 2.0, "dash": "dot"}))
            _f.update_layout(template="seaborn", height=520, margin={"t": 20, "l": 72, "r": 250},
                             hovermode="closest",
                             legend={"x": 1.02, "xanchor": "left", "y": 1, "yanchor": "top", "font": {"size": 10}})
            _f.update_xaxes(range=_xr)
            _f.update_yaxes(title_text="portfolio value ($)", type="log",
                            tickvals=[10000, 30000, 100000, 300000, 1000000, 3000000],
                            ticktext=["$10K", "$30K", "$100K", "$300K", "$1M", "$3M"])
            return (f'<h3 style="margin:1.3em 0 .2em;">{title}</h3>'
                    f'<p style="color:#555;max-width:940px;">{caption}</p>'
                    + _f.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))

        # Portfolio value over time for the gem/wave-diversity table's configs (curves stored in _gem_diversity.json).
        _gcurved = [x for x in _div if x.get("curve", {}).get("x")]
        _geq_html = _eqplot(
            _gcurved,
            f'Portfolio value over time &mdash; the {len(_gcurved)} gem &amp; wave-diversity configs (vs buy/hold and SPY)',
            'The equity curve of every config in the table above, on the canonical mws-'
            f'{_mws_fixed} (new-thesis) curations, alongside the equal-weight starter buy/hold and SPY. Log axis. '
            'The <b style="color:#d62728;">red</b> line is the live config; the spread shows how differently the '
            'SAME curations perform under different optimizer settings (same picks, different weighting).')

        _div_html = _div_html + _geq_html   # recent-year rotation table/plot dropped (didn't factor into the canonical decision)

        gems_html = (
            '<h2>5. Backtest gems</h2>'
            '<p style="color:#555;max-width:940px;">For every ticker the curator ever held, its best <b>$ gain</b> '
            '(price-driven P&amp;L on the position) across <b>all 1350 sweep settings</b>, and the setting that '
            'produced it. A ticker&#39;s gain is maximized by the config that weighted it most while it ran (usually '
            'high cap, low λ, short lookback). Note the $ figure is <b>compounding-weighted</b>: a late-window '
            'winner rides an already-grown balance at up to 100% concentration, so it dwarfs early picks &mdash; '
            'these are hindsight, in-sample <b>upper bounds</b>, a gem list to build a low-churn strategy around, '
            'not a track record. Anchors (SPY/AGG/IAU) excluded.</p>'
            + '<table style="font-size:12.5px;margin-top:.5em;"><thead><tr>'
            f'<th {_lc}>#</th><th {_lc}>ticker</th><th {_lc}>best $ gain</th>'
            f'<th {_lc}>days held</th><th {_lc}>best setting</th><th {_lc}>gain</th>'
            f'</tr></thead><tbody>{_grows}</tbody></table>'
            + _div_html)
    else:
        gems_html = ('<h2>5. Backtest gems</h2><p style="color:#999;">Gem scan not yet run'
                     '(<code>scripts/gems_scan.py</code> writes <code>data/curator_runs/_gems.json</code>).</p>')

    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    # Title date range: same snapshots.csv the Curator Backtest reads, so all three DBs show one range.
    _snap = ROOT / runs_dir / "_backtest" / "snapshots.csv"
    _sd = sorted({ln.split(",", 1)[0] for ln in _snap.read_text().splitlines()[1:] if ln}) if _snap.exists() else []
    _range = (f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{_sd[0]} to {_sd[-1]}</p>'
              if _sd else "")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Backtest sweeps (BTS)</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;margin:0 auto;
padding:0 1.5em;color:#222;line-height:1.5}}h1,h2{{color:#111}}table{{border-collapse:collapse;font-size:13px;width:100%}}
th{{text-align:right;padding:6px 10px;border-bottom:2px solid #ccc;white-space:nowrap}}th:first-child{{text-align:left}}
.built{{position:absolute;top:8px;right:16px;font-size:12px;color:#888}}
</style></head><body><div class="built">dashboard built {ts}</div>{nav}
<h1>Backtest sweeps (BTS)</h1>
{_range}
<p style="color:#555;max-width:860px;">{len(rows_all)} configs: concentration_cap × risk_aversion (λ) × optimizer_lookback
({_grid3d} combinations) replayed across all {len(_mws_present)} <b>max_watchlist_size</b> curation sets, plus
<b>min_trade_size_frac</b> swept as a 4th axis on the canonical watchlist ({len(MIN_TRADE_FRACS)} values).
All {len(_mws_present)} watchlist sizes are the SAME geosplit thesis (one coherent curation family), so
cross-size comparisons in plots 1-3 are clean. These knobs touch only the mean-variance replay, not the
curator, so the whole grid costs <b>$0</b> (no LLM — the curations already exist; only expanding
max_watchlist_size itself, section 11, re-curates). Metrics are benchmark-relative: <b>Information Ratio</b>
= annualized active return ÷ tracking error vs SPY (consistency of beating the benchmark). SPY returned
{spy_ret*100:+.0f}% over the window. ★ = best in column.</p>
<p style="color:#b45309;max-width:860px;background:#fffbeb;border:1px solid #fde68a;padding:.6em .8em;border-radius:6px;">
<b>Look-ahead-clean.</b> The compounder gridsearch on the geosplit curations: the curator read archived
<b>Wayback</b> ledes where available (~46% of articles) else title-only, with the biased live ledes ignored,
so each rebalance saw only period-correct news. The curator-<b>model</b> comparison (§12, kimi vs sonnet vs
deepseek, ranked by a blind Opus soundness judge) sits at the bottom. Canonical config: mws&nbsp;{_mws_fixed} / cap&nbsp;{CURRENT[0]}
/ λ&nbsp;{CURRENT[1]} / {CURRENT[2]}d. <b>All in-sample</b> — these rank candidates to <b>forward-test</b>, not an optimum.</p>
{grid_html}
<h2>1. Return vs drawdown</h2>
<p style="color:#555;max-width:920px;">The horizontal axis is <b>max drawdown</b> — the portfolio&#39;s biggest
peak-to-trough loss as a fraction of its running peak (the single worst high-to-low decline over the window);
further right = deeper loss. The vertical axis is annualized return, so <b>upper-left is best</b> (high return,
shallow drawdown). Each point is one cap/λ/lookback config; <b>color = max_watchlist_size</b>
({_fmt([m for m in _mws_present])} shown), so every watchlist size contributes its own {_grid3d}-config cloud
(total {_plotted} points; min_trade pinned to the live {CURRENT_MT:g} here — it is swept separately in section 9).
Within a cloud the free knobs move a point deterministically; jumping between
colors ALSO swaps the underlying curation, and those kimi re-curations swing 2-5&times; on their own — so
<b>read cross-color separation as parameter effect + curation-draw noise, not a clean mws response</b>. The
canonical live config (watchlist {_mws_fixed}) is the <b>black star</b>.</p>
{scatter}
<h2>2. Return vs L1 churn</h2>
<p style="color:#555;max-width:920px;">Same points (all watchlist sizes), risk axis replaced by <b>L1 churn = annualized one-way
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
<h2>4. Recommended settings</h2>
{rec_html}
{gems_html}
{lb_html}
{cap_html}
{lam_html}
{min_trade_html}
{mws_equity_html}
{mws_html}
{model_html}
{llm4_html}
</body></html>"""
    out.write_text(page)
    top = max(rows, key=lambda r: r["ir"] if r["ir"] == r["ir"] else -9e9)
    print(f"wrote {out}  ({len(rows)} configs)")
    print(f"  best IR: cap {top['cap']} / λ {top['lam']} / {top['lb']}d -> IR {top['ir']:+.2f} "
          f"t={top['tstat']:+.1f} Calmar {top['calmar']:.2f} ann {top['ann']*100:+.0f}% dd {top['dd']*100:.0f}%")
    cur = next(r for r in rows if r["cur"])
    print(f"  current ({CURRENT[0]}/{CURRENT[1]}/{CURRENT[2]}): IR {cur['ir']:+.2f} t={cur['tstat']:+.1f} Calmar {cur['calmar']:.2f} "
          f"ann {cur['ann']*100:+.0f}% dd {cur['dd']*100:.0f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="data/curator_runs/proto-mws20")  # INTERIM canonical (mws20 geosplit titles+live)
    ap.add_argument("--out", default=str(ROOT / "docs" / "sweep_pwr.html"))
    ap.add_argument("--recompute", action="store_true", help="re-run the 150 backtests (else use cache)")
    a = ap.parse_args()
    build(a.runs_dir, Path(a.out), recompute=a.recompute)
