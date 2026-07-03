"""Aggregate the max_watchlist_size sweep into docs/sweep_max_watchlist_size.html.

Unlike the three optimizer-knob sweeps (risk_aversion, lookback,
concentration_cap), the cap shapes the *curator's* decisions, not just the
optimizer's response, so each cap needs its own curator-runs dir rather than a
re-replay of a shared one. This script replays each cap's runs dir through
``curator_backtest`` with the SAME held-constant optimizer config the other
three sweeps use (read from ``investor_profile.md``), so all four sweeps are
directly comparable over the same window.

All caps run over the canonical post-COVID window (2022-03-31 -> 2025-10-31,
15 quarterly curator calls), identical to the window in ``postcovid/`` that the
other three sweeps replay. Per-cap runs-dir layout:

    cap= 5: data/curator_runs/postcovid-cap05/
    cap= 8: data/curator_runs/postcovid/        (project default = canonical run)
    cap=12: data/curator_runs/postcovid-cap12/
    cap=16: data/curator_runs/postcovid-cap16/
    cap=24: data/curator_runs/postcovid-cap24/

A cap whose runs dir has no curation JSONs yet is silently skipped, so the page
can render mid-build before all curator calls have completed.
"""
from __future__ import annotations

import re as _re
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from src.portfolio import (
    _fetch_benchmark_curves,
    _nav_strip,
    curator_backtest,
    load_backtest_config,
    load_financial_model,
)

RISK_FREE_RATE = 0.04

# Cap -> curator-runs dir. cap=5 is the project default (postcovid-cap05/ is the
# canonical run the other three sweeps and the published backtest now replay);
# cap=8 is the preserved older default at postcovid/.
CAPS: list[tuple[int, Path]] = [
    (5,  Path("data/curator_runs/postcovid-cap05")),
    (8,  Path("data/curator_runs/postcovid")),
    (12, Path("data/curator_runs/postcovid-cap12")),
    (16, Path("data/curator_runs/postcovid-cap16")),
    (24, Path("data/curator_runs/postcovid-cap24")),
]

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]

# Held-constant optimizer config, same source of truth as sweep.py so the
# max_watchlist_size curves are directly comparable to the other three sweeps.
_FM = load_financial_model()
_BASE_MAX_WEIGHT = float(_FM["concentration_cap"])
_BASE_RISK_AVERSION = float(_FM["risk_aversion"])
_m = _re.match(r"(\d+(?:\.\d+)?)", str(_FM["lookback_period"]))
_BASE_LOOKBACK = float(_m.group(1)) if _m else 1.5


