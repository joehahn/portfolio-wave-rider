"""Walk-forward backtest swept across concentration_cap (max_weight) values.

For each max_weight in MAX_WEIGHTS, runs a 12-month monthly-rebalance
walk-forward on the 12-ticker watchlist with mean_variance λ=1 and
time-varying wave tilts from data/wave_history.csv. Outputs:

  - one Plotly chart: 4 portfolio-value curves + SPY benchmark
  - a summary table (realized return, vol, Sharpe, max DD, vs SPY)
  - 4 per-ticker $-gain breakdown tables (one per cap)

Output file: data/backtest/max_weight_comparison.html and
             docs/max_weight_comparison.html.
"""
from __future__ import annotations

import pathlib
import numpy as np
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go

from src.portfolio import (
    compute_returns, optimize_portfolio, _fetch_benchmark_curves,
    TICKER_WAVE, _render_nav_strip,
)

TICKERS = ["AGG", "BIL", "IAU", "GOOGL", "RKLB", "NVDA", "MSFT", "BOTZ", "ARKG", "QTUM", "NUKZ", "VIG"]
MAX_WEIGHTS = [0.25, 0.33, 0.50, 1.00]
LAMBDA = 1.0
# Rolling 12-month window ending today, so reruns automatically pick up
# the most recent prices and the most recent wave_history.csv classifications.
END = pd.Timestamp.today().normalize()
START = (END - pd.DateOffset(years=1)).normalize()
INITIAL_USD = 50_000.0
LOOKBACK_YEARS = 3
RISK_FREE = 0.04


def wave_views_at(wh_df: pd.DataFrame, date: pd.Timestamp) -> dict[str, str] | None:
    """Build {ticker: stage} from the most recent wave_history row at-or-before date."""
    if wh_df is None:
        return None
    relevant = wh_df[wh_df["date"] <= date]
    if relevant.empty:
        return None
    latest_date = relevant["date"].max()
    latest = relevant[relevant["date"] == latest_date]
    wave_to_stage = dict(zip(latest["wave"], latest["stage"]))
    return {t: wave_to_stage.get(TICKER_WAVE.get(t, "general_markets"), "neutral")
            for t in TICKERS}


def run_walk_forward(prices: pd.DataFrame, daily_dates, wh_df, max_weight: float):
    """Returns (totals_series, snapshots_df). snapshots_df has columns
    date, ticker, shares, price for the per-ticker P&L computation."""
    shares = None
    totals_rows = []
    snap_rows = []
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
                max_weight=max_weight, risk_aversion=LAMBDA,
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
            totals_rows.append((date, total))
            for t in TICKERS:
                snap_rows.append({
                    "date": date, "ticker": t,
                    "shares": shares[t], "price": float(prices.loc[date, t]),
                })
    totals = pd.Series({d: v for d, v in totals_rows}, name=f"max={max_weight}")
    snaps = pd.DataFrame(snap_rows)
    return totals, snaps


def per_ticker_gain(snaps: pd.DataFrame) -> dict[str, float]:
    """Same attribution as dashboard chart 4: sum_t prior_shares * price.diff()."""
    out: dict[str, float] = {}
    for t, sub in snaps.groupby("ticker"):
        sub = sub.sort_values("date").reset_index(drop=True)
        pnl = (sub["shares"].shift(1) * sub["price"].diff()).fillna(0).sum()
        out[t] = float(pnl)
    return out


def per_ticker_final_value(snaps: pd.DataFrame) -> dict[str, float]:
    """Final $ value per ticker at end of backtest (= end_shares * end_price).
    Reflects the portfolio's actual end-of-window composition."""
    last_date = snaps["date"].max()
    last = snaps[snaps["date"] == last_date]
    return {row["ticker"]: float(row["shares"]) * float(row["price"]) for _, row in last.iterrows()}


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


