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

import pandas as pd
import plotly.graph_objects as go

from src.portfolio import curator_backtest, _fetch_benchmark_curves

DEFAULTS = {
    "risk_aversion": [0.0, 0.33, 1.0, 3.3, 10.0, 33.0],
    "lookback":      [0.5, 1.0, 1.3, 2.0, 3.0],
    "max_weight":    [0.10, 0.15, 0.25, 0.40, 1.00],
}

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]


def run_one(param: str, value: float, runs_dir: str, tmp: Path,
            base_max_weight: float, base_risk_aversion: float) -> pd.Series:
    """Replay the curator runs with one parameter swapped to ``value``.
    Returns a date-indexed Series of total portfolio value."""
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
    return totals


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--param", required=True, choices=list(DEFAULTS.keys()))
    p.add_argument("--values", default=None,
                   help="comma-separated values to sweep (overrides defaults)")
    p.add_argument("--runs-dir", default="data/curator_runs/5y-quarterly")
    p.add_argument("--out", default=None,
                   help="output HTML path (default: docs/sweep_<param>.html)")
    p.add_argument("--benchmarks", nargs="*", default=["SPY"])
    p.add_argument("--base-max-weight", type=float, default=0.25)
    p.add_argument("--base-risk-aversion", type=float, default=1.0)
    args = p.parse_args(argv)

    values = ([float(v) for v in args.values.split(",")]
              if args.values else DEFAULTS[args.param])
    out_path = Path(args.out) if args.out else Path(f"docs/sweep_{args.param}.html")

    tmp = Path(tempfile.mkdtemp(prefix="sweep_"))
    try:
        curves: dict[float, pd.Series] = {}
        for v in values:
            print(f"  {args.param} = {v}", file=sys.stderr)
            curves[v] = run_one(args.param, v, args.runs_dir, tmp,
                                args.base_max_weight, args.base_risk_aversion)

        # All curves share the same x-axis. Build summary first.
        first = next(iter(curves.values()))
        start, end = first.index[0], first.index[-1]
        initial = float(first.iloc[0])
        summary = []
        for v, s in curves.items():
            final = float(s.iloc[-1])
            ret = (final / initial) - 1.0
            ann = (final / initial) ** (365.25 / (end - start).days) - 1.0
            summary.append((v, final, ret, ann))

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
        rows = "".join(
            f"<tr><td>{v}</td><td>${final:,.0f}</td>"
            f"<td>{ret*100:+.1f}%</td><td>{ann*100:+.1f}%</td></tr>"
            for v, final, ret, ann in summary
        )
        table = (
            f"<h2>Summary</h2><table style='border-collapse:collapse;font-size:14px;'>"
            f"<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
            f"<th style='padding:4px 12px;'>{args.param}</th>"
            f"<th style='padding:4px 12px;'>Final value</th>"
            f"<th style='padding:4px 12px;'>Total return</th>"
            f"<th style='padding:4px 12px;'>Annualized</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

        page = (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Sweep: {args.param}</title>'
            '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
            'max-width:1180px;margin:0 auto;padding:1em 1.5em;color:#222;}'
            'th,td{border-bottom:1px solid #eee;}</style></head><body>'
            f'<h1>Parameter sweep: <code>{args.param}</code></h1>'
            f'<p style="color:#555;max-width:780px;">Same curator JSONs replayed '
            f'through the optimizer at each <code>{args.param}</code> value. '
            f'All other knobs held at their <code>investor_profile.md</code> defaults. '
            f'Differences across curves isolate the optimizer\'s sensitivity to '
            f'<code>{args.param}</code>.</p>'
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
