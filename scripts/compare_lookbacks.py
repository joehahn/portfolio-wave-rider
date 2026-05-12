"""Walk-forward backtest swept across lookback_period (years).

For each LB in LOOKBACKS_YEARS, runs a 12-month monthly-rebalance
walk-forward with mean_variance λ=1, recomputing μ and Σ from a window
of length LB years at each rebalance. Wave-stage tilts come from
data/wave_history.csv (same as the headline backtest).

The ticker universe is determined per lookback: a ticker is included
only if its history extends back at least LB years before the backtest
start date. With backtest START = today − 1y, the constraints today
are:

  NUKZ launched 2024-01-24, so it can only be included for LB ≤ 1.3y.
  RKLB started 2020-11-24, so it drops at LB = 5y.

That means short-LB runs use all 12 tickers, runs at 2-4y use 11
(NUKZ dropped), and the 5y run uses 10 (NUKZ + RKLB dropped). The
universes are not identical across the sweep, which is the price of
honestly extending the lookback range; see the chart caption.

Output: data/backtest/lookback_comparison.html and docs/lookback_comparison.html.
"""
from __future__ import annotations

import pathlib
import numpy as np
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go

from src.portfolio import (
    compute_returns, optimize_portfolio, _fetch_benchmark_curves,
    _render_nav_strip, TICKER_WAVE,
)

TICKERS = ["AGG", "BIL", "IAU", "GOOGL", "RKLB", "NVDA", "MSFT", "BOTZ", "ARKG", "QTUM", "NUKZ", "VIG"]
LOOKBACKS_YEARS = [0.25, 0.5, 0.75, 1.0, 1.3, 2.0, 3.0, 4.0, 5.0]
LAMBDA = 1.0
END = pd.Timestamp.today().normalize()
START = (END - pd.DateOffset(years=1)).normalize()
INITIAL_USD = 50_000.0
MAX_WEIGHT = 0.25
RISK_FREE = 0.04
WAVE_HISTORY_PATH = "data/wave_history.csv"


def wave_views_at(wh_df: pd.DataFrame, date: pd.Timestamp,
                  tickers: list[str]) -> dict[str, str] | None:
    """Build {ticker: stage} from the most recent wave_history row at-or-before date."""
    relevant = wh_df[wh_df["date"] <= date]
    if relevant.empty:
        return None
    latest_date = relevant["date"].max()
    latest = relevant[relevant["date"] == latest_date]
    wave_to_stage = dict(zip(latest["wave"], latest["stage"]))
    return {t: wave_to_stage.get(TICKER_WAVE.get(t, "general_markets"), "neutral")
            for t in tickers}


def tickers_with_history(all_prices: pd.DataFrame, lookback_years: float,
                         start_date: pd.Timestamp) -> list[str]:
    """Tickers whose first valid price is at or before
    (start_date − lookback_years), i.e. those with enough history to
    support a full LB-year lookback at the start of the backtest."""
    earliest_needed = start_date - pd.Timedelta(days=int(round(365 * lookback_years)))
    out = []
    for t in TICKERS:
        s = all_prices[t].dropna()
        if len(s) > 0 and s.index[0] <= earliest_needed:
            out.append(t)
    return out