# Load price history once.
print(f"fetching prices for {len(TICKERS)} tickers, {LOOKBACK_YEARS}y + window...")
fetch_start = START - pd.Timedelta(days=365 * LOOKBACK_YEARS + 30)
raw = yf.download(TICKERS, start=fetch_start, end=END + pd.Timedelta(days=1),
                  auto_adjust=True, progress=False, group_by="column")
prices = raw["Close"].dropna(how="all").ffill().dropna()
daily_dates = prices.loc[START:END].index
print(f"{len(daily_dates)} trading days in [{START.date()}, {END.date()}]")

wh_df = pd.read_csv("data/wave_history.csv", parse_dates=["date"])

curves: dict[float, pd.Series] = {}
gains: dict[float, dict[str, float]] = {}
final_values: dict[float, dict[str, float]] = {}
for mw in MAX_WEIGHTS:
    print(f"running mean_variance walk-forward, max_weight={mw} ...")
    totals, snaps = run_walk_forward(prices, daily_dates, wh_df, mw)
    curves[mw] = totals
    gains[mw] = per_ticker_gain(snaps)
    final_values[mw] = per_ticker_final_value(snaps)

spy = _fetch_benchmark_curves(["SPY"], daily_dates[0], daily_dates[-1], INITIAL_USD)["SPY"]
spy_return = float(spy.iloc[-1] / spy.iloc[0] - 1.0)
stats = {mw: summarize(curve) for mw, curve in curves.items()}

# Compute rebalance dates: first trading day of each month within
# the simulation window. Same for every cap.
rebalance_dates = []
_last_month: int | None = None
for d in daily_dates:
    if d.month != _last_month:
        rebalance_dates.append(d)
        _last_month = d.month

# Figure: 4 portfolio-value lines + SPY.
fig = go.Figure()
n = len(MAX_WEIGHTS)
for i, mw in enumerate(MAX_WEIGHTS):
    t = i / max(n - 1, 1)
    r = int(60 + 160 * t); g = int(80 + 100 * t); b = int(220 - 160 * t)
    color = f"rgb({r},{g},{b})"
    s = curves[mw]
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                             name=f"max_weight={mw}",
                             line={"width": 2, "color": color},
                             hovertemplate=f"max={mw}<br>%{{x|%Y-%m-%d}}<br>$%{{y:,.0f}}<extra></extra>"))
fig.add_trace(go.Scatter(x=spy.index, y=spy.values, mode="lines",
                         name="SPY (rebased)", line={"width": 1.5, "color": "#444", "dash": "dash"},
                         hovertemplate="SPY<br>%{x|%Y-%m-%d}<br>$%{y:,.0f}<extra></extra>"))

# Rebalance indicators: orange dotted vertical lines at each rebalance
# date, behind all the line traces. A dummy zero-data scatter trace
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
# rightmost data points sit inside the plotting frame.
_x_span = daily_dates[-1] - daily_dates[0]
_x_pad = _x_span * 0.02
_x_range = [daily_dates[0] - _x_pad, daily_dates[-1] + _x_pad]
fig.update_xaxes(range=_x_range)
fig.update_layout(
    title=f"Portfolio value over time, mean_variance λ=1 swept across concentration_cap "
          f"(executed {END.date()})",
    xaxis_title="date",
    yaxis_title="$",
    height=600,
    hovermode="closest",
    legend={"title_text": "Concentration cap"},
)

