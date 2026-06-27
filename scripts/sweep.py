"""Sweep one optimizer parameter across a range, replaying the same
curator JSONs each time. Renders an overlay of equity curves to
docs/sweep_<param>.html.

Three params are supported. Each is a pure replay (no LLM calls):

  risk_aversion     : λ in the mean-variance utility μᵀw − λ·wᵀΣw
  lookback          : years of price history used to estimate μ and Σ
  concentration_cap : per-ticker upper bound on weight (the `max_weight`
                      kwarg on portfolio.optimize_portfolio; the profile
                      knob name is `concentration_cap`)

Usage:
  python scripts/sweep.py --param risk_aversion
  python scripts/sweep.py --param lookback --values 0.5,1,1.3,2,3
  python scripts/sweep.py --param concentration_cap --runs-dir data/curator_runs/5y-sweep-cap08

Output: docs/sweep_<param>.html, plus a one-row-per-variant summary table
appended below the chart.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

RISK_FREE_RATE = 0.04  # matches portfolio.py default

from src.portfolio import (
    curator_backtest, _fetch_benchmark_curves, _nav_strip,
    load_financial_model, load_backtest_config,
)

# Live optimizer defaults, used as the held-constant base for each sweep so the
# swept parameter is varied around the config /review-portfolio actually uses.
_FM = load_financial_model()
_BASE_MAX_WEIGHT = float(_FM["concentration_cap"])
_BASE_RISK_AVERSION = float(_FM["risk_aversion"])
import re as _re
_m = _re.match(r"(\d+(?:\.\d+)?)", str(_FM["lookback_period"]))
_BASE_LOOKBACK = float(_m.group(1)) if _m else 1.5

DEFAULTS = {
    "risk_aversion":     [0.0, 0.33, 0.5, 0.67, 1.0, 1.5, 2.0, 3.0, 10.0],
    "lookback":          [0.2, 0.33, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0],
    "concentration_cap": [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00],
}

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]


def run_one(param: str, value: float, runs_dir: str, tmp: Path,
            base_max_weight: float, base_risk_aversion: float,
            base_lookback: float, base_t_update: int) -> pd.Series:
    """Replay the curator runs with one parameter swapped to ``value``.
    Returns a date-indexed Series of total portfolio value. The two
    parameters not being swept are held at the live-config base."""
    out_dir = tmp / f"{param}_{value}"
    kw = {
        "runs_dir": runs_dir,
        "out_dir": str(out_dir),
        "max_weight": base_max_weight,
        "risk_aversion": base_risk_aversion,
        "lookback_years_override": base_lookback,
        "t_update_days": base_t_update,
        "benchmarks": [],
    }
    if param == "risk_aversion":
        kw["risk_aversion"] = value
    elif param == "lookback":
        kw["lookback_years_override"] = value
    elif param == "concentration_cap":
        kw["max_weight"] = value
    else:
        raise ValueError(f"unknown param: {param}")
    curator_backtest(**kw)
    snaps = pd.read_csv(out_dir / "snapshots.csv", parse_dates=["date"])
    totals = snaps.groupby("date")["total_value"].first().sort_index()
    return totals


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--param", required=True, choices=list(DEFAULTS.keys()))
    p.add_argument("--values", default=None,
                   help="comma-separated values to sweep (overrides defaults)")
    p.add_argument("--runs-dir", default="data/curator_runs/postcovid")
    p.add_argument("--out", default=None,
                   help="output HTML path (default: docs/sweep_<param>.html)")
    p.add_argument("--benchmarks", nargs="*", default=["SPY"])
    p.add_argument("--base-max-weight", type=float, default=_BASE_MAX_WEIGHT)
    p.add_argument("--base-risk-aversion", type=float, default=_BASE_RISK_AVERSION)
    p.add_argument("--base-lookback", type=float, default=_BASE_LOOKBACK)
    args = p.parse_args(argv)

    _bc = load_backtest_config()
    base_t_update = int(_bc["t_update_days"])

    values = ([float(v) for v in args.values.split(",")]
              if args.values else DEFAULTS[args.param])
    out_path = Path(args.out) if args.out else Path(f"docs/sweep_{args.param}.html")

    tmp = Path(tempfile.mkdtemp(prefix="sweep_"))
    try:
        curves: dict[float, pd.Series] = {}
        for v in values:
            print(f"  {args.param} = {v}", file=sys.stderr)
            curves[v] = run_one(args.param, v, args.runs_dir, tmp,
                                args.base_max_weight, args.base_risk_aversion,
                                args.base_lookback, base_t_update)

        # All curves share the same x-axis. Build summary first.
        first = next(iter(curves.values()))
        start, end = first.index[0], first.index[-1]
        initial = float(first.iloc[0])
        summary = []
        for v, s in curves.items():
            final = float(s.iloc[-1])
            ret = (final / initial) - 1.0
            ann = (final / initial) ** (365.25 / (end - start).days) - 1.0
            daily_ret = s.pct_change().dropna()
            ann_vol = float(daily_ret.std() * np.sqrt(252))
            sharpe = (ann - RISK_FREE_RATE) / ann_vol if ann_vol > 0 else float("nan")
            # Max drawdown: running peak minus current, normalized by running peak.
            running_peak = s.cummax()
            drawdown = (s / running_peak) - 1.0
            mdd = float(drawdown.min())
            # Calmar = annualized return / |MDD|; penalizes high-drawdown paths
            # the way Sharpe doesn't (Sharpe penalizes upside vol equally).
            calmar = ann / abs(mdd) if mdd < 0 else float("nan")
            summary.append((v, final, ret, ann, mdd, sharpe, calmar))

        fig = go.Figure()
        for i, (v, s) in enumerate(curves.items()):
            fig.add_trace(go.Scatter(
                x=s.index, y=s.values, name=f"{args.param}={v}",
                mode="lines", line={"color": PALETTE[i % len(PALETTE)], "width": 2},
            ))
        if args.benchmarks:
            for b, curve in _fetch_benchmark_curves(args.benchmarks, start, end, initial).items():
                fig.add_trace(go.Scatter(
                    x=curve.index, y=curve.values, name=f"{b} benchmark",
                    mode="lines", line={"color": "#10b981", "width": 1.5, "dash": "dot"},
                ))

        fig.update_layout(
            template="seaborn",
            title=f"Curator backtest swept across {args.param} "
                  f"({start.date()} to {end.date()})",
            xaxis_title="date",
            yaxis_title="portfolio value ($)",
            yaxis_tickformat="$,.0f",
            height=600,
            margin={"t": 60, "b": 60, "l": 80, "r": 30},
        )

        # Summary table.
        # Identify the current investor_profile.md default value for this
        # param so the corresponding table row can be rendered in bold.
        if args.param == "risk_aversion":
            default_v = args.base_risk_aversion
        elif args.param == "concentration_cap":
            default_v = args.base_max_weight
        else:  # lookback — read the live default from _starter.json
            import json as _json
            _starter = _json.loads((Path(args.runs_dir) / "_starter.json").read_text())
            default_v = float(_starter.get("lookback_years", 1.3))

        def _fmt_row(v, final, ret, ann, mdd, sharpe, calmar):
            tr = "<tr style='font-weight:bold;'>" if abs(v - default_v) < 1e-9 else "<tr>"
            return (
                f"{tr}<td>{v}</td><td>${final:,.0f}</td>"
                f"<td>{ret*100:+.1f}%</td><td>{ann*100:+.1f}%</td>"
                f"<td>{mdd*100:+.1f}%</td>"
                f"<td>{sharpe:.2f}</td><td>{calmar:.2f}</td></tr>"
            )

        rows = "".join(_fmt_row(*r) for r in summary)
        WALK_FORWARD_NOTES = {
            "risk_aversion": (
                "total return on this window keeps rising as λ falls toward 0, "
                "because a smaller variance penalty lets the optimizer concentrate "
                "harder into the single 2025 winner (RKLB). That is the in-sample "
                "tail, not proof of a durable edge. The profile sets "
                "<code>risk_aversion=0.33</code> — return-tilted but still penalizing "
                "variance; whether an even lower λ is genuinely better is a question "
                "for <b>forward testing</b> on out-of-sample quarters, not this "
                "in-sample curve."
            ),
            "lookback": (
                "shorter lookbacks tend to score higher here because a shorter memory "
                "chases this window's recent run-up (RKLB) harder — an in-sample "
                "momentum artifact. The profile sets <code>lookback=1.5y</code>, a "
                "steadier μ/Σ estimate; <b>forward testing</b> on out-of-sample "
                "quarters is the real check on whether a shorter window helps."
            ),
            "concentration_cap": (
                "total return rises with the cap because a higher cap lets the "
                "optimizer pile more into the single winner (RKLB) — higher "
                "single-name risk, and an in-sample artifact. The profile sets "
                "<code>concentration_cap=0.80</code>, which still allows high "
                "conviction while bounding any one position; <b>forward testing</b> "
                "is the arbiter of whether a higher cap is worth the concentration."
            ),
        }
        wf_note = WALK_FORWARD_NOTES.get(args.param, "")
        table = (
            f"<h2>Summary</h2><table style='border-collapse:collapse;font-size:14px;'>"
            f"<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
            f"<th style='padding:4px 12px;'>{args.param}</th>"
            f"<th style='padding:4px 12px;'>Final value</th>"
            f"<th style='padding:4px 12px;'>Total return</th>"
            f"<th style='padding:4px 12px;'>Annualized</th>"
            f"<th style='padding:4px 12px;'>Max drawdown</th>"
            f"<th style='padding:4px 12px;'>Sharpe</th>"
            f"<th style='padding:4px 12px;'>Calmar</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"<p style='font-size:13px;color:#666;'>"
            f"<strong>Sharpe</strong> = (annualized return − "
            f"{RISK_FREE_RATE * 100:.0f}% risk-free) / annualized daily-return "
            f"σ × √252.<br>"
            f"<strong>Calmar</strong> = annualized return / |max drawdown|; "
            f"penalizes deep drawdowns the way Sharpe doesn't.</p>"
            f"<p style='font-size:13px;color:#666;'>"
            f"<strong>How to read the winner:</strong> this sweep runs over the "
            f"published post-COVID window (2022-03-31 → 2025-10-31), which is "
            f"dominated by a single position (RKLB) that ran in 2025, so "
            f"{wf_note}</p>"
        )

        # Parameter-settings table (mirrors the backtest dashboard): the knobs
        # held constant across this sweep, plus the swept parameter and its values.
        import json as _json2
        _starter = _json2.loads((Path(args.runs_dir) / "_starter.json").read_text())
        _fm2 = load_financial_model()
        _g = lambda x: f"{x:g}"
        _lbf = lambda x: f"{x:g}y"
        _pctf = lambda x: f"{x:.0%}"

        def _knob(label, base_val, fmt, swept):
            return ((label, "swept &darr;", "this sweep's variable") if swept
                    else (label, fmt(base_val), ""))

        _swept_fmt = _lbf if args.param == "lookback" else _g
        _param_rows = [
            ("Backtest window", f"{start.date()} &rarr; {end.date()}", ""),
            ("Rebalance cadence",
             f"{_starter.get('rebalance_period', 'quarterly')} "
             f"({len(_starter.get('as_of_dates', []))} curator calls)", ""),
            ("Starter watchlist", ", ".join(_starter.get("starter_watchlist", [])) or "—", ""),
            ("Initial capital", f"${initial:,.0f}", ""),
            _knob("Risk aversion (&lambda;)", args.base_risk_aversion, _g,
                  args.param == "risk_aversion"),
            _knob("Lookback (&mu;/&Sigma; estimation)", args.base_lookback, _lbf,
                  args.param == "lookback"),
            _knob("Concentration cap (max weight)", args.base_max_weight, _pctf,
                  args.param == "concentration_cap"),
            ("Max watchlist size", f"{_fm2['max_watchlist_size']}", ""),
            ("Risk-free rate", f"{RISK_FREE_RATE:.0%}", ""),
            ("Execution lag", f"{base_t_update} trading day(s)", ""),
            ("Swept parameter", f"<code>{args.param}</code>",
             "varied across the values below"),
            ("Swept values", ", ".join(_swept_fmt(v) for v in values), ""),
        ]
        _ptr = "".join(
            f"<tr><td style='padding:5px 14px 5px 0;color:#555;white-space:nowrap;'>{k}</td>"
            f"<td style='padding:5px 14px 5px 0;font-weight:600;'>{v}</td>"
            f"<td style='padding:5px 0;color:#b45309;font-size:13px;'>{note}</td></tr>"
            for k, v, note in _param_rows
        )
        params_table = (
            "<h2 style='margin:1.2em 0 0.3em;'>Parameter settings</h2>"
            "<p style='color:#555;max-width:780px;margin:0 0 0.6em;'>The "
            "optimizer/backtest knobs held constant across this sweep, read from "
            "<code>investor_profile.md</code> (the same config "
            "<code>/review-portfolio</code> uses with real money). Only "
            f"<code>{args.param}</code> is varied — across the values listed in the "
            "last row.</p>"
            "<table style='border-collapse:collapse;font-size:14px;margin-bottom:1.2em;'>"
            f"<tbody>{_ptr}</tbody></table>"
        )

        nav = _nav_strip(f"sweep_{args.param}.html")

        page = (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Sweep: {args.param}</title>'
            '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
            'max-width:1180px;margin:0 auto;padding:1em 1.5em;color:#222;}'
            'th,td{border-bottom:1px solid #eee;}</style></head><body>'
            + nav +
            f'<h1>Parameter sweep: <code>{args.param}</code></h1>'
            f'<p style="color:#555;max-width:780px;">Same curator JSONs replayed '
            f'through the optimizer at each <code>{args.param}</code> value. '
            f'All other knobs held at their <code>investor_profile.md</code> defaults. '
            f'Differences across curves isolate the optimizer\'s sensitivity to '
            f'<code>{args.param}</code>.</p>'
            + params_table
            + fig.to_html(full_html=False, include_plotlyjs="cdn",
                          config={"displayModeBar": False})
            + table
            + '</body></html>'
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page, encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