def run_walk_forward(all_prices: pd.DataFrame, daily_dates,
                     lookback_years: float, wh_df: pd.DataFrame,
                     tickers: list[str]) -> pd.Series:
    """Walk-forward backtest with this lookback and ticker universe."""
    sub_prices = all_prices[tickers].dropna()
    shares = None
    values = []
    last_rebalance_month: int | None = None
    lookback_days = int(round(365 * lookback_years))
    for date in daily_dates:
        if date not in sub_prices.index:
            continue
        is_new_month = date.month != last_rebalance_month
        is_first = date == daily_dates[0]
        if is_new_month or (is_first and shares is None):
            lookback_start = date - pd.Timedelta(days=lookback_days)
            slice_prices = sub_prices.loc[lookback_start:date]
            if len(slice_prices) < 30:
                continue
            returns = compute_returns(slice_prices)
            opt = optimize_portfolio(
                returns, objective="mean_variance", risk_free_rate=RISK_FREE,
                max_weight=MAX_WEIGHT, risk_aversion=LAMBDA,
                wave_views=wave_views_at(wh_df, date, tickers),
            )
            if not opt.get("success"):
                continue
            weights = opt["weights"]
            pv = INITIAL_USD if shares is None else sum(
                shares[t] * float(sub_prices.loc[date, t]) for t in tickers
            )
            shares = {t: weights[t] * pv / float(sub_prices.loc[date, t]) for t in tickers}
            last_rebalance_month = date.month
        if shares is not None:
            total = sum(shares[t] * float(sub_prices.loc[date, t]) for t in tickers)
            values.append((date, total))
    return pd.Series({d: v for d, v in values}, name=f"LB={lookback_years}y")


# Fetch enough history for the longest lookback at the start of the window.
max_lookback_days = int(round(365 * max(LOOKBACKS_YEARS) + 60))
fetch_start = START - pd.Timedelta(days=max_lookback_days)
print(f"fetching prices for {len(TICKERS)} tickers from {fetch_start.date()} ...")
raw = yf.download(TICKERS, start=fetch_start, end=END + pd.Timedelta(days=1),
                  auto_adjust=True, progress=False, group_by="column")
# Keep per-ticker NaN at the leading edge so we can subset per lookback;
# don't call the final dropna() that would clip to NUKZ's earliest date.
all_prices = raw["Close"].dropna(how="all").ffill()

# daily_dates is the same across all sweeps: business days in the
# backtest window. Use the subset of all_prices that has every ticker
# available within [START, END] (NUKZ launched in Jan 2024, so all
# tickers have prices by START = today − 1y).
full_window_prices = all_prices.dropna()
daily_dates = full_window_prices.loc[START:END].index
print(f"{len(daily_dates)} trading days in [{START.date()}, {END.date()}]")

print(f"loading wave history from {WAVE_HISTORY_PATH} ...")
wh_df = pd.read_csv(WAVE_HISTORY_PATH, parse_dates=["date"])

curves: dict[float, pd.Series] = {}
universes: dict[float, list[str]] = {}
for lb in LOOKBACKS_YEARS:
    universe = tickers_with_history(all_prices, lb, START)
    universes[lb] = universe
    print(f"running walk-forward, lookback={lb}y ({len(universe)} tickers) ...")
    curves[lb] = run_walk_forward(all_prices, daily_dates, lb, wh_df, universe)

spy = _fetch_benchmark_curves(["SPY"], daily_dates[0], daily_dates[-1], INITIAL_USD)["SPY"]


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

stats = {lb: summarize(curve) for lb, curve in curves.items()}
spy_return = float(spy.iloc[-1] / spy.iloc[0] - 1.0)

# Rebalance dates: first trading day of each month within the window.
rebalance_dates = []
_last_month: int | None = None
for d in daily_dates:
    if d.month != _last_month:
        rebalance_dates.append(d)
        _last_month = d.month

# Figure: portfolio value over time, one line per lookback + SPY.
fig = go.Figure()
n = len(LOOKBACKS_YEARS)
for i, lb in enumerate(LOOKBACKS_YEARS):
    # short-lookback (momentum-chasing) = warm red; long-lookback
    # (smooth, value-tilted) = cool blue. Linear interpolation across
    # the 9 lookbacks.
    t = i / max(n - 1, 1)
    r = int(220 * (1 - t)); g = int(80 + 30 * t); b = int(60 + 195 * t)
    color = f"rgb({r},{g},{b})"
    s = curves[lb]
    n_tk = len(universes[lb])
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                             name=f"LB={lb}y ({n_tk} tickers)",
                             line={"width": 2, "color": color},
                             hovertemplate=f"LB={lb}y<br>%{{x|%Y-%m-%d}}<br>$%{{y:,.0f}}<extra></extra>"))