def _replay(runs_dir: Path, tmp: Path) -> pd.Series:
    """Replay one cap's curator runs dir through the optimizer at the live-config
    base and return a date-indexed Series of total portfolio value."""
    out_dir = tmp / runs_dir.name
    base_t_update = int(load_backtest_config()["t_update_days"])
    curator_backtest(
        runs_dir=str(runs_dir),
        out_dir=str(out_dir),
        max_weight=_BASE_MAX_WEIGHT,
        risk_aversion=_BASE_RISK_AVERSION,
        lookback_years_override=_BASE_LOOKBACK,
        t_update_days=base_t_update,
        benchmarks=[],
        always_include=_FM["always_include"],
    )
    snaps = pd.read_csv(out_dir / "snapshots.csv", parse_dates=["date"])
    return snaps.groupby("date")["total_value"].first().sort_index()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sweep_wls_"))
    curves: dict[int, pd.Series] = {}
    for cap, runs_dir in CAPS:
        if not list(runs_dir.glob("*-curation.json")):
            print(f"  cap={cap}: {runs_dir} has no curation JSONs — skipping",
                  file=sys.stderr)
            continue
        curves[cap] = _replay(runs_dir, tmp)
        print(f"  cap={cap}: {len(curves[cap])} days, "
              f"final ${curves[cap].iloc[-1]:,.0f}", file=sys.stderr)

    if not curves:
        print("error: no per-cap curator runs found; fire the curator calls first.",
              file=sys.stderr)
        return 1

    first = next(iter(curves.values()))
    start, end = first.index[0], first.index[-1]
    initial = float(first.iloc[0])

    summary: list[tuple[int, float, float, float, float, float, float]] = []
    for cap, s in curves.items():
        final = float(s.iloc[-1])
        ret = (final / initial) - 1.0
        ann = (final / initial) ** (365.25 / (end - start).days) - 1.0
        daily_ret = s.pct_change().dropna()
        ann_vol = float(daily_ret.std() * np.sqrt(252))
        sharpe = (ann - RISK_FREE_RATE) / ann_vol if ann_vol > 0 else float("nan")
        running_peak = s.cummax()
        drawdown = (s / running_peak) - 1.0
        mdd = float(drawdown.min())
        calmar = ann / abs(mdd) if mdd < 0 else float("nan")
        summary.append((cap, final, ret, ann, mdd, sharpe, calmar))

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
        template="seaborn",
        title=f"Plot 1. Curator backtest swept across max_watchlist_size "
              f"({start.date()} to {end.date()})",
        xaxis_title="date",
        yaxis_title="portfolio value ($)",
        yaxis_tickformat="$,.0f",
        height=600,
        margin={"t": 60, "b": 60, "l": 80, "r": 30},
    )

    default_cap = 5

    # Plot 2: final portfolio value vs max_watchlist_size, as a connected-dot
    # curve, with the project default cap marked.
    _pts = sorted((r[0], r[1]) for r in summary)  # (cap, final $)
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=[p[0] for p in _pts], y=[p[1] for p in _pts],
        mode="lines+markers", line={"color": "#1f77b4", "width": 2},
        marker={"size": 8}, showlegend=False,
        hovertemplate="max_watchlist_size=%{x}<br>final $%{y:,.0f}<extra></extra>",
    ))
    fig2.update_layout(
        template="seaborn",
        title="Plot 2. Final portfolio value vs max_watchlist_size",
        xaxis_title="max_watchlist_size",
        yaxis_title="final portfolio value ($)",
        yaxis_tickformat="$,.0f",
        height=420,
        margin={"t": 60, "b": 60, "l": 80, "r": 30},
    )

    def _fmt_row(cap, final, ret, ann, mdd, sharpe, calmar):
        tr = "<tr style='font-weight:bold;'>" if cap == default_cap else "<tr>"
        return (
            f"{tr}<td>{cap}</td><td>${final:,.0f}</td>"
            f"<td>{ret*100:+.1f}%</td><td>{ann*100:+.1f}%</td>"
            f"<td>{mdd*100:+.1f}%</td>"
            f"<td>{sharpe:.2f}</td><td>{calmar:.2f}</td></tr>"
        )

    rows = "".join(_fmt_row(*r) for r in summary)
    table = (
        "<h2>Summary</h2><table style='border-collapse:collapse;font-size:14px;'>"
        "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
        "<th style='padding:4px 12px;'>max_watchlist_size</th>"
        "<th style='padding:4px 12px;'>Final value</th>"
        "<th style='padding:4px 12px;'>Total return</th>"
        "<th style='padding:4px 12px;'>Annualized</th>"
        "<th style='padding:4px 12px;'>Max drawdown</th>"
        "<th style='padding:4px 12px;'>Sharpe</th>"
        "<th style='padding:4px 12px;'>Calmar</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p style='font-size:13px;color:#666;'>"
        f"<strong>Sharpe</strong> = (annualized return &minus; "
        f"{RISK_FREE_RATE * 100:.0f}% risk-free) / annualized daily-return "
        f"&sigma; &times; &radic;252.<br>"
        f"<strong>Calmar</strong> = annualized return / |max drawdown|; "
        f"penalizes deep drawdowns the way Sharpe doesn't.</p>"
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
        'Each curve is a separate curator-driven walk-forward over the same '
        'post-COVID window (2022-03-31 to 2025-10-31, 15 quarterly calls) the '
        'other three sweeps use, with starter watchlist '
        '<code>[AAPL, MSFT, GOOGL, NVDA, SPY]</code> and only the '
        '<code>max_watchlist_size</code> input changing. All other optimizer '
        'knobs are held at their <code>investor_profile.md</code> defaults, so '
        'the curves are directly comparable to the other sweeps. cap=5 is the '
        'project default (bolded).</p>'
        + fig.to_html(full_html=False, include_plotlyjs="cdn",
                      config={"displayModeBar": False})
        + fig2.to_html(full_html=False, include_plotlyjs=False,
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
