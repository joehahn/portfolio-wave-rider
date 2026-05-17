"""Sweep one optimizer parameter across a range, replaying the same
curator JSONs each time. Renders an overlay of equity curves to
docs/sweep_<param>.html.

Three params are supported. Each is a pure replay (no LLM calls):

  risk_aversion : λ in the mean-variance utility μᵀw − λ·wᵀΣw
  lookback      : years of price history used to estimate μ and Σ
  max_weight    : per-ticker concentration cap

Usage:
  python scripts/sweep.py --param risk_aversion
  python scripts/sweep.py --param lookback --values 0.5,1,1.3,2,3
  python scripts/sweep.py --param max_weight --runs-dir data/curator_runs/5y-quarterly

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
    _compute_expected_vs_realized,
)

DEFAULTS = {
    "risk_aversion": [0.0, 0.33, 0.5, 0.67, 1.0, 2.0, 3.0, 10.0],
    "lookback":      [0.5, 1.0, 1.5, 2.0, 3.0, 5.0],
    "max_weight":    [0.10, 0.25, 0.33, 0.50, 0.75, 1.00],
}

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]


def run_one(param: str, value: float, runs_dir: str, tmp: Path,
            base_max_weight: float, base_risk_aversion: float
            ) -> tuple[pd.Series, pd.DataFrame]:
    """Replay the curator runs with one parameter swapped to ``value``.
    Returns (date-indexed Series of total portfolio value, per-rebalance
    expected-vs-realized DataFrame)."""
    out_dir = tmp / f"{param}_{value}"
    kw = {
        "runs_dir": runs_dir,
        "out_dir": str(out_dir),
        "max_weight": base_max_weight,
        "risk_aversion": base_risk_aversion,
        "benchmarks": [],
    }
    if param == "risk_aversion":
        kw["risk_aversion"] = value
    elif param == "lookback":
        kw["lookback_years_override"] = value
    elif param == "max_weight":
        kw["max_weight"] = value
    else:
        raise ValueError(f"unknown param: {param}")
    curator_backtest(**kw)
    snaps = pd.read_csv(out_dir / "snapshots.csv", parse_dates=["date"])
    totals = snaps.groupby("date")["total_value"].first().sort_index()
    recs = pd.read_csv(out_dir / "recommendations.csv")
    snaps_raw = pd.read_csv(out_dir / "snapshots.csv")
    try:
        evr = _compute_expected_vs_realized(recs, snaps_raw, window_days=365)
    except Exception:
        evr = pd.DataFrame(columns=["date", "expected", "realized"])
    return totals, evr


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--param", required=True, choices=list(DEFAULTS.keys()))
    p.add_argument("--values", default=None,
                   help="comma-separated values to sweep (overrides defaults)")
    p.add_argument("--runs-dir", default="data/curator_runs/5y-quarterly")
    p.add_argument("--out", default=None,
                   help="output HTML path (default: docs/sweep_<param>.html)")
    p.add_argument("--benchmarks", nargs="*", default=["SPY"])
    p.add_argument("--base-max-weight", type=float, default=0.50)
    p.add_argument("--base-risk-aversion", type=float, default=0.5)
    args = p.parse_args(argv)

    values = ([float(v) for v in args.values.split(",")]
              if args.values else DEFAULTS[args.param])
    out_path = Path(args.out) if args.out else Path(f"docs/sweep_{args.param}.html")

    tmp = Path(tempfile.mkdtemp(prefix="sweep_"))
    try:
        curves: dict[float, pd.Series] = {}
        evrs: dict[float, pd.DataFrame] = {}
        for v in values:
            print(f"  {args.param} = {v}", file=sys.stderr)
            totals, evr = run_one(args.param, v, args.runs_dir, tmp,
                                  args.base_max_weight, args.base_risk_aversion)
            curves[v] = totals
            evrs[v] = evr

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
            summary.append((v, final, ret, ann, sharpe))

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
        # Second figure: per-rebalance expected μᵀw vs forward-1y realized
        # annualized return, one solid+dashed pair per variant. Same hue
        # palette as the equity-curve chart so the reader can map across.
        # The last few rebalances have NaN realized (1y forward window
        # not complete within the backtest window).
        fig_evr = go.Figure()
        for i, (v, evr) in enumerate(evrs.items()):
            if evr.empty:
                continue
            color = PALETTE[i % len(PALETTE)]
            fig_evr.add_trace(go.Scatter(
                x=evr["date"], y=evr["expected"],
                name=f"{args.param}={v} expected",
                mode="lines+markers", legendgroup=f"{v}",
                line={"color": color, "width": 1.5, "dash": "dash"},
            ))
            fig_evr.add_trace(go.Scatter(
                x=evr["date"], y=evr["realized"],
                name=f"{args.param}={v} realized",
                mode="lines+markers", legendgroup=f"{v}", showlegend=False,
                line={"color": color, "width": 2},
            ))
        fig_evr.update_layout(
            title="Expected vs realized annualized return per rebalance "
                  "(solid = realized 1y forward; dashed = optimizer μᵀw)",
            xaxis_title="rebalance date",
            yaxis_title="annualized return",
            yaxis_tickformat=".0%",
            height=520,
            plot_bgcolor="#fafafa",
            margin={"t": 60, "b": 60, "l": 80, "r": 30},
        )

        fig.update_layout(
            title=f"Curator backtest swept across {args.param} "
                  f"({start.date()} to {end.date()})",
            xaxis_title="date",
            yaxis_title="portfolio value ($)",
            yaxis_tickformat="$,.0f",
            height=600,
            plot_bgcolor="#fafafa",
            margin={"t": 60, "b": 60, "l": 80, "r": 30},
        )

        # Summary table.
        # Identify the current investor_profile.md default value for this
        # param so the corresponding table row can be rendered in bold.
        if args.param == "risk_aversion":
            default_v = args.base_risk_aversion
        elif args.param == "max_weight":
            default_v = args.base_max_weight
        else:  # lookback — read the live default from _starter.json
            import json as _json
            _starter = _json.loads((Path(args.runs_dir) / "_starter.json").read_text())
            default_v = float(_starter.get("lookback_years", 1.3))

        def _fmt_row(v, final, ret, ann, sharpe):
            tr = "<tr style='font-weight:bold;'>" if abs(v - default_v) < 1e-9 else "<tr>"
            return (
                f"{tr}<td>{v}</td><td>${final:,.0f}</td>"
                f"<td>{ret*100:+.1f}%</td><td>{ann*100:+.1f}%</td>"
                f"<td>{sharpe:.2f}</td></tr>"
            )

        rows = "".join(_fmt_row(v, final, ret, ann, sharpe)
                       for v, final, ret, ann, sharpe in summary)
        table = (
            f"<h2>Summary</h2><table style='border-collapse:collapse;font-size:14px;'>"
            f"<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
            f"<th style='padding:4px 12px;'>{args.param}</th>"
            f"<th style='padding:4px 12px;'>Final value</th>"
            f"<th style='padding:4px 12px;'>Total return</th>"
            f"<th style='padding:4px 12px;'>Annualized</th>"
            f"<th style='padding:4px 12px;'>Sharpe</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"<p style='font-size:13px;color:#666;'>Sharpe = (annualized return − "
            f"{RISK_FREE_RATE * 100:.0f}% risk-free) / annualized daily-return σ × √252.</p>"
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
            + fig.to_html(full_html=False, include_plotlyjs="cdn",
                          config={"displayModeBar": False})
            + (fig_evr.to_html(full_html=False, include_plotlyjs=False,
                               config={"displayModeBar": False})
               if args.param == "risk_aversion" else "")
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
