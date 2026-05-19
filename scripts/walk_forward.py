"""Walk-forward stability check on the sweep defaults.

For each of risk_aversion, lookback, max_weight: re-run the backtest at
every sweep value, split the resulting daily-portfolio-value series in
half (at the midpoint of the date range), and compute per-half metrics
(annualized return, max drawdown, Sharpe, Calmar) for the first and
second halves separately.

The point of the split is to ask: if we picked the param value that
won by Sharpe (or Calmar) on the full 5y window, would that same value
also be the winner if we'd only had the first half of the data? If yes,
the choice is stable across regimes; if no, the "best" value is at
least partly fit to one regime.

Writes a markdown report to data/reports/walk_forward_check.md.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from src.portfolio import curator_backtest

RISK_FREE_RATE = 0.04

DEFAULTS = {
    "risk_aversion": [0.0, 0.33, 0.5, 0.67, 1.0, 2.0, 3.0, 10.0],
    "lookback":      [0.5, 1.0, 1.5, 2.0, 3.0, 5.0],
    "max_weight":    [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00],
}

LIVE_DEFAULTS = {
    "risk_aversion": 0.5,
    "lookback": 1.5,
    "max_weight": 0.70,
}


def run_one(param: str, value: float, runs_dir: str, tmp: Path,
            base_max_weight: float, base_risk_aversion: float) -> pd.Series:
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
    curator_backtest(**kw)
    snaps = pd.read_csv(out_dir / "snapshots.csv", parse_dates=["date"])
    return snaps.groupby("date")["total_value"].first().sort_index()


def metrics_for(s: pd.Series) -> dict:
    initial = float(s.iloc[0])
    final = float(s.iloc[-1])
    start, end = s.index[0], s.index[-1]
    days = (end - start).days
    ret = (final / initial) - 1.0
    ann = (final / initial) ** (365.25 / days) - 1.0 if days > 0 else 0.0
    daily_ret = s.pct_change().dropna()
    ann_vol = float(daily_ret.std() * np.sqrt(252))
    sharpe = (ann - RISK_FREE_RATE) / ann_vol if ann_vol > 0 else float("nan")
    running_peak = s.cummax()
    drawdown = (s / running_peak) - 1.0
    mdd = float(drawdown.min())
    calmar = ann / abs(mdd) if mdd < 0 else float("nan")
    return {"ann": ann, "mdd": mdd, "sharpe": sharpe, "calmar": calmar}


def split_at_midpoint(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Split the series into two halves by row count (not calendar)."""
    n = len(s)
    mid = n // 2
    return s.iloc[:mid], s.iloc[mid:]


def render_table(param: str, results: dict[float, dict]) -> str:
    live = LIVE_DEFAULTS[param]
    lines = [
        f"## {param}",
        "",
        "| value | H1 ann | H1 Sharpe | H1 Calmar | H2 ann | H2 Sharpe | H2 Calmar |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for v, m in results.items():
        marker = " **default**" if abs(v - live) < 1e-9 else ""
        lines.append(
            f"| {v}{marker} | {m['h1']['ann']*100:+.1f}% | {m['h1']['sharpe']:.2f} "
            f"| {m['h1']['calmar']:.2f} | {m['h2']['ann']*100:+.1f}% "
            f"| {m['h2']['sharpe']:.2f} | {m['h2']['calmar']:.2f} |"
        )
    # Winners per half by Sharpe and Calmar. Ties (within 0.005) are
    # resolved in favor of the live default if it ties, otherwise the
    # smallest value (more conservative).
    def _winner(half: str, metric: str) -> float:
        scored = [(v, results[v][half][metric]) for v in results]
        best = max(s for _, s in scored)
        tied = [v for v, s in scored if abs(s - best) < 5e-3]
        if abs(live - tied[0]) < 1e-9 or live in tied:
            return live
        return tied[0]
    by_h1_sharpe = _winner('h1', 'sharpe')
    by_h2_sharpe = _winner('h2', 'sharpe')
    by_h1_calmar = _winner('h1', 'calmar')
    by_h2_calmar = _winner('h2', 'calmar')
    lines.append("")
    lines.append(
        f"**Winners:** H1 Sharpe={by_h1_sharpe}, H2 Sharpe={by_h2_sharpe}; "
        f"H1 Calmar={by_h1_calmar}, H2 Calmar={by_h2_calmar}. "
        f"Live default: **{live}**."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    runs_dir = "data/curator_runs/5y-sweep-cap08"
    base_max_weight = 0.70
    base_risk_aversion = 0.5

    sections: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="walkforward_"))
    try:
        for param, values in DEFAULTS.items():
            print(f"=== {param} ===", file=sys.stderr)
            results: dict[float, dict] = {}
            for v in values:
                print(f"  {param} = {v}", file=sys.stderr)
                s = run_one(param, v, runs_dir, tmp, base_max_weight, base_risk_aversion)
                h1, h2 = split_at_midpoint(s)
                results[v] = {"h1": metrics_for(h1), "h2": metrics_for(h2)}
            sections.append(render_table(param, results))

        # The split date is the same for every variant (same date index).
        # Grab it from the last series.
        h1_end = h1.index[-1].date()
        h2_start = h2.index[0].date()

    finally:
        # Clean up tempdir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    report = (
        "# Walk-forward stability check\n\n"
        "Each sweep variant's daily-portfolio-value series is split at the\n"
        f"midpoint by row count: H1 ends {h1_end}, H2 begins {h2_start}.\n"
        "Per-half annualized return, Sharpe, and Calmar are then computed\n"
        "separately. The question is whether the live default value also\n"
        "wins by Sharpe or Calmar on each half independently — if yes, the\n"
        "choice is stable across regimes; if no, the full-window choice is\n"
        "at least partly fit to one half.\n\n"
        + "\n".join(sections)
    )

    out_path = Path("data/reports/walk_forward_check.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\nwrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
