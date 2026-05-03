"""All portfolio math in one file.

Six public functions plus one orchestrator:

- ``fetch_prices`` — download adjusted-close prices from yfinance
- ``compute_returns`` — log-returns + annualized mean + covariance matrix
- ``optimize_portfolio`` — mean-variance optimization via scipy
- ``risk_metrics`` — Sharpe, vol, max drawdown, VaR, CVaR for a weight vector
- ``analyze`` — one-shot: fetch + returns + optimize + risk in one call
- ``snapshot_holdings`` — append daily $ values to data/snapshots.csv
- ``recommend_portfolio`` — append weekly weights to data/recommendations.csv
- ``build_dashboard`` — render a static HTML dashboard from the two CSVs plus the latest news payload

Functions pass DataFrames in-memory; there is no on-disk handle store. The
CLI calls ``analyze`` (or ``snapshot``/``recommend``/``dashboard``) once
per invocation.
"""

from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.optimize import minimize

TRADING_DAYS = 252

# Wave-cycle tilt multipliers applied to expected returns when the caller
# supplies a wave_views dict (ticker -> stage). Implements the profile's
# "ride the wave, exit before the crest" thesis: lean into early waves,
# trim late ones. Numbers are intentionally small and symmetric so the
# tilt nudges the optimizer rather than dominating it.
WAVE_STAGE_TILT = {
    "buildup":   1.20,   # early, under-owned — lean in hard
    "surge":     1.10,   # adoption compounding — lean in
    "peak":      0.80,   # enthusiasm priced in — trim
    "digestion": 0.90,   # post-crest hangover — mild underweight
    "neutral":   1.00,   # no wave signal — leave alone
}


def apply_wave_tilt(mu: pd.Series, wave_views: dict[str, str]) -> pd.Series:
    """Multiply annualized mean returns by each ticker's stage tilt."""
    tilted = mu.copy()
    for ticker, stage in wave_views.items():
        if ticker in tilted.index:
            tilted[ticker] = tilted[ticker] * WAVE_STAGE_TILT.get(stage, 1.0)
    return tilted


# ---------------------------------------------------------------------------
# Market data: fetch prices and turn them into a returns bundle.
# ---------------------------------------------------------------------------

def fetch_prices(tickers: list[str], period: str = "3y", interval: str = "1d") -> pd.DataFrame:
    """Download adjusted-close prices for the given tickers via yfinance."""
    if not tickers:
        raise ValueError("tickers must be non-empty")
    clean = [t.upper().strip() for t in tickers]
    data = yf.download(clean, period=period, interval=interval,
                       auto_adjust=True, progress=False, group_by="column")
    if data.empty:
        raise RuntimeError(f"yfinance returned no data for {clean} over {period}")

    # yfinance returns a MultiIndex when there are 2+ tickers, a flat index for 1.
    prices = data["Close"] if isinstance(data.columns, pd.MultiIndex) \
        else data[["Close"]].rename(columns={"Close": clean[0]})
    return prices.dropna(how="all").ffill().dropna()


def compute_returns(prices: pd.DataFrame, frequency: str = "daily") -> dict[str, Any]:
    """Compute log-returns + annualized mean + covariance from a prices frame."""
    factor = {"daily": TRADING_DAYS, "weekly": 52, "monthly": 12}[frequency]
    log_returns = np.log(prices / prices.shift(1)).dropna()
    return {
        "log_returns": log_returns,
        "mean": log_returns.mean() * factor,
        "cov": log_returns.cov() * factor,
        "annualization": factor,
    }


# ---------------------------------------------------------------------------
# Mean-variance optimizer. Three objectives: max_sharpe, min_variance, target_return.
# Long-only by default, with an optional per-asset cap.
# ---------------------------------------------------------------------------