# Summary table.
summary_rows = "".join(
    f"<tr><td style='text-align:right;font-family:monospace'>max_weight = {mw}</td>"
    f"<td style='text-align:right;font-family:monospace'>${stats[mw]['final']:,.0f}</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[mw]['return']*100:+.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[mw]['vol']*100:.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[mw]['sharpe']:.2f}</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[mw]['max_dd']*100:.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{(stats[mw]['return']-spy_return)*100:+.2f}pp</td>"
    f"</tr>"
    for mw in MAX_WEIGHTS
)
summary_table = (
    f"<h2>Summary, {START.date()} to {END.date()}, 12-ticker watchlist, mean_variance λ=1, organic wave-tilts</h2>"
    f"<table style='border-collapse:collapse;font-size:0.95em'>"
    f"<thead><tr style='border-bottom:1px solid #888'>"
    f"<th style='padding:4px 12px;text-align:right'>Concentration cap</th>"
    f"<th style='padding:4px 12px;text-align:right'>Final value</th>"
    f"<th style='padding:4px 12px;text-align:right'>Realized return</th>"
    f"<th style='padding:4px 12px;text-align:right'>Annualized vol</th>"
    f"<th style='padding:4px 12px;text-align:right'>Realized Sharpe</th>"
    f"<th style='padding:4px 12px;text-align:right'>Max drawdown</th>"
    f"<th style='padding:4px 12px;text-align:right'>vs SPY</th>"
    f"</tr></thead><tbody>{summary_rows}</tbody></table>"
    f"<p style='color:#666;font-size:0.9em;max-width:65em'>"
    f"SPY benchmark return over the same window: {spy_return*100:+.2f}%. "
    f"Lowering the concentration cap forces more diversification at the "
    f"cost of constraining the optimizer's preferred weights; raising it "
    f"lets the optimizer concentrate but amplifies estimation error in μ."
    f"</p>"
)

# Per-ticker $-gain breakdown: 2×2 grid of bar charts, one per cap.
# All four panels share the same ticker order (sorted by gain at the
# default cap=0.25 so the eye can track each name across panels) and
# the same y-axis range so the "loosening the cap = bigger swings"
# point reads visually.
from plotly.subplots import make_subplots

base_order = [t for t, _ in sorted(gains[0.25].items(), key=lambda kv: kv[1], reverse=True)]
y_min = min(min(g.values()) for g in gains.values())
y_max = max(max(g.values()) for g in gains.values())
y_pad = max(abs(y_min), abs(y_max)) * 0.05
y_range = [y_min - y_pad, y_max + y_pad]

# Final $ value 2x2: each panel shows end-of-backtest dollar value per
# ticker (= end_shares × end_price), same ticker order as the gain
# panels below so the eye can pair "ended at $X" with "of which $Y was
# gain". Shared y-axis across the four panels makes the "loosening the
# cap concentrates ownership in fewer names" point read visually.
fv_y_max = max(max(v.values()) for v in final_values.values())
fv_range = [0, fv_y_max * 1.05]

