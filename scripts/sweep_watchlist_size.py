"""Aggregate the max_watchlist_size sweep into docs/sweep_max_watchlist_size.html.

Reads each cap variant's snapshots.csv (from running the math replay
once per cap), builds an overlay equity-curve chart plus a summary
table (final value, total %, annualized %, Sharpe), and renders an
HTML page with the same nav strip and visual style as the other three
sweep dashboards.

Per-cap input layout:
  cap=12: data/backtest_curator_5y/snapshots.csv (existing, from the
          standard /run-backtest)
  others: data/curator_runs/5y-sweep-cap{NN}/_backtest/snapshots.csv

If a cap's snapshots.csv is missing, that cap is silently skipped (so
the page can render mid-build before all curator calls have completed).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from src.portfolio import _fetch_benchmark_curves, _nav_strip

RISK_FREE_RATE = 0.04

CAPS: list[tuple[int, Path]] = [
    (5,  Path("data/curator_runs/5y-sweep-cap05/_backtest/snapshots.csv")),
    (8,  Path("data/curator_runs/5y-sweep-cap08/_backtest/snapshots.csv")),
    (12, Path("data/backtest_curator_5y/snapshots.csv")),
    (16, Path("data/curator_runs/5y-sweep-cap16/_backtest/snapshots.csv")),
    (24, Path("data/curator_runs/5y-sweep-cap24/_backtest/snapshots.csv")),
]

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]


def main() -> int:
    curves: dict[int, pd.Series] = {}
    for cap, path in CAPS:
        if not path.exists():
            print(f"  cap={cap}: {path} missing — skipping", file=sys.stderr)
            continue
        snaps = pd.read_csv(path, parse_dates=["date"])
        totals = snaps.groupby("date")["total_value"].first().sort_index()
        curves[cap] = totals
        print(f"  cap={cap}: {len(totals)} days, final ${totals.iloc[-1]:,.0f}",
              file=sys.stderr)

    if not curves:
        print("error: no per-cap snapshots found; run the math replays first.",
              file=sys.stderr)
        return 1

    first = next(iter(curves.values()))
    start, end = first.index[0], first.index[-1]
    initial = float(first.iloc[0])

    summary: list[tuple[int, float, float, float, float]] = []
    for cap, s in curves.items():
        final = float(s.iloc[-1])
        ret = (final / initial) - 1.0
        ann = (final / initial) ** (365.25 / (end - start).days) - 1.0
        daily_ret = s.pct_change().dropna()
        ann_vol = float(daily_ret.std() * np.sqrt(252))
        sharpe = (ann - RISK_FREE_RATE) / ann_vol if ann_vol > 0 else float("nan")
        summary.append((cap, final, ret, ann, sharpe))

    fig = go.Figure()
    for i, (cap, s) in enumerate(curves.items()):
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values, name=f"max_watchlist_size={cap}",
            mode="lines", line={"color": PALETTE[i % len(PALETTE)], "width": 2},
        ))
    for b, curve in _fetch_benchmark_curves(["SPY"], start, end, initial).items():
        fig.add_trace(go.Scatter(
            x=curve.index, y=curve.values, name=f"{b} benchmark",
            mode="lines", line={"color": "#10b981", "width": 1.5, "dash": "dot"},
        ))
    fig.update_layout(
        title=f"Curator backtest swept across max_watchlist_size "
              f"({start.date()} to {end.date()})",
        xaxis_title="date",
        yaxis_title="portfolio value ($)",
        yaxis_tickformat="$,.0f",
        height=600,
        plot_bgcolor="#fafafa",
        margin={"t": 60, "b": 60, "l": 80, "r": 30},
    )

    default_cap = 12

    def _fmt_row(cap, final, ret, ann, sharpe):
        tr = "<tr style='font-weight:bold;'>" if cap == default_cap else "<tr>"
        return (
            f"{tr}<td>{cap}</td><td>${final:,.0f}</td>"
            f"<td>{ret*100:+.1f}%</td><td>{ann*100:+.1f}%</td>"
            f"<td>{sharpe:.2f}</td></tr>"
        )

    rows = "".join(_fmt_row(*r) for r in summary)
    table = (
        "<h2>Summary</h2><table style='border-collapse:collapse;font-size:14px;'>"
        "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
        "<th style='padding:4px 12px;'>max_watchlist_size</th>"
        "<th style='padding:4px 12px;'>Final value</th>"
        "<th style='padding:4px 12px;'>Total return</th>"
        "<th style='padding:4px 12px;'>Annualized</th>"
        "<th style='padding:4px 12px;'>Sharpe</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p style='font-size:13px;color:#666;'>Sharpe = (annualized return − "
        f"{RISK_FREE_RATE * 100:.0f}% risk-free) / annualized daily-return σ × √252.</p>"
    )

    nav = _nav_strip("sweep_max_watchlist_size.html")

    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Sweep: max_watchlist_size</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:1em 1.5em;color:#222;}'
        'th,td{border-bottom:1px solid #eee;}</style></head><body>'
        + nav +
        '<h1>Parameter sweep: <code>max_watchlist_size</code></h1>'
        '<p style="color:#555;max-width:780px;">Unlike the three optimizer-knob '
        'sweeps, this one re-fires the curator at each cap value because the cap '
        'shapes the curator\'s decisions, not just the optimizer\'s response. '
        'Each curve is a separate curator-driven walk-forward through the 5y '
        'window, with starter watchlist <code>[AAPL, MSFT, GOOGL, NVDA, SPY]</code> '
        'and only the <code>max_watchlist_size</code> input changing. cap=12 is '
        'the project default.</p>'
        + fig.to_html(full_html=False, include_plotlyjs="cdn",
                      config={"displayModeBar": False})
        + table
        + '</body></html>'
    )
    out_path = Path("docs/sweep_max_watchlist_size.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