def optimize_portfolio(
    returns: dict[str, Any],
    objective: str = "max_sharpe",
    risk_free_rate: float = 0.04,
    target_return: float | None = None,
    max_weight: float = 1.0,
    min_weight: float = 0.0,
    wave_views: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Solve the mean-variance problem and return weights + summary stats."""
    if objective not in {"max_sharpe", "min_variance", "target_return"}:
        raise ValueError(f"unknown objective: {objective}")
    if objective == "target_return" and target_return is None:
        raise ValueError("target_return is required when objective='target_return'")

    tickers = list(returns["mean"].index)
    mean_series = apply_wave_tilt(returns["mean"], wave_views) if wave_views else returns["mean"]
    mu = mean_series.to_numpy(dtype=float)
    sigma = returns["cov"].to_numpy(dtype=float)
    n = len(tickers)

    # Weights must sum to 1; target-return adds a second equality constraint.
    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    if objective == "target_return":
        constraints.append({"type": "eq", "fun": lambda w: float(w @ mu) - target_return})

    bounds = [(min_weight, max_weight)] * n
    w0 = np.full(n, 1.0 / n)

    if objective == "max_sharpe":
        # Minimize -Sharpe.
        def neg_sharpe(w: np.ndarray) -> float:
            vol = float(np.sqrt(w @ sigma @ w))
            return 0.0 if vol < 1e-10 else -(float(w @ mu) - risk_free_rate) / vol
        result = minimize(neg_sharpe, w0, method="SLSQP", bounds=bounds, constraints=constraints)
    else:
        # min_variance and target_return both minimize portfolio variance.
        result = minimize(lambda w: w @ sigma @ w, w0, method="SLSQP",
                          bounds=bounds, constraints=constraints)

    if not result.success:
        return {"success": False, "message": result.message, "objective": objective}

    w = result.x
    vol = float(np.sqrt(w @ sigma @ w))
    ret = float(w @ mu)
    weights = {t: float(w[i]) for i, t in enumerate(tickers)}
    at_bound = [t for i, t in enumerate(tickers)
                if abs(w[i] - max_weight) < 1e-4 or abs(w[i] - min_weight) < 1e-4]

    return {
        "success": True,
        "objective": objective,
        "weights": weights,
        "expected_annual_return": ret,
        "annual_volatility": vol,
        "sharpe_ratio": (ret - risk_free_rate) / vol if vol > 1e-10 else None,
        "assets_at_boundary": at_bound,
        "applied_wave_views": wave_views or None,
        "concentration_warning": (
            f"Top holding is {max(weights, key=weights.get)} at "
            f"{max(weights.values()) * 100:.1f}%."
            if max(weights.values()) > 0.5 else None
        ),
    }


# ---------------------------------------------------------------------------
# Risk metrics. Apply a weight vector to a returns bundle.
# ---------------------------------------------------------------------------

def risk_metrics(
    returns: dict[str, Any],
    weights: dict[str, float],
    risk_free_rate: float = 0.04,
    var_confidence: float = 0.95,
) -> dict[str, Any]:
    """Portfolio Sharpe, vol, max drawdown, VaR, CVaR for the given weights."""
    log_returns = returns["log_returns"]
    missing = [t for t in log_returns.columns if t not in weights]
    if missing:
        raise ValueError(f"weights missing for tickers: {missing}")
    w = np.array([weights[t] for t in log_returns.columns], dtype=float)
    port = pd.Series(log_returns.values @ w, index=log_returns.index)

    ann_ret = float(port.mean() * TRADING_DAYS)
    ann_vol = float(port.std() * np.sqrt(TRADING_DAYS))
    sharpe = (ann_ret - risk_free_rate) / ann_vol if ann_vol > 1e-10 else None
    equity = (1 + port).cumprod()
    max_dd = float(((equity - equity.cummax()) / equity.cummax()).min())

    alpha = 1 - var_confidence
    var = float(np.quantile(port.values, alpha))
    below_var = port.values[port.values <= var]

    return {
        "annual_return": ann_ret,
        "annual_volatility": ann_vol,
        "sharpe_ratio": float(sharpe) if sharpe is not None else None,
        "max_drawdown": max_dd,
        "var_1d": var,
        "cvar_1d": float(below_var.mean()) if below_var.size else var,
        "var_confidence": var_confidence,
        "n_observations": len(port),
        "period_start": str(port.index[0].date()),
        "period_end": str(port.index[-1].date()),
    }


# ---------------------------------------------------------------------------
# One-shot orchestrator: fetch + returns + optimize + risk in one call.
# This is what the /review-portfolio skill calls via Bash.
# ---------------------------------------------------------------------------

def analyze(
    tickers: list[str],
    period: str = "3y",
    objective: str = "max_sharpe",
    max_weight: float = 0.25,
    risk_free_rate: float = 0.04,
    wave_views: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the full pipeline and return a single JSON-serializable dict."""
    prices = fetch_prices(tickers, period=period)
    returns = compute_returns(prices)
    opt = optimize_portfolio(
        returns, objective=objective, risk_free_rate=risk_free_rate,
        max_weight=max_weight, wave_views=wave_views,
    )
    risk = risk_metrics(returns, opt["weights"], risk_free_rate=risk_free_rate) \
        if opt.get("success") else None

    return {
        "tickers": list(prices.columns),
        "period": {
            "start": str(prices.index[0].date()),
            "end": str(prices.index[-1].date()),
            "n_observations": len(prices),
        },
        "last_prices": {t: float(prices[t].iloc[-1]) for t in prices.columns},
        "annualized_mean_return": {k: float(v) for k, v in returns["mean"].items()},
        "annualized_volatility": {
            t: float(np.sqrt(returns["cov"].loc[t, t])) for t in returns["cov"].index
        },
        "optimization": opt,
        "risk": risk,
    }


# ---------------------------------------------------------------------------
# Day 0 setup. Convert a thesis-driven dollar allocation to shares and write
# the initial holdings.csv. Pure function: prices are passed in so the unit
# test stays offline.
# ---------------------------------------------------------------------------

def initialize_holdings(
    allocations: dict[str, float],
    prices: dict[str, float],
    holdings_path: str = "holdings.csv",
) -> dict[str, Any]:
    """Convert ticker -> dollars + ticker -> price into ticker -> shares,
    then overwrite ``holdings_path`` with a fresh ``ticker, shares`` CSV.

    Allocations and prices must cover the same tickers. Shares are stored
    as floats (4 decimals) since most modern brokers support fractional
    shares. Tickers with $0 allocated keep shares=0 (still appear in the
    file as a watchlist entry).
    """
    if not allocations:
        raise ValueError("allocations must be non-empty")
    missing = [t for t in allocations if t not in prices]
    if missing:
        raise ValueError(f"prices missing for tickers: {missing}")
    if any(d < 0 for d in allocations.values()):
        raise ValueError("allocations must be non-negative")

    rows = []
    total = 0.0
    for ticker, dollars in allocations.items():
        price = float(prices[ticker])
        shares = round(dollars / price, 4) if price > 0 and dollars > 0 else 0.0
        value = round(shares * price, 2)
        total += value
        rows.append({"ticker": ticker.upper(), "shares": shares,
                     "dollars_allocated": float(dollars), "price": price, "value": value})

    df = pd.DataFrame(rows)
    o_path = Path(holdings_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    df[["ticker", "shares"]].to_csv(o_path, index=False)

    return {
        "out_path": str(o_path),
        "total_invested": round(total, 2),
        "total_requested": round(sum(allocations.values()), 2),
        "holdings": {r["ticker"]: {"shares": r["shares"], "price": r["price"],
                                   "value": r["value"], "dollars_allocated": r["dollars_allocated"]}
                     for r in rows},
    }


# ---------------------------------------------------------------------------
# Time-series writers. snapshot = daily, recommend = weekly.
# ---------------------------------------------------------------------------

def snapshot_holdings(
    holdings_path: str = "holdings.csv",
    out_path: str = "data/snapshots.csv",
    date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Append today's per-ticker $ values to a snapshots CSV.

    Reads `holdings_path` (columns: ticker, shares), fetches the most
    recent close for each ticker via yfinance, and appends one row per
    ticker to `out_path` with columns:
        date, ticker, shares, price, value, total_value

    Tickers with shares=0 are still recorded so the file doubles as a
    price log before the user actually invests. If `date` already
    appears in the snapshot file, the call is a no-op unless force=True.
    """
    h_path = Path(holdings_path)
    if not h_path.exists():
        raise FileNotFoundError(f"holdings file not found: {h_path}")
    holdings = pd.read_csv(h_path)
    if "ticker" not in holdings.columns or "shares" not in holdings.columns:
        raise ValueError(f"{h_path} must have columns: ticker, shares")
    holdings["ticker"] = holdings["ticker"].str.upper().str.strip()
    holdings["shares"] = holdings["shares"].astype(float)

    snap_date = pd.Timestamp(date).date() if date else pd.Timestamp.today().date()

    o_path = Path(out_path)
    existing = pd.read_csv(o_path) if o_path.exists() else None
    if existing is not None and (existing["date"] == str(snap_date)).any():
        if not force:
            return {"skipped": True, "date": str(snap_date),
                    "reason": "snapshot already exists; pass force=True to overwrite"}
        existing = existing[existing["date"] != str(snap_date)]

    # Pull a short window so a stale weekend/holiday still resolves to a real close.
    tickers = holdings["ticker"].tolist()
    raw = yf.download(tickers, period="7d", interval="1d",
                      auto_adjust=True, progress=False, group_by="column")
    if raw.empty:
        raise RuntimeError(f"yfinance returned no data for {tickers}")
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
        else raw[["Close"]].rename(columns={"Close": tickers[0]})
    last_close = closes.ffill().iloc[-1]

    rows = []
    total = 0.0
    for _, row in holdings.iterrows():
        price = float(last_close.get(row["ticker"], float("nan")))
        value = price * row["shares"] if not np.isnan(price) else 0.0
        total += value
        rows.append({"date": str(snap_date), "ticker": row["ticker"],
                     "shares": row["shares"], "price": price, "value": value})
    for r in rows:
        r["total_value"] = total

    new_rows = pd.DataFrame(rows)
    out = pd.concat([existing, new_rows], ignore_index=True) if existing is not None else new_rows
    o_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(o_path, index=False)

    return {
        "date": str(snap_date),
        "tickers": tickers,
        "total_value": total,
        "n_rows_appended": len(new_rows),
        "out_path": str(o_path),
    }


def recommend_portfolio(
    holdings_path: str = "holdings.csv",
    out_path: str = "data/recommendations.csv",
    period: str = "3y",
    max_weight: float = 0.25,
    risk_free_rate: float = 0.04,
    objective: str = "max_sharpe",
    date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run a lightweight optimization and append per-ticker weights to a CSV.

    The "automation" sibling of /review-portfolio: pure Python, no news,
    no wave-stage tilts. Universe = the tickers listed in `holdings_path`.
    Schema appended to `out_path`:
        date, ticker, weight, expected_return, annual_volatility,
        sharpe_ratio, objective

    Idempotent on date (skip unless force=True). Run /review-portfolio
    when you want fresh wave-stage tilts and a written report.
    """
    h_path = Path(holdings_path)
    if not h_path.exists():
        raise FileNotFoundError(f"holdings file not found: {h_path}")
    holdings = pd.read_csv(h_path)
    if "ticker" not in holdings.columns:
        raise ValueError(f"{h_path} must have a 'ticker' column")
    tickers = holdings["ticker"].str.upper().str.strip().tolist()

    rec_date = pd.Timestamp(date).date() if date else pd.Timestamp.today().date()
    o_path = Path(out_path)
    existing = pd.read_csv(o_path) if o_path.exists() else None
    if existing is not None and (existing["date"] == str(rec_date)).any():
        if not force:
            return {"skipped": True, "date": str(rec_date),
                    "reason": "recommendation already exists; pass force=True to overwrite"}
        existing = existing[existing["date"] != str(rec_date)]

    result = analyze(tickers, period=period, objective=objective,
                     max_weight=max_weight, risk_free_rate=risk_free_rate)
    opt = result["optimization"]
    if not opt.get("success"):
        raise RuntimeError(f"optimization failed: {opt.get('message')}")

    rows = [
        {
            "date": str(rec_date),
            "ticker": ticker,
            "weight": weight,
            "expected_return": opt["expected_annual_return"],
            "annual_volatility": opt["annual_volatility"],
            "sharpe_ratio": opt["sharpe_ratio"],
            "objective": objective,
        }
        for ticker, weight in opt["weights"].items()
    ]
    new_rows = pd.DataFrame(rows)
    out = pd.concat([existing, new_rows], ignore_index=True) if existing is not None else new_rows
    o_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(o_path, index=False)

    return {
        "date": str(rec_date),
        "tickers": tickers,
        "weights": opt["weights"],
        "expected_annual_return": opt["expected_annual_return"],
        "annual_volatility": opt["annual_volatility"],
        "sharpe_ratio": opt["sharpe_ratio"],
        "n_rows_appended": len(new_rows),
        "out_path": str(o_path),
    }


# ---------------------------------------------------------------------------
# Static HTML dashboard. Reads the two append-only CSVs and emits one file
# the user can open in a browser. No server, no Streamlit.
# ---------------------------------------------------------------------------

# Wave bucket display order for the news section. Matches the profile's
# nearest-impact-first convention (rockets > robotics > engineered biology >
# quantum > fusion), with the current AI wave first and general_markets last.
_WAVE_DISPLAY_ORDER = [
    "AI", "rockets_spacecraft", "robotics", "engineered_biology",
    "quantum", "nuclear_fusion", "general_markets",
]


def _render_news_html(news_path: Path) -> str:
    """Render data/news_latest.json as a clickable HTML headlines block.

    Returns an empty string if the file does not exist or is empty. The
    block is plain HTML (no Plotly) so it appends cleanly after Plotly's
    write_html output.
    """
    if not news_path.exists():
        return ""
    try:
        data = json.loads(news_path.read_text())
    except (json.JSONDecodeError, OSError):
        return ""

    per_ticker = data.get("per_ticker") or {}
    if not per_ticker:
        return ""

    run_date = data.get("date") or "unknown date"

    def _wave_rank(ticker: str) -> tuple[int, str]:
        wave = per_ticker[ticker].get("wave_bucket", "general_markets")
        rank = _WAVE_DISPLAY_ORDER.index(wave) if wave in _WAVE_DISPLAY_ORDER else 99
        return (rank, ticker)

    ordered_tickers = sorted(per_ticker.keys(), key=_wave_rank)

    parts = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:980px;margin:1.5em auto 3em;padding:0 1.5em;color:#222;">',
        '<h2 style="border-bottom:1px solid #ddd;padding-bottom:0.3em;">'
        f'Latest news <span style="color:#888;font-weight:normal;font-size:0.7em;">'
        f'(last reviewed {_html.escape(str(run_date))})</span></h2>',
        '<p style="color:#666;font-size:0.9em;">Headlines from the most recent '
        '<code>/review-portfolio</code> run, grouped by ticker. Click any '
        'headline to open the source. The dashboard updates this section '
        'each time the skill runs.</p>',
    ]

    for ticker in ordered_tickers:
        info = per_ticker[ticker]
        bullets = info.get("bullets") or []
        if not bullets:
            continue
        wave = info.get("wave_bucket", "general_markets")
        parts.append(
            f'<h3 style="margin-top:1.5em;color:#222;">{_html.escape(ticker)} '
            f'<small style="color:#999;font-weight:normal;">({_html.escape(wave)})</small></h3>'
        )
        parts.append('<ul style="line-height:1.55;padding-left:1.2em;">')
        for b in bullets:
            summary = _html.escape(str(b.get("summary", "")))
            url = _html.escape(str(b.get("url", "#")), quote=True)
            source = _html.escape(str(b.get("source", "")))
            date = _html.escape(str(b.get("date", "")))
            meta = " · ".join(x for x in (source, date) if x)
            parts.append(
                f'<li><a href="{url}" target="_blank" rel="noopener" '
                f'style="color:#1a73e8;text-decoration:none;">{summary}</a> '
                f'<small style="color:#999;">{meta}</small></li>'
            )
        parts.append('</ul>')

    parts.append('</div>')
    return "\n".join(parts)


def build_dashboard(
    snapshots_path: str = "data/snapshots.csv",
    recommendations_path: str = "data/recommendations.csv",
    out_path: str = "data/dashboard.html",
    news_path: str = "data/news_latest.json",
) -> dict[str, Any]:
    """Render three Plotly charts plus a Latest-news headlines block into one HTML file."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    snap_path = Path(snapshots_path)
    rec_path = Path(recommendations_path)
    if not snap_path.exists() and not rec_path.exists():
        raise FileNotFoundError(
            f"neither {snap_path} nor {rec_path} exists; run snapshot/recommend first"
        )

    fig = make_subplots(
        rows=3, cols=1,
        subplot_titles=(
            "Portfolio value over time",
            "Recommended weights drift over time",
            "Latest recommended weights",
        ),
        vertical_spacing=0.10,
    )

    # 1. Portfolio total value over time (from snapshots.csv).
    if snap_path.exists():
        snaps = pd.read_csv(snap_path, parse_dates=["date"])
        totals = snaps.groupby("date")["total_value"].first().sort_index()
        fig.add_trace(
            go.Scatter(x=totals.index, y=totals.values, mode="lines+markers",
                       name="Total $", line={"width": 2}),
            row=1, col=1,
        )

    # 2. Weight drift over time (one line per ticker, from recommendations.csv).
    latest_weights: pd.DataFrame | None = None
    if rec_path.exists():
        recs = pd.read_csv(rec_path, parse_dates=["date"])
        for ticker, sub in recs.groupby("ticker"):
            sub = sub.sort_values("date")
            fig.add_trace(
                go.Scatter(x=sub["date"], y=sub["weight"], mode="lines+markers",
                           name=ticker, legendgroup="drift",
                           legendgrouptitle_text="Weight drift"),
                row=2, col=1,
            )
        latest_date = recs["date"].max()
        latest_weights = recs[recs["date"] == latest_date].sort_values("weight", ascending=False)

    # 3. Latest recommended weights (bar chart).
    if latest_weights is not None and not latest_weights.empty:
        fig.add_trace(
            go.Bar(x=latest_weights["ticker"], y=latest_weights["weight"],
                   name=f"As of {latest_weights['date'].iloc[0].date()}",
                   showlegend=False),
            row=3, col=1,
        )

    fig.update_layout(
        height=900,
        title_text="Portfolio Wave Rider — dashboard",
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="$", row=1, col=1)
    fig.update_yaxes(title_text="weight", row=2, col=1)
    fig.update_yaxes(title_text="weight", row=3, col=1)

    o_path = Path(out_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(o_path), include_plotlyjs="cdn")

    # Append the Latest-news headlines block after Plotly's HTML, if present.
    news_html = _render_news_html(Path(news_path))
    news_included = bool(news_html)
    if news_included:
        with o_path.open("a", encoding="utf-8") as f:
            f.write("\n" + news_html + "\n")

    return {
        "out_path": str(o_path),
        "snapshots_rows": int(len(pd.read_csv(snap_path))) if snap_path.exists() else 0,
        "recommendations_rows": int(len(pd.read_csv(rec_path))) if rec_path.exists() else 0,
        "news_included": news_included,
    }