fig_fv = make_subplots(
    rows=2, cols=2,
    subplot_titles=[
        f"max_weight = {mw}  |  Final: ${stats[mw]['final']:,.0f}  ({stats[mw]['return']*100:+.1f}%)"
        for mw in MAX_WEIGHTS
    ],
    shared_yaxes=True,
    horizontal_spacing=0.08,
    vertical_spacing=0.15,
)
positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
for (mw, (r, c)) in zip(MAX_WEIGHTS, positions):
    fv = final_values[mw]
    values = [fv.get(t, 0.0) for t in base_order]
    fig_fv.add_trace(
        go.Bar(x=base_order, y=values, marker_color="#1f77b4",
               showlegend=False,
               hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"),
        row=r, col=c,
    )
    fig_fv.update_xaxes(tickangle=0, row=r, col=c)
    fig_fv.update_yaxes(range=fv_range,
                        title_text="$ value" if c == 1 else None,
                        row=r, col=c)

fig_fv.update_layout(
    title=f"Per-ticker final $ value at end of backtest, by concentration cap "
          f"(executed {END.date()})",
    height=700,
    margin={"t": 80},
)

fv_intro = (
    f"<p style='color:#666;font-size:0.9em;max-width:65em'>"
    f"For each cap, the bars show each ticker's dollar value at the "
    f"end of the 12-month window (= end shares × end price). Sum across "
    f"bars in each panel matches that cap's final portfolio value in the "
    f"summary table above. Same ticker order (by gain at cap=0.25) and "
    f"shared y-axis as the per-ticker $-gain breakdown below, so you "
    f"can pair 'ended at $X' with 'of which $Y was gain' across panels."
    f"</p>"
)

fig2 = make_subplots(
    rows=2, cols=2,
    subplot_titles=[
        f"max_weight = {mw}  |  Final: ${stats[mw]['final']:,.0f}  ({stats[mw]['return']*100:+.1f}%)"
        for mw in MAX_WEIGHTS
    ],
    shared_yaxes=True,
    horizontal_spacing=0.08,
    vertical_spacing=0.15,
)
positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
for (mw, (r, c)) in zip(MAX_WEIGHTS, positions):
    g = gains[mw]
    values = [g[t] for t in base_order]
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in values]
    fig2.add_trace(
        go.Bar(x=base_order, y=values, marker_color=colors,
               showlegend=False,
               hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"),
        row=r, col=c,
    )
    fig2.update_xaxes(tickangle=0, row=r, col=c)
    fig2.update_yaxes(range=y_range, zeroline=True,
                      zerolinewidth=1, zerolinecolor="#888",
                      title_text="$ gain" if c == 1 else None,
                      row=r, col=c)

fig2.update_layout(
    title=f"Per-ticker $-gain breakdown, by concentration cap "
          f"(executed {END.date()})",
    height=700,
    margin={"t": 80},
)

breakdown_intro = (
    f"<p style='color:#666;font-size:0.9em;max-width:65em'>"
    f"For each cap, the bars show each ticker's cumulative dollar gain "
    f"over the 12-month window. Per-ticker P&amp;L = sum_t (prior shares) "
    f"× (price change), so the values reflect changing position sizes "
    f"across monthly rebalances. Sum across bars in each panel matches "
    f"that cap's realized portfolio gain in the summary table above. "
    f"All four panels share the same ticker order (by gain at cap=0.25) "
    f"and the same y-axis range, so loosening the cap is visible as "
    f"taller bars on a few names."
    f"</p>"
)

# Write the HTML.
out_paths = [
    pathlib.Path("data/backtest/max_weight_comparison.html"),
    pathlib.Path("docs/max_weight_comparison.html"),
]
chart_caption = (
    f"<p style='color:#666;font-size:0.9em;max-width:65em;margin:0 auto;padding:0 1.5em;'>"
    f"<i>Walk-forward 12-month backtest run four times, once per concentration cap. "
    f"Each line is the same simulation (mean_variance λ=1, organic wave-history tilts) "
    f"with a different per-position max weight. SPY rebased to share the starting value. "
    f"Orange dotted vertical lines mark rebalance dates.</i>"
    f"</p>"
)

nav_html = _render_nav_strip("max_weight")

for p in out_paths:
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(p), include_plotlyjs="cdn")
    # Inject the cross-page nav strip just after <body> so this page links
    # back to the live dashboard, backtest, and lambda sweep.
    html = p.read_text(encoding="utf-8")
    p.write_text(html.replace("<body>", "<body>\n" + nav_html, 1), encoding="utf-8")
    fig_fv_html = fig_fv.to_html(include_plotlyjs=False, full_html=False)
    fig2_html = fig2.to_html(include_plotlyjs=False, full_html=False)
    with p.open("a", encoding="utf-8") as f:
        f.write("\n" + chart_caption + "\n"
                + summary_table + "\n"
                + fv_intro + "\n" + fig_fv_html + "\n"
                + breakdown_intro + "\n" + fig2_html + "\n")
    print(f"wrote {p}")

print()
print(f"{'cap':>6} {'Final':>11} {'Return':>9} {'Vol':>7} {'Sharpe':>7} {'MaxDD':>8} {'vs SPY':>9}")
for mw in MAX_WEIGHTS:
    s = stats[mw]
    print(f"{mw:>6.2f} {s['final']:>11,.0f} {s['return']*100:>+8.2f}% {s['vol']*100:>6.2f}% "
          f"{s['sharpe']:>7.2f} {s['max_dd']*100:>+7.2f}% {(s['return']-spy_return)*100:>+8.2f}pp")
print(f"   SPY {spy.iloc[-1]:>11,.0f} {spy_return*100:>+8.2f}%")