fig.add_trace(go.Scatter(x=spy.index, y=spy.values, mode="lines",
                         name="SPY (rebased)", line={"width": 1.5, "color": "#66c266", "dash": "dash"},
                         hovertemplate="SPY<br>%{x|%Y-%m-%d}<br>$%{y:,.0f}<extra></extra>"))

# No-rebalance counterfactual: take lookback=1.3y (headline) first-month
# allocation and hold for the full window. Same treatment as the
# backtest dashboard and other sweeps.
_headline_lb = 1.3
_headline_tickers = universes[_headline_lb]
_headline_prices = all_prices[_headline_tickers].dropna()
_first = daily_dates[0]
_lookback_start = _first - pd.Timedelta(days=int(round(365 * _headline_lb)))
_returns = compute_returns(_headline_prices.loc[_lookback_start:_first])
_opt = optimize_portfolio(
    _returns, objective="mean_variance", risk_free_rate=RISK_FREE,
    max_weight=MAX_WEIGHT, risk_aversion=LAMBDA,
    wave_views=wave_views_at(wh_df, _first, _headline_tickers),
)
_weights = _opt["weights"]
_init_shares = {t: _weights[t] * INITIAL_USD / float(_headline_prices.loc[_first, t])
                for t in _headline_tickers}
_no_rebal = pd.Series(
    {d: sum(_init_shares[t] * float(_headline_prices.loc[d, t]) for t in _headline_tickers)
     for d in daily_dates},
    name="No rebalancing",
)
fig.add_trace(go.Scatter(x=_no_rebal.index, y=_no_rebal.values, mode="lines",
                         name="No rebalancing (buy-and-hold, LB=1.3y initial)",
                         line={"width": 1.5, "color": "#8c564b", "dash": "dashdot"},
                         hovertemplate="No rebalance<br>%{x|%Y-%m-%d}<br>$%{y:,.0f}<extra></extra>"))

# Rebalance indicators
for d in rebalance_dates:
    fig.add_vline(x=d, line_dash="dot", line_width=2, line_color="#ff7f0e",
                  layer="below")
fig.add_trace(go.Scatter(
    x=[None], y=[None], mode="lines",
    line={"dash": "dot", "width": 2, "color": "#ff7f0e"},
    name="Rebalance",
))

# Pad x-axis 2% so edge data isn't clipped against the frame.
_x_span = daily_dates[-1] - daily_dates[0]
_x_pad = _x_span * 0.02
fig.update_xaxes(range=[daily_dates[0] - _x_pad, daily_dates[-1] + _x_pad])
fig.update_layout(
    title=f"Portfolio value over time, mean_variance λ=1 swept across lookback_period "
          f"(executed {END.date()})",
    xaxis_title="date",
    yaxis_title="$",
    height=600,
    hovermode="closest",
    legend={"title_text": "Lookback (years)"},
)

