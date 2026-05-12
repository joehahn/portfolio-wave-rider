"""Walk-forward backtest swept across mean_variance risk-aversion (lambda) values.

For each lambda in LAMBDAS, runs a 12-month monthly-rebalance walk-forward
on the 12-ticker watchlist with the mean_variance objective. Each rebalance
applies time-varying wave-stage tilts looked up as-of-date from
data/wave_history.csv (same source as the headline backtest). Aggregates
all curves into one Plotly chart plus a summary table.

Output: data/backtest/lambda_comparison.html and docs/lambda_comparison.html.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go

from src.portfolio import (
    compute_returns, optimize_portfolio, _fetch_benchmark_curves,
    _render_nav_strip, TICKER_WAVE,
)

TICKERS = ["AGG", "BIL", "IAU", "GOOGL", "RKLB", "NVDA", "MSFT", "BOTZ", "ARKG", "QTUM", "NUKZ", "VIG"]
LAMBDAS = [0.0, 0.33, 1.0, 3.3, 10.0, 33.3]
# Rolling 12-month window ending today, so reruns automatically pick up
# the most recent prices and the most recent wave_history.csv classifications.
END = pd.Timestamp.today().normalize()
START = (END - pd.DateOffset(years=1)).normalize()
INITIAL_USD = 50_000.0
LOOKBACK_YEARS = 1.3
MAX_WEIGHT = 0.25
RISK_FREE = 0.04
WAVE_HISTORY_PATH = "data/wave_history.csv"


def wave_views_at(wh_df: pd.DataFrame, date: pd.Timestamp) -> dict[str, str] | None:
    """Build {ticker: stage} from the most recent wave_history row at-or-before date.
    Mirrors the as-of-date lookup in src.portfolio.backtest so the lambda sweep
    sees the same time-varying tilts the headline backtest does."""
    relevant = wh_df[wh_df["date"] <= date]
    if relevant.empty:
        return None
    latest_date = relevant["date"].max()
    latest = relevant[relevant["date"] == latest_date]
    wave_to_stage = dict(zip(latest["wave"], latest["stage"]))
    return {
        t: wave_to_stage.get(TICKER_WAVE.get(t, "general_markets"), "neutral")
        for t in TICKERS
    }


def run_walk_forward(prices: pd.DataFrame, daily_dates, lam: float,
                     wh_df: pd.DataFrame) -> pd.Series:
    """Walk-forward backtest under mean_variance with this lambda. Returns
    a Series of portfolio total value indexed by trading day. wave_views are
    looked up as-of-date from wh_df at each monthly rebalance (first
    trading day of each new month)."""
    shares = None
    values = []
    last_rebalance_month: int | None = None
    for date in daily_dates:
        is_new_month = date.month != last_rebalance_month
        is_first = date == daily_dates[0]
        if is_new_month or (is_first and shares is None):
            lookback_start = date - pd.Timedelta(days=365 * LOOKBACK_YEARS)
            slice_prices = prices.loc[lookback_start:date]
            if len(slice_prices) < 30:
                continue
            returns = compute_returns(slice_prices)
            opt = optimize_portfolio(
                returns, objective="mean_variance", risk_free_rate=RISK_FREE,
                max_weight=MAX_WEIGHT, risk_aversion=lam,
                wave_views=wave_views_at(wh_df, date),
            )
            if not opt.get("success"):
                continue
            weights = opt["weights"]
            pv = INITIAL_USD if shares is None else sum(
                shares[t] * float(prices.loc[date, t]) for t in TICKERS
            )
            shares = {t: weights[t] * pv / float(prices.loc[date, t]) for t in TICKERS}
            last_rebalance_month = date.month
        if shares is not None:
            total = sum(shares[t] * float(prices.loc[date, t]) for t in TICKERS)
            values.append((date, total))
    return pd.Series({d: v for d, v in values}, name=f"λ={lam}")


# Fetch prices once.
print(f"fetching prices for {len(TICKERS)} tickers, {LOOKBACK_YEARS}y + window...")
fetch_start = START - pd.Timedelta(days=365 * LOOKBACK_YEARS + 30)
raw = yf.download(TICKERS, start=fetch_start, end=END + pd.Timedelta(days=1),
                  auto_adjust=True, progress=False, group_by="column")
prices = raw["Close"].dropna(how="all").ffill().dropna()
daily_dates = prices.loc[START:END].index
print(f"{len(daily_dates)} trading days in [{START.date()}, {END.date()}]")

# Load the as-of-date wave history once; reused at every rebalance.
print(f"loading wave history from {WAVE_HISTORY_PATH} ...")
wh_df = pd.read_csv(WAVE_HISTORY_PATH, parse_dates=["date"])

# Run a walk-forward per lambda.
curves: dict[float, pd.Series] = {}
for lam in LAMBDAS:
    print(f"running mean_variance walk-forward, λ={lam} ...")
    curves[lam] = run_walk_forward(prices, daily_dates, lam, wh_df)

# SPY benchmark, rebased to the same starting value.
spy = _fetch_benchmark_curves(["SPY"], daily_dates[0], daily_dates[-1], INITIAL_USD)["SPY"]

# Summary table.
def summarize(s: pd.Series) -> dict[str, float]:
    log_rets = np.log(s / s.shift(1)).dropna()
    realized = float(s.iloc[-1] / s.iloc[0] - 1.0)
    annualized_vol = float(log_rets.std() * np.sqrt(252))
    excess = log_rets - RISK_FREE / 252
    sharpe = float(excess.mean() / excess.std() * np.sqrt(252))
    peak = np.maximum.accumulate(s.values)
    max_dd = float(((s.values - peak) / peak).min())
    return {"final": float(s.iloc[-1]), "return": realized,
            "vol": annualized_vol, "sharpe": sharpe, "max_dd": max_dd}

stats = {lam: summarize(curve) for lam, curve in curves.items()}
spy_return = float(spy.iloc[-1] / spy.iloc[0] - 1.0)

# Compute rebalance dates: the first trading day of each month within
# the simulation window. Same dates for every λ since the walk-forward
# uses a fixed monthly cadence.
rebalance_dates = []
_last_month: int | None = None
for d in daily_dates:
    if d.month != _last_month:
        rebalance_dates.append(d)
        _last_month = d.month

# Figure: portfolio value over time, one line per λ + SPY.
fig = go.Figure()
# Use a color gradient from low-λ (return-greedy, red) to high-λ (variance-averse, blue).
n = len(LAMBDAS)
for i, lam in enumerate(LAMBDAS):
    # red -> purple -> blue gradient.
    t = i / max(n - 1, 1)
    r = int(220 * (1 - t)); g = int(80 + 30 * t); b = int(60 + 195 * t)
    color = f"rgb({r},{g},{b})"
    s = curves[lam]
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                             name=f"λ={lam}", line={"width": 2, "color": color},
                             hovertemplate=f"λ={lam}<br>%{{x|%Y-%m-%d}}<br>$%{{y:,.0f}}<extra></extra>"))
# SPY in light green, consistent across all backtest + sweep dashboards.
fig.add_trace(go.Scatter(x=spy.index, y=spy.values, mode="lines",
                         name="SPY (rebased)", line={"width": 1.5, "color": "#66c266", "dash": "dash"},
                         hovertemplate="SPY<br>%{x|%Y-%m-%d}<br>$%{y:,.0f}<extra></extra>"))

# No-rebalance counterfactual: take lambda=1's first-month allocation
# and hold it for the full window. One brown dashdot line, consistent
# with the backtest dashboard's no-rebalance treatment.
_first = daily_dates[0]
_lookback_start = _first - pd.Timedelta(days=365 * LOOKBACK_YEARS)
_returns = compute_returns(prices.loc[_lookback_start:_first])
_opt = optimize_portfolio(
    _returns, objective="mean_variance", risk_free_rate=RISK_FREE,
    max_weight=MAX_WEIGHT, risk_aversion=1.0,
    wave_views=wave_views_at(wh_df, _first),
)
_weights = _opt["weights"]
_init_shares = {t: _weights[t] * INITIAL_USD / float(prices.loc[_first, t]) for t in TICKERS}
_no_rebal = pd.Series(
    {d: sum(_init_shares[t] * float(prices.loc[d, t]) for t in TICKERS) for d in daily_dates},
    name="No rebalancing",
)
fig.add_trace(go.Scatter(x=_no_rebal.index, y=_no_rebal.values, mode="lines",
                         name="buy-and-hold (λ=1 initial)",
                         line={"width": 1.5, "color": "#8c564b", "dash": "dashdot"},
                         hovertemplate="No rebalance<br>%{x|%Y-%m-%d}<br>$%{y:,.0f}<extra></extra>"))

# Rebalance indicators: orange dotted vertical lines at each rebalance
# date, behind all the line traces. Same orange as the square markers
# on the live and backtest dashboards. A dummy zero-data scatter trace
# carries the "Rebalance" legend entry since add_vline shapes don't
# show up in the legend.
for d in rebalance_dates:
    fig.add_vline(x=d, line_dash="dot", line_width=2, line_color="#ff7f0e",
                  layer="below")
fig.add_trace(go.Scatter(
    x=[None], y=[None], mode="lines",
    line={"dash": "dot", "width": 2, "color": "#ff7f0e"},
    name="Rebalance",
))

# Pad the x-axis a couple percent on each side so the leftmost and
# rightmost data points sit inside the plotting frame rather than on
# the edge. Same pad applied to every sweep dashboard.
_x_span = daily_dates[-1] - daily_dates[0]
_x_pad = _x_span * 0.02
_x_range = [daily_dates[0] - _x_pad, daily_dates[-1] + _x_pad]
fig.update_xaxes(range=_x_range)
fig.update_layout(
    title=f"Portfolio value over time, mean_variance objective swept across λ "
          f"(executed {END.date()})",
    xaxis_title="date",
    yaxis_title="$",
    height=600,
    hovermode="closest",
    legend={"title_text": "Risk-aversion λ"},
)

# Build the summary table HTML.
rows = "".join(
    f"<tr><td style='text-align:right;font-family:monospace'>λ = {lam}</td>"
    f"<td style='text-align:right;font-family:monospace'>${stats[lam]['final']:,.0f}</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lam]['return']*100:+.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lam]['vol']*100:.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lam]['sharpe']:.2f}</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lam]['max_dd']*100:.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{(stats[lam]['return']-spy_return)*100:+.2f}pp</td>"
    f"</tr>"
    for lam in LAMBDAS
)
table_html = (
    f"<h2>Summary, {START.date()} to {END.date()}, 12-ticker watchlist with wave tilts</h2>"
    f"<table style='border-collapse:collapse;font-size:0.95em'>"
    f"<thead><tr style='border-bottom:1px solid #888'>"
    f"<th style='padding:4px 12px;text-align:right'>λ</th>"
    f"<th style='padding:4px 12px;text-align:right'>Final value</th>"
    f"<th style='padding:4px 12px;text-align:right'>Realized return</th>"
    f"<th style='padding:4px 12px;text-align:right'>Annualized vol</th>"
    f"<th style='padding:4px 12px;text-align:right'>Realized Sharpe</th>"
    f"<th style='padding:4px 12px;text-align:right'>Max drawdown</th>"
    f"<th style='padding:4px 12px;text-align:right'>vs SPY</th>"
    f"</tr></thead><tbody>{rows}</tbody></table>"
    f"<p style='color:#666;font-size:0.9em;max-width:65em'>"
    f"Each λ corresponds to a different point on the Markowitz efficient frontier. "
    f"λ → 0 ignores variance (return-greedy: equity-heavy, big swings); "
    f"λ → ∞ ignores return (variance-averse: defensive ballast). "
    f"SPY benchmark return over the same window: {spy_return*100:+.2f}%. "
    f"This is one path through history; the AI / data-center electricity narrative "
    f"drove tech and nuclear-energy ETFs hard over this specific 12-month window. "
    f"Backtest applies the as-of-date wave-stage tilts from data/wave_history.csv at each monthly rebalance — same as the headline backtest path. "
    f"</p>"
)

# Write the standalone HTML.
import pathlib
out_paths = [
    pathlib.Path("data/backtest/lambda_comparison.html"),
    pathlib.Path("docs/lambda_comparison.html"),
]
chart_caption = (
    f"<p style='color:#666;font-size:0.9em;max-width:65em;margin:0 auto;padding:0 1.5em;'>"
    f"<i>Walk-forward 12-month backtest run six times, once per λ (risk-aversion parameter "
    f"in the mean_variance utility μᵀw - λ·wᵀΣw). Each line is the same simulation with a "
    f"different λ, with time-varying wave-stage tilts looked up as-of-date from "
    f"data/wave_history.csv at each monthly rebalance — same source as the headline backtest. "
    f"SPY rebased to share the starting value. Orange dotted vertical lines mark rebalance dates.</i>"
    f"</p>"
)

nav_html = _render_nav_strip("lambda")

for p in out_paths:
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(p), include_plotlyjs="cdn")
    # Inject the cross-page nav strip just after <body> so this page links
    # back to the live dashboard, backtest, and concentration sweep.
    html = p.read_text(encoding="utf-8")
    p.write_text(html.replace("<body>", "<body>\n" + nav_html, 1), encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write("\n" + chart_caption + "\n" + table_html + "\n")
    print(f"wrote {p}")

print()
print(f"{'λ':>8} {'Final':>11} {'Return':>9} {'Vol':>7} {'Sharpe':>7} {'MaxDD':>8} {'vs SPY':>9}")
for lam in LAMBDAS:
    s = stats[lam]
    print(f"{lam:>8.2f} {s['final']:>11,.0f} {s['return']*100:>+8.2f}% {s['vol']*100:>6.2f}% "
          f"{s['sharpe']:>7.2f} {s['max_dd']*100:>+7.2f}% {(s['return']-spy_return)*100:>+8.2f}pp")
print(f"     SPY {spy.iloc[-1]:>11,.0f} {spy_return*100:>+8.2f}%")