# Summary table.
rows = "".join(
    f"<tr><td style='text-align:right;font-family:monospace'>lookback = {lb}y</td>"
    f"<td style='text-align:right;font-family:monospace'>{len(universes[lb])}</td>"
    f"<td style='text-align:right;font-family:monospace'>${stats[lb]['final']:,.0f}</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lb]['return']*100:+.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lb]['vol']*100:.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lb]['sharpe']:.2f}</td>"
    f"<td style='text-align:right;font-family:monospace'>{stats[lb]['max_dd']*100:.2f}%</td>"
    f"<td style='text-align:right;font-family:monospace'>{(stats[lb]['return']-spy_return)*100:+.2f}pp</td>"
    f"</tr>"
    for lb in LOOKBACKS_YEARS
)
table_html = (
    f"<h2>Summary, {START.date()} to {END.date()}, mean_variance λ=1, organic wave-tilts</h2>"
    f"<table style='border-collapse:collapse;font-size:0.95em'>"
    f"<thead><tr style='border-bottom:1px solid #888'>"
    f"<th style='padding:4px 12px;text-align:right'>lookback</th>"
    f"<th style='padding:4px 12px;text-align:right'>tickers</th>"
    f"<th style='padding:4px 12px;text-align:right'>Final value</th>"
    f"<th style='padding:4px 12px;text-align:right'>Realized return</th>"
    f"<th style='padding:4px 12px;text-align:right'>Annualized vol</th>"
    f"<th style='padding:4px 12px;text-align:right'>Realized Sharpe</th>"
    f"<th style='padding:4px 12px;text-align:right'>Max drawdown</th>"
    f"<th style='padding:4px 12px;text-align:right'>vs SPY</th>"
    f"</tr></thead><tbody>{rows}</tbody></table>"
    f"<p style='color:#666;font-size:0.9em;max-width:65em'>"
    f"Shorter lookbacks (≤ 1 year) chase recent momentum: the μ estimate "
    f"weighs the most recent surges heavily, so newly accelerating waves "
    f"get more weight, but the resulting weights are noisier. Longer "
    f"lookbacks smooth out short-term swings at the cost of diluting "
    f"recent dynamics. "
    f"SPY benchmark return over the same window: {spy_return*100:+.2f}%. "
    f"<br><br><b>Ticker-universe note:</b> a ticker is included only if "
    f"it has at least LB years of price history at the start of the "
    f"backtest. NUKZ launched 2024-01-24, so it drops for LB ≥ 2y; "
    f"RKLB started 2020-11-24, so it also drops at LB = 5y. Lines with "
    f"different ticker counts aren't strictly comparable — the LB ≥ 2y "
    f"runs are missing the nuclear-energy bucket entirely, which is the "
    f"strongest wave tilt over this 12-month window."
    f"</p>"
)

# Write the HTML.
out_paths = [
    pathlib.Path("data/backtest/lookback_comparison.html"),
    pathlib.Path("docs/lookback_comparison.html"),
]
chart_caption = (
    f"<p style='color:#666;font-size:0.9em;max-width:65em;margin:0 auto;padding:0 1.5em;'>"
    f"<i>Walk-forward 12-month backtest run nine times, once per lookback "
    f"window. Each line is the same simulation (mean_variance λ=1, time-varying "
    f"wave tilts from data/wave_history.csv) with a different lookback for "
    f"estimating μ and Σ at each monthly rebalance. The ticker universe "
    f"shrinks for longer lookbacks: LB ≥ 2y drops NUKZ (launched 2024-01-24), "
    f"LB = 5y also drops RKLB. SPY rebased to share the starting value. "
    f"Orange dotted vertical lines mark rebalance dates.</i>"
    f"</p>"
)

nav_html = _render_nav_strip("lookback")

for p in out_paths:
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(p), include_plotlyjs="cdn")
    html = p.read_text(encoding="utf-8")
    p.write_text(html.replace("<body>", "<body>\n" + nav_html, 1), encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write("\n" + chart_caption + "\n" + table_html + "\n")
    print(f"wrote {p}")

print()
print(f"{'LB':>6} {'Tk':>3} {'Final':>11} {'Return':>9} {'Vol':>7} {'Sharpe':>7} {'MaxDD':>8} {'vs SPY':>9}")
for lb in LOOKBACKS_YEARS:
    s = stats[lb]
    print(f"{lb:>5.2f}y {len(universes[lb]):>3d} {s['final']:>11,.0f} {s['return']*100:>+8.2f}% {s['vol']*100:>6.2f}% "
          f"{s['sharpe']:>7.2f} {s['max_dd']*100:>+7.2f}% {(s['return']-spy_return)*100:>+8.2f}pp")
print(f"   SPY {' ':>3} {spy.iloc[-1]:>11,.0f} {spy_return*100:>+8.2f}%")
