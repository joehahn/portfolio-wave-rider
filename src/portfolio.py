"""All portfolio math in one file.

Six public functions plus one orchestrator:

- ``fetch_prices`` — download adjusted-close prices from yfinance
- ``compute_returns`` — log-returns + annualized mean + covariance matrix
- ``optimize_portfolio`` — mean-variance optimization via scipy
- ``risk_metrics`` — Sharpe, vol, max drawdown, VaR, CVaR for a weight vector
- ``analyze`` — one-shot: fetch + returns + optimize + risk in one call
- ``snapshot_holdings`` — append daily $ values to data/snapshots.csv
- ``recommend_portfolio`` — append weekly weights to data/recommendations.csv
- ``append_wave_history`` — append per-wave stage classifications to data/wave_history.csv
- ``build_dashboard`` — render a static HTML dashboard from the CSVs plus the latest news payload

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
    risk_aversion: float = 1.0,
) -> dict[str, Any]:
    """Solve the mean-variance problem and return weights + summary stats.

    Objectives:
      - ``max_sharpe`` (default): maximize (μᵀw - r_free) / √(wᵀΣw).
        Picks the tangent portfolio on the efficient frontier.
      - ``min_variance``: minimize wᵀΣw. Lowest-vol point on the frontier.
      - ``mean_variance``: maximize μᵀw - λ·wᵀΣw. Slides along the frontier
        as ``risk_aversion`` (λ) changes; small λ favors return, large λ
        favors variance reduction.
      - ``target_return``: minimize wᵀΣw subject to μᵀw = target_return.
    """
    if objective not in {"max_sharpe", "min_variance", "target_return", "mean_variance"}:
        raise ValueError(f"unknown objective: {objective}")
    if objective == "target_return" and target_return is None:
        raise ValueError("target_return is required when objective='target_return'")
    if objective == "mean_variance" and risk_aversion <= 0:
        raise ValueError("risk_aversion (lambda) must be > 0 for mean_variance objective")

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
    elif objective == "mean_variance":
        # Maximize μᵀw - λ·wᵀΣw, equivalently minimize -μᵀw + λ·wᵀΣw.
        result = minimize(lambda w: -(w @ mu) + risk_aversion * (w @ sigma @ w),
                          w0, method="SLSQP", bounds=bounds, constraints=constraints)
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
    risk_aversion: float = 1.0,
) -> dict[str, Any]:
    """Run the full pipeline and return a single JSON-serializable dict."""
    prices = fetch_prices(tickers, period=period)
    returns = compute_returns(prices)
    opt = optimize_portfolio(
        returns, objective=objective, risk_free_rate=risk_free_rate,
        max_weight=max_weight, wave_views=wave_views,
        risk_aversion=risk_aversion,
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
# Walk-forward backtest. Replays the lightweight Python-only weekly path
# (the cron `recommend` cadence) over a historical window so we can spot-
# check whether the optimizer's recommendations are stable and whether
# rebalancing to them would have produced a reasonable realized return.
# No news, no wave tilts, no LLM cost. Output files go into a separate
# data/backtest/ directory so they don't disturb the live time-series.
# ---------------------------------------------------------------------------

def backtest(
    holdings_path: str = "holdings.csv",
    start_date: str | None = None,
    end_date: str | None = None,
    initial_usd: float = 50000.0,
    out_dir: str = "data/backtest/",
    lookback_years: int = 3,
    max_weight: float = 0.25,
    objective: str = "max_sharpe",
    risk_free_rate: float = 0.04,
    benchmarks: list[str] | None = None,
) -> dict[str, Any]:
    """Walk-forward weekly-rebalance backtest of the lightweight Python-only path.

    For each Friday in [start_date, end_date], runs the optimizer with a
    `lookback_years`-long window ending that Friday and rebalances the
    portfolio to those weights. Daily snapshots in between record the
    drifting value. No transaction costs are modeled. No news, no wave
    tilts. The point is to verify that the math-only system produces
    stable, profitable recommendations on real historical data.

    Outputs (under ``out_dir``):
      - snapshots.csv (same schema as live data/snapshots.csv)
      - recommendations.csv (same schema as live data/recommendations.csv)
      - report.md (realized return, max drawdown, weight-stability metric)
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Date window (default: 12 months back to yesterday). Tickers younger
    # than the 3y optimizer lookback (e.g., NUKZ, listed Nov 2024) get a
    # thin μ estimate in early weeks; the lookback is the real constraint
    # on young-ticker statistics, not the backtest window length.
    end = pd.Timestamp(end_date) if end_date else pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    start = pd.Timestamp(start_date) if start_date else end - pd.Timedelta(days=365)
    if start >= end:
        raise ValueError(f"start_date ({start.date()}) must be before end_date ({end.date()})")

    # Tickers from the holdings file (we don't care about its current shares).
    h_path = Path(holdings_path)
    if not h_path.exists():
        raise FileNotFoundError(f"holdings file not found: {h_path}")
    tickers = pd.read_csv(h_path)["ticker"].str.upper().str.strip().tolist()

    # Feasibility: weights must sum to 1 with each <= max_weight, so max_weight * n >= 1.
    if max_weight * len(tickers) < 1.0 - 1e-9:
        raise ValueError(
            f"infeasible: max_weight ({max_weight}) * n_tickers ({len(tickers)}) "
            f"= {max_weight * len(tickers):.3f} < 1. Either lower the cap or "
            f"add more tickers."
        )

    # One bulk yfinance call covering the optimizer's longest lookback through end.
    fetch_start = start - pd.Timedelta(days=365 * lookback_years + 30)  # padding for weekends
    raw = yf.download(tickers, start=fetch_start, end=end + pd.Timedelta(days=1),
                      auto_adjust=True, progress=False, group_by="column")
    if raw.empty:
        raise RuntimeError(f"yfinance returned no data for {tickers} between {fetch_start.date()} and {end.date()}")
    full_prices = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
        else raw[["Close"]].rename(columns={"Close": tickers[0]})
    full_prices = full_prices.dropna(how="all").ffill().dropna()

    # Trading days inside the backtest window.
    daily_dates = full_prices.loc[start:end].index
    if len(daily_dates) < 5:
        raise RuntimeError(f"only {len(daily_dates)} trading days in [{start.date()}, {end.date()}]")

    # Iterate. Friday = rebalance; every trading day = snapshot.
    snap_rows: list[dict[str, Any]] = []
    rec_rows: list[dict[str, Any]] = []
    current_shares: dict[str, float] | None = None
    last_weights: dict[str, float] | None = None
    weight_l1_distances: list[float] = []

    for date in daily_dates:
        is_friday = date.weekday() == 4
        is_first_day = date == daily_dates[0]

        if is_friday or (is_first_day and current_shares is None):
            # Run optimizer with a `lookback_years`-long window ending today.
            lookback_start = date - pd.Timedelta(days=365 * lookback_years)
            slice_prices = full_prices.loc[lookback_start:date]
            if len(slice_prices) < 30:
                continue
            returns = compute_returns(slice_prices)
            opt = optimize_portfolio(
                returns, objective=objective, risk_free_rate=risk_free_rate,
                max_weight=max_weight,
            )
            if not opt.get("success"):
                continue
            weights = opt["weights"]

            # Track week-over-week weight stability (L1 distance between weight vectors).
            if last_weights is not None:
                l1 = sum(abs(weights[t] - last_weights.get(t, 0)) for t in weights)
                weight_l1_distances.append(l1)
            last_weights = weights

            # Compute current portfolio value, then rebalance to the new weights.
            if current_shares is None:
                portfolio_value = initial_usd
            else:
                portfolio_value = sum(
                    current_shares[t] * float(full_prices.loc[date, t]) for t in tickers
                )
            current_shares = {
                t: (weights[t] * portfolio_value) / float(full_prices.loc[date, t])
                for t in tickers
            }

            for t in tickers:
                rec_rows.append({
                    "date": str(date.date()),
                    "ticker": t,
                    "weight": weights[t],
                    "expected_return": opt["expected_annual_return"],
                    "annual_volatility": opt["annual_volatility"],
                    "sharpe_ratio": opt["sharpe_ratio"],
                    "objective": objective,
                })

        # Daily snapshot (always, once we have shares).
        if current_shares is not None:
            day_total = sum(
                current_shares[t] * float(full_prices.loc[date, t]) for t in tickers
            )
            for t in tickers:
                px = float(full_prices.loc[date, t])
                snap_rows.append({
                    "date": str(date.date()),
                    "ticker": t,
                    "shares": round(current_shares[t], 4),
                    "price": px,
                    "value": round(current_shares[t] * px, 2),
                    "total_value": round(day_total, 2),
                })

    if not snap_rows:
        raise RuntimeError("backtest produced no snapshots; the optimizer never converged")

    snap_df = pd.DataFrame(snap_rows)
    rec_df = pd.DataFrame(rec_rows)
    snap_df.to_csv(out / "snapshots.csv", index=False)
    rec_df.to_csv(out / "recommendations.csv", index=False)

    # Summary metrics for the report.
    totals = snap_df.groupby("date")["total_value"].first().sort_index()
    initial_v = float(totals.iloc[0])
    final_v = float(totals.iloc[-1])
    realized_return = (final_v / initial_v) - 1.0
    days = (pd.Timestamp(totals.index[-1]) - pd.Timestamp(totals.index[0])).days or 1
    annualized_return = (final_v / initial_v) ** (365.0 / days) - 1.0
    equity = totals.values
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_drawdown = float(drawdown.min())
    weight_stability = float(np.mean(weight_l1_distances)) if weight_l1_distances else 0.0
    n_rebalances = len(weight_l1_distances) + 1

    # Realized-return per ticker if the user had bought-and-held the start-date weights.
    start_weights = {r["ticker"]: r["weight"] for r in rec_rows[:len(tickers)]}
    end_prices = {t: float(full_prices.loc[totals.index[-1], t]) for t in tickers}
    start_prices_row = full_prices.loc[totals.index[0]]
    bnh_per_ticker = {
        t: ((end_prices[t] / float(start_prices_row[t])) - 1.0) * start_weights.get(t, 0.0)
        for t in tickers
    }
    bnh_total = sum(bnh_per_ticker.values())

    # Benchmark realized returns over the same window. Skip on yfinance failure.
    if benchmarks is None:
        benchmarks = ["SPY"]
    benchmark_returns: dict[str, float] = {}
    if benchmarks:
        b_curves = _fetch_benchmark_curves(
            benchmarks, totals.index[0], totals.index[-1], 1.0,
        )
        for b, curve in b_curves.items():
            benchmark_returns[b] = float(curve.iloc[-1] - 1.0)

    bench_lines = "".join(
        f"| {b} (over the same window) | {ret * 100:+.2f}% |\n"
        for b, ret in benchmark_returns.items()
    )
    bench_active_lines = "".join(
        f"| Active return vs {b} | {(realized_return - ret) * 100:+.2f}pp |\n"
        for b, ret in benchmark_returns.items()
    )
    report = (
        f"# Backtest report\n\n"
        f"**Window:** {totals.index[0]} to {totals.index[-1]} ({days} calendar days, "
        f"{len(totals)} trading days)\n"
        f"**Tickers:** {', '.join(tickers)}\n"
        f"**Benchmarks:** {', '.join(benchmarks) if benchmarks else 'none'}\n"
        f"**Optimizer:** `{objective}`, lookback {lookback_years}y, max_weight {max_weight:.2f}\n"
        f"**Rebalance cadence:** weekly (Friday close)\n"
        f"**Transaction costs:** none modeled\n\n"
        f"## Realized performance\n\n"
        f"| Metric | Value |\n|---|---|\n"
        f"| Starting value | ${initial_v:,.2f} |\n"
        f"| Ending value | ${final_v:,.2f} |\n"
        f"| Realized return | {realized_return * 100:+.2f}% |\n"
        f"| Annualized return | {annualized_return * 100:+.2f}% |\n"
        f"| Max drawdown | {max_drawdown * 100:.2f}% |\n"
        f"| Buy-and-hold return (start-date weights) | {bnh_total * 100:+.2f}% |\n"
        f"| Active return vs buy-and-hold | {(realized_return - bnh_total) * 100:+.2f}pp |\n"
        f"{bench_lines}"
        f"{bench_active_lines}\n"
        f"## Weight stability\n\n"
        f"**Rebalance count:** {n_rebalances}\n"
        f"**Mean week-over-week L1 distance between weight vectors:** "
        f"{weight_stability:.4f}\n"
        f"(Lower is more stable. 0 = same weights every week. 2 = full portfolio "
        f"flipped between two disjoint sets every week.)\n\n"
        f"## Caveats\n\n"
        f"- No transaction costs, taxes, or market-impact slippage.\n"
        f"- No news, no wave-stage tilts. This is the cron `recommend` "
        f"path's behavior, not `/review-portfolio`'s.\n"
        f"- Look-ahead-bias-free: each Friday's optimizer sees only prices "
        f"up to that Friday.\n"
        f"- The 3-year lookback is the same window the live system uses, so "
        f"this backtest reflects how the live system would have decided.\n"
    )
    (out / "report.md").write_text(report)

    return {
        "out_dir": str(out),
        "window": {"start": str(totals.index[0]), "end": str(totals.index[-1]), "days": int(days)},
        "n_rebalances": n_rebalances,
        "n_snapshots": len(snap_rows) // len(tickers),
        "initial_value": round(initial_v, 2),
        "final_value": round(final_v, 2),
        "realized_return": round(realized_return, 4),
        "annualized_return": round(annualized_return, 4),
        "max_drawdown": round(max_drawdown, 4),
        "weight_stability_l1": round(weight_stability, 4),
        "benchmark_returns": {b: round(r, 4) for b, r in benchmark_returns.items()},
    }


# ---------------------------------------------------------------------------
# Daily news feed. Cron-friendly, no LLM. Uses yfinance's per-ticker news
# (Yahoo Finance) to keep the dashboard's "Today's headlines" section fresh
# between manual /review-portfolio runs.
# ---------------------------------------------------------------------------

def fetch_news_feed(
    holdings_path: str = "holdings.csv",
    out_path: str = "data/news_feed.json",
    per_ticker_limit: int = 5,
    date: str | None = None,
) -> dict[str, Any]:
    """Pull recent Yahoo Finance headlines for each ticker in holdings.csv,
    write a JSON payload shaped like ``data/news_latest.json`` (so the
    dashboard's existing news-rendering code can consume it without
    branching).

    yfinance returns up to ~10 items per ticker; we keep the most recent
    ``per_ticker_limit`` of them. No wave-stage classification; this is
    raw "what happened today" surfacing, not the LLM's interpretation.
    """
    h_path = Path(holdings_path)
    if not h_path.exists():
        raise FileNotFoundError(f"holdings file not found: {h_path}")
    holdings = pd.read_csv(h_path)
    if "ticker" not in holdings.columns:
        raise ValueError(f"{h_path} must have a 'ticker' column")
    tickers = holdings["ticker"].str.upper().str.strip().tolist()

    feed_date = pd.Timestamp(date).date() if date else pd.Timestamp.today().date()

    per_ticker: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        items = yf.Ticker(ticker).news or []
        bullets = []
        for item in items[:per_ticker_limit]:
            content = item.get("content") or {}
            title = (content.get("title") or "").strip()
            summary = (content.get("summary") or "").strip()
            url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            url = (url_obj.get("url") if isinstance(url_obj, dict) else "") or ""
            provider = content.get("provider") or {}
            source = (provider.get("displayName") if isinstance(provider, dict) else "") or ""
            pub_iso = content.get("pubDate") or ""
            pub_date = pub_iso[:10] if pub_iso else ""
            if not title or not url:
                continue
            bullets.append({
                "headline": title,
                "summary": summary,
                "source": source,
                "url": url,
                "date": pub_date,
            })
        per_ticker[ticker] = {"bullets": bullets}

    payload = {
        "date": str(feed_date),
        "per_ticker": per_ticker,
    }

    o_path = Path(out_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    o_path.write_text(json.dumps(payload, indent=2))

    n_bullets = sum(len(v.get("bullets") or []) for v in per_ticker.values())
    return {
        "date": str(feed_date),
        "tickers": tickers,
        "n_bullets": n_bullets,
        "out_path": str(o_path),
    }


# ---------------------------------------------------------------------------
# Wave-stage history. Appended each /review-portfolio run so the dashboard
# can chart how each wave's stage classification evolves between rebalances.
# ---------------------------------------------------------------------------

# Stage rank used for the trajectory chart. Monotonic in cycle position so
# a rising line means the wave is heating up; a falling line after the peak
# row means digestion. neutral = 0 because general_markets tickers carry no
# wave thesis and shouldn't visually bias the chart.
WAVE_STAGE_RANK = {
    "neutral":   0,
    "buildup":   1,
    "surge":     2,
    "peak":      3,
    "digestion": 4,
}


def append_wave_history(
    wave_stages: dict[str, dict[str, Any]],
    date: str,
    out_path: str = "data/wave_history.csv",
    force: bool = False,
    seeded: bool = False,
) -> dict[str, Any]:
    """Append today's wave-stage classifications to wave_history.csv.

    Schema: date, wave, stage, evidence_tickers, rationale, seeded.
    `evidence_tickers` is semicolon-joined inside the cell so the file
    stays a flat 2D CSV. `seeded` is True for synthetic backfill rows
    (from `seed_wave_history`) and False for organic /review-portfolio
    output. Idempotent on (date, wave): if rows already exist for
    ``date``, the call is a no-op unless force=True (in which case
    existing rows for that date are dropped first).
    """
    if not wave_stages:
        return {"skipped": True, "reason": "wave_stages is empty"}
    if not date:
        raise ValueError("date is required")

    o_path = Path(out_path)
    existing = pd.read_csv(o_path) if o_path.exists() else None
    if existing is not None and (existing["date"] == str(date)).any():
        if not force:
            return {"skipped": True, "date": str(date),
                    "reason": "wave-history rows already exist for this date; pass force=True to overwrite"}
        existing = existing[existing["date"] != str(date)]

    rows = []
    for wave, info in wave_stages.items():
        rows.append({
            "date": str(date),
            "wave": wave,
            "stage": info.get("stage", "neutral"),
            "evidence_tickers": ";".join(info.get("evidence_tickers") or []),
            "rationale": (info.get("rationale") or "").replace("\n", " ").strip(),
            "seeded": bool(seeded),
        })

    new_rows = pd.DataFrame(rows)
    out = pd.concat([existing, new_rows], ignore_index=True) if existing is not None else new_rows
    o_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(o_path, index=False)

    return {
        "date": str(date),
        "waves": list(wave_stages.keys()),
        "n_rows_appended": len(new_rows),
        "out_path": str(o_path),
    }


# ---------------------------------------------------------------------------
# Seeded historical wave-stage classifications. Used once on a fresh repo
# to populate ~12 months of trajectory so the dashboard's chart 4 isn't
# empty. These are post-hoc judgments grounded in real news flow over
# 2025-2026 (AI revenue compounding mid-2025, humanoid surge late 2025,
# nuclear-energy run-up Q4 2025 then digestion in Q1 2026, etc.).
# Tagged seeded=True so they're distinguishable from organic /review-
# portfolio output. Rationales are keyed by (wave, stage) and reused
# across months in the same stage.
# ---------------------------------------------------------------------------

# Twelve end-of-month classifications, May 2025 through April 2026.
# Each entry is (date, {wave: stage}).
_SEEDED_MONTHLY_STAGES: list[tuple[str, dict[str, str]]] = [
    ("2025-05-31", {"AI": "buildup", "rockets_spacecraft": "buildup", "robotics": "buildup", "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2025-06-30", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "buildup", "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2025-07-31", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "buildup", "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2025-08-31", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "buildup", "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2025-09-30", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "buildup", "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2025-10-31", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "surge",   "general_markets": "neutral"}),
    ("2025-11-30", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "surge",   "general_markets": "neutral"}),
    ("2025-12-31", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "surge",   "general_markets": "neutral"}),
    ("2026-01-31", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "surge",   "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2026-02-28", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "surge",   "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2026-03-31", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "surge",   "nuclear_fusion": "buildup", "general_markets": "neutral"}),
    ("2026-04-30", {"AI": "surge",   "rockets_spacecraft": "buildup", "robotics": "surge",   "engineered_biology": "buildup", "quantum": "buildup", "nuclear_fusion": "buildup", "general_markets": "neutral"}),
]

# Rationale keyed by (wave, stage). Same text repeats across months in
# the same stage. Phrased as a brief post-hoc summary, not pretending
# to be a real-time classification.
_SEEDED_RATIONALES: dict[tuple[str, str], str] = {
    ("AI", "buildup"): "Pre-mid-2025: AI capex still primarily speculative; revenue compounding had not yet broadly shown up in hyperscaler results.",
    ("AI", "surge"): "Mid-2025 onward: GOOGL Cloud, MSFT Azure, NVDA datacenter all growing 30-70% YoY; enterprise AI revenue compounding, hyperscalers competing to buy capacity.",
    ("rockets_spacecraft", "buildup"): "RKLB Electron cadence growing throughout 2025 ($1.85B backlog by year-end); Neutron pre-launch the entire window. Real commercial revenue from a small base.",
    ("robotics", "buildup"): "Industrial automation steady through mid-2025; humanoid programs raising capital but pre-deployment.",
    ("robotics", "surge"): "Late-2025 onward: Tesla Optimus Fremont production announced for Q2 2026, Figure AI $700M raise, humanoid demos dominate CES 2026. Adoption beginning to catch up with hype.",
    ("engineered_biology", "buildup"): "Gene-editing therapies clearing clinical milestones (Casgevy commercial; Intellia Phase 3 in vivo CRISPR April 2026), but ARKG ~80% below 2021 peak. Wave under-owned and cheap relative to scientific trajectory.",
    ("quantum", "buildup"): "Hardware milestones (Willow, Majorana 1, IBM Chicago hub) but no commercial deployment at scale. QCR 2026 explicitly forecasts no commercial scale this year.",
    ("quantum", "surge"): "Q1 2026 inflection: QTUM hit $4B AUM with 5-star Morningstar; +77% trailing year; March 20 rebalance toward hardware specialists. Quantinuum IPO catalyst flagged.",
    ("nuclear_fusion", "buildup"): "Private investment growing, IEA 2030 first-plant target, regulatory frameworks being built. Pure-play fusion still pre-IPO.",
    ("nuclear_fusion", "surge"): "Q4 2025 nuclear-energy run-up: NUKZ ran from ~$42 to $75 on AI-data-center electricity demand narrative (Meta 6.6 GW PPAs, Microsoft Three Mile Island). Adjacent, not pure fusion.",
    ("general_markets", "neutral"): "AGG/BIL/IAU/VIG are macro instruments, no wave attachment. Always neutral by construction.",
}


def seed_wave_history(
    out_path: str = "data/wave_history.csv",
    force: bool = False,
) -> dict[str, Any]:
    """Backfill wave_history.csv with 12 months of post-hoc classifications.

    Writes one row per (date, wave) for the 12 end-of-month dates in
    `_SEEDED_MONTHLY_STAGES`, using rationales from `_SEEDED_RATIONALES`.
    All rows are tagged `seeded=True`. Run once on a fresh repo so chart
    4 (wave-stage trajectories) renders meaningfully before /review-
    portfolio has had time to accumulate organic history.

    Idempotent on date: if a date already exists in the CSV (organic or
    seeded), the call skips it unless force=True.
    """
    o_path = Path(out_path)
    existing = pd.read_csv(o_path) if o_path.exists() else None
    existing_dates = set(existing["date"].astype(str)) if existing is not None else set()

    rows = []
    for date, stages in _SEEDED_MONTHLY_STAGES:
        if date in existing_dates and not force:
            continue
        for wave, stage in stages.items():
            rows.append({
                "date": date,
                "wave": wave,
                "stage": stage,
                "evidence_tickers": "",
                "rationale": _SEEDED_RATIONALES.get((wave, stage), ""),
                "seeded": True,
            })

    if not rows:
        return {"skipped": True, "reason": "all seeded dates already present; pass force=True to overwrite"}

    new_rows = pd.DataFrame(rows)
    if force and existing is not None:
        existing = existing[~existing["date"].astype(str).isin(
            {d for d, _ in _SEEDED_MONTHLY_STAGES}
        )]
    out = pd.concat([existing, new_rows], ignore_index=True) if existing is not None else new_rows
    out = out.sort_values(["date", "wave"]).reset_index(drop=True)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(o_path, index=False)

    return {
        "n_rows_appended": len(new_rows),
        "n_dates": len({r["date"] for r in rows}),
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

# Asset-class labels for the dashboard's "Latest recommended weights" bar chart.
# Each ticker gets a small secondary label under its name so a reader can
# scan "what kind of thing am I looking at" at a glance. Unknown tickers
# default to "equity" since that's the most common case for retail watchlists.
TICKER_ASSET_CLASS: dict[str, str] = {
    # Bonds
    "AGG": "bond", "BND": "bond", "TLT": "bond", "IEF": "bond",
    "SHY": "bond", "MUB": "bond", "LQD": "bond", "HYG": "bond",
    # Cash / ultra-short Treasuries
    "BIL": "cash", "SGOV": "cash", "SPAXX": "cash", "VMFXX": "cash",
    # Precious metals
    "IAU": "gold", "GLD": "gold", "SLV": "silver",
    "PPLT": "platinum", "PALL": "palladium",
    # Cryptocurrencies (spot ETFs)
    "IBIT": "crypto", "FBTC": "crypto", "BITB": "crypto",
    "ETHA": "crypto", "FETH": "crypto",
    # Broad-market and themed equity ETFs (called out so they don't all
    # look identical to single-stock equity tickers in the dashboard).
    "VTI": "equity ETF", "VOO": "equity ETF", "SPY": "equity ETF",
    "QQQ": "equity ETF", "VXUS": "equity ETF",
    "BOTZ": "equity ETF", "ROBO": "equity ETF",
    "ARKG": "equity ETF", "ARKK": "equity ETF",
    "AIQ": "equity ETF",
    "QTUM": "equity ETF", "NUKZ": "equity ETF",
    "VIG": "equity ETF",
}

# Map raw asset-class labels to the broader buckets shown on the
# "$ by asset class" chart. Equity singles and equity ETFs collapse to
# "equities"; precious metals collapse to one bucket. Anything not in
# this map falls back to "equities" (the most common single-stock case).
ASSET_CLASS_BUCKET: dict[str, str] = {
    "equity": "equities",
    "equity ETF": "equities",
    "bond": "bonds",
    "cash": "cash",
    "gold": "precious metals",
    "silver": "precious metals",
    "platinum": "precious metals",
    "palladium": "precious metals",
    "crypto": "crypto",
}

# Wave-bucket mapping for the "$ by wave" chart. Slow-moving fact about
# what each ticker is fundamentally a play on. Anything not in this map
# falls back to "general_markets".
TICKER_WAVE: dict[str, str] = {
    # AI
    "GOOGL": "AI", "NVDA": "AI", "MSFT": "AI",
    "AIQ": "AI", "ARKK": "AI", "QQQ": "AI",
    # Robotics
    "BOTZ": "robotics", "ROBO": "robotics",
    # Rockets / spacecraft
    "RKLB": "rockets_spacecraft",
    # Engineered biology
    "ARKG": "engineered_biology",
    # Quantum computing
    "QTUM": "quantum",
    # Nuclear fusion (NUKZ is a fission-heavy nuclear-energy ETF used as a
    # proxy until pure-play fusion firms like Commonwealth Fusion Systems
    # or Helion go public).
    "NUKZ": "nuclear_fusion",
    # General markets (broad ETFs, bonds, cash, metals, crypto)
    "AGG": "general_markets", "BND": "general_markets", "TLT": "general_markets",
    "IEF": "general_markets", "SHY": "general_markets", "MUB": "general_markets",
    "LQD": "general_markets", "HYG": "general_markets",
    "BIL": "general_markets", "SGOV": "general_markets",
    "SPAXX": "general_markets", "VMFXX": "general_markets",
    "IAU": "general_markets", "GLD": "general_markets", "SLV": "general_markets",
    "PPLT": "general_markets", "PALL": "general_markets",
    "IBIT": "general_markets", "FBTC": "general_markets", "BITB": "general_markets",
    "ETHA": "general_markets", "FETH": "general_markets",
    "VTI": "general_markets", "VOO": "general_markets",
    "SPY": "general_markets", "VXUS": "general_markets",
    # Defensive / dividend-quality equity (broad market with a quality tilt;
    # not a wave bet, so general_markets bucket).
    "VIG": "general_markets", "DVY": "general_markets",
    "XLU": "general_markets", "XLP": "general_markets",
}

# Short display labels for chart 3 (Latest recommended portfolio %). Each
# equity ticker gets a wave annotation under its asset class so a reader
# can tell at a glance which wave thesis each stock or ETF belongs to.
# `general_markets` renders as "defensive" because that bucket on equities
# is a defensive / quality-tilted holding (e.g., VIG), not a thesis bet.
WAVE_DISPLAY_LABEL: dict[str, str] = {
    "AI": "AI",
    "robotics": "robotics",
    "rockets_spacecraft": "rockets",
    "engineered_biology": "biology",
    "quantum": "quantum",
    "nuclear_fusion": "fusion",
    "general_markets": "defensive",
}


def _render_news_section(payload: dict, title: str, intro: str) -> str:
    """Render one news section (title + intro + per-ticker click-to-expand bullets).

    Returns an empty string if payload has no per_ticker bullets. Used by
    `_render_news_html` for both the daily yfinance feed and the monthly
    /review-portfolio rich payload; the two sections share the same
    schema (per_ticker -> {bullets: [{headline, summary, source, url, date,
    optional wave_bucket}]}) so the same renderer fits both.
    """
    per_ticker = payload.get("per_ticker") or {}
    if not per_ticker:
        return ""

    run_date = payload.get("date") or "unknown date"

    def _wave_rank(ticker: str) -> tuple[int, str]:
        wave = per_ticker[ticker].get("wave_bucket", "general_markets")
        rank = _WAVE_DISPLAY_ORDER.index(wave) if wave in _WAVE_DISPLAY_ORDER else 99
        return (rank, ticker)

    ordered_tickers = sorted(per_ticker.keys(), key=_wave_rank)

    # Titles and intros are caller-controlled constants, not user input,
    # so they're rendered as-is. Only date and bullet fields are escaped.
    parts = [
        '<h2 style="border-bottom:1px solid #ddd;padding-bottom:0.3em;margin-top:1.5em;">'
        f'{title} '
        f'<span style="color:#888;font-weight:normal;font-size:0.7em;">'
        f'({_html.escape(str(run_date))})</span></h2>',
        f'<p style="color:#666;font-size:0.9em;">{intro}</p>',
    ]

    for ticker in ordered_tickers:
        info = per_ticker[ticker]
        bullets = info.get("bullets") or []
        if not bullets:
            continue
        wave = info.get("wave_bucket")
        if wave:
            ticker_html = (f'{_html.escape(ticker)} '
                           f'<small style="color:#999;font-weight:normal;">'
                           f'({_html.escape(wave)})</small>')
        else:
            ticker_html = _html.escape(ticker)
        parts.append(
            f'<h3 style="margin-top:1.5em;color:#222;">{ticker_html}</h3>'
        )
        for b in bullets:
            summary_text = str(b.get("summary", ""))
            # Headline: prefer the explicit field; fall back to a truncated summary.
            headline = str(b.get("headline") or "").strip()
            if not headline:
                trimmed = summary_text.strip().split(". ")[0]
                headline = (trimmed[:100] + "…") if len(trimmed) > 100 else trimmed
            url = _html.escape(str(b.get("url", "#")), quote=True)
            source = _html.escape(str(b.get("source", "")))
            date = _html.escape(str(b.get("date", "")))
            meta = " · ".join(x for x in (source, date) if x)
            parts.append(
                '<details style="margin:0.5em 0;padding:0.4em 0.6em;'
                'border-left:3px solid #e0e0e0;">'
                '<summary style="cursor:pointer;line-height:1.4;color:#222;">'
                f'<span style="font-weight:600;">{_html.escape(headline)}</span>'
                f' <small style="color:#999;font-weight:normal;">{meta}</small>'
                '</summary>'
                '<div style="margin:0.6em 0 0.4em;line-height:1.55;color:#333;">'
                f'<p style="margin:0 0 0.5em;">{_html.escape(summary_text)}</p>'
                f'<a href="{url}" target="_blank" rel="noopener" '
                'style="color:#1a73e8;text-decoration:none;font-size:0.9em;">'
                'Read full article →</a>'
                '</div></details>'
            )

    return "\n".join(parts)


def _render_news_html(news_path: Path, news_feed_path: Path | None = None) -> str:
    """Render the dashboard's news area as up to two sections.

    Top section ("Today's headlines") comes from ``news_feed_path``
    (the daily yfinance scrape) when present. Bottom section
    ("In-depth news from last /review-portfolio") comes from
    ``news_path`` (the monthly LLM-driven payload) when present.

    Returns "" if neither file exists / has content.
    """
    sections: list[str] = []

    if news_feed_path is not None and news_feed_path.exists():
        try:
            feed = json.loads(news_feed_path.read_text())
            section = _render_news_section(
                feed,
                title="Today's headlines",
                intro="Refreshed daily by cron via Yahoo Finance "
                      "(<code>yfinance.Ticker(t).news</code>). Each entry's "
                      "summary is the article's lead paragraph from the source. "
                      "Surface-level coverage; for portfolio-relevant analysis "
                      "see the section below.",
            )
            if section:
                sections.append(section)
        except (json.JSONDecodeError, OSError):
            pass

    if news_path.exists():
        try:
            latest = json.loads(news_path.read_text())
            section = _render_news_section(
                latest,
                title="In-depth news from last /review-portfolio",
                intro="Click any headline to expand the LLM-written, "
                      "portfolio-relevance summary plus a link to the source. "
                      "Refreshed when you run <code>/review-portfolio</code> "
                      "(typically monthly). Tickers are grouped by wave bucket.",
            )
            if section:
                sections.append(section)
        except (json.JSONDecodeError, OSError):
            pass

    if not sections:
        return ""

    return "\n".join([
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:980px;margin:1.5em auto 3em;padding:0 1.5em;color:#222;">',
        *sections,
        '</div>',
    ])


def _fetch_benchmark_curves(
    benchmarks: list[str],
    start: pd.Timestamp | str,
    end: pd.Timestamp | str,
    starting_value: float,
) -> dict[str, pd.Series]:
    """Fetch benchmark prices via yfinance and rescale each to start at
    ``starting_value`` so the curve is comparable to a portfolio that
    began at the same dollar level on ``start``.

    Returns ``{benchmark_ticker: pd.Series indexed by date}``. Tickers
    that fail to download are silently skipped so a benchmark outage
    doesn't break the dashboard. ``start`` and ``end`` may be Timestamps
    or any string yfinance accepts (e.g. ``"2025-11-04"``).
    """
    if not benchmarks:
        return {}
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    try:
        raw = yf.download(benchmarks, start=start_ts, end=end_ts + pd.Timedelta(days=1),
                          auto_adjust=True, progress=False, group_by="column")
    except Exception:  # noqa: BLE001 - yfinance can raise many errors; be permissive.
        return {}
    if raw.empty:
        return {}
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
        else raw[["Close"]].rename(columns={"Close": benchmarks[0]})
    closes = closes.dropna(how="all").ffill().dropna(how="all")

    curves: dict[str, pd.Series] = {}
    for b in benchmarks:
        if b not in closes.columns:
            continue
        series = closes[b].dropna()
        if series.empty:
            continue
        first = float(series.iloc[0])
        if first <= 0:
            continue
        curves[b] = (series / first) * starting_value
    return curves


def build_dashboard(
    snapshots_path: str = "data/snapshots.csv",
    recommendations_path: str = "data/recommendations.csv",
    out_path: str = "data/dashboard.html",
    news_path: str = "data/news_latest.json",
    news_feed_path: str = "data/news_feed.json",
    wave_history_path: str = "data/wave_history.csv",
    benchmarks: list[str] | None = None,
) -> dict[str, Any]:
    """Render four Plotly charts plus the news area (daily yfinance feed
    + the most recent /review-portfolio payload) into one HTML file.

    If ``benchmarks`` is provided (or defaulted to ``["SPY"]``), each
    benchmark ticker's price curve is fetched via yfinance for the
    snapshot date range and overlaid on the portfolio-value chart,
    normalized so that benchmark and portfolio share a starting value.
    Pass an empty list to suppress benchmark overlays."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    snap_path = Path(snapshots_path)
    rec_path = Path(recommendations_path)
    wh_path = Path(wave_history_path)
    if not snap_path.exists() and not rec_path.exists():
        raise FileNotFoundError(
            f"neither {snap_path} nor {rec_path} exists; run snapshot/recommend first"
        )

    fig = make_subplots(
        rows=6, cols=1,
        subplot_titles=(
            "Portfolio value over time",
            "Recommended portfolio % drift over time",
            "Latest recommended portfolio %",
            "Wave-stage trajectories (0=neutral, 1=buildup, 2=surge, 3=peak, 4=digestion)",
            "$ by asset class over time",
            "$ by wave over time",
        ),
        vertical_spacing=0.06,
    )

    # 1. Portfolio total value over time (from snapshots.csv).
    benchmark_curves: dict[str, pd.Series] = {}
    if benchmarks is None:
        benchmarks = ["SPY"]
    if snap_path.exists():
        snaps = pd.read_csv(snap_path, parse_dates=["date"])
        totals = snaps.groupby("date")["total_value"].first().sort_index()
        fig.add_trace(
            go.Scatter(x=totals.index, y=totals.values, mode="lines+markers",
                       name="Portfolio $", line={"width": 2}),
            row=1, col=1,
        )
        # Benchmark overlays normalized to the portfolio's starting value.
        if benchmarks and len(totals) > 1:
            benchmark_curves = _fetch_benchmark_curves(
                benchmarks, totals.index[0], totals.index[-1], float(totals.iloc[0]),
            )
            for b, curve in benchmark_curves.items():
                fig.add_trace(
                    go.Scatter(x=curve.index, y=curve.values, mode="lines",
                               name=f"{b} (rebased)", line={"width": 1, "dash": "dash"}),
                    row=1, col=1,
                )

    # 2. Weight drift over time (one line per ticker, from recommendations.csv).
    # Legend labels include the asset class so a reader can scan which
    # bucket each line belongs to without consulting the holdings file.
    latest_weights: pd.DataFrame | None = None
    if rec_path.exists():
        recs = pd.read_csv(rec_path, parse_dates=["date"])
        for ticker, sub in recs.groupby("ticker"):
            sub = sub.sort_values("date")
            cls = TICKER_ASSET_CLASS.get(ticker, "equity")
            fig.add_trace(
                go.Scatter(x=sub["date"], y=sub["weight"], mode="lines+markers",
                           name=f"{ticker} ({cls})", legendgroup="drift",
                           legendgrouptitle_text="Portfolio % drift"),
                row=2, col=1,
            )
        latest_date = recs["date"].max()
        latest_weights = recs[recs["date"] == latest_date].sort_values("weight", ascending=False)

    # 3. Latest recommended weights (bar chart). The x-axis tick text
    # shows ticker plus a small asset-class label so a reader can scan
    # "what kind of thing is this" without consulting the holdings file.
    # Equities also get a wave annotation (AI, robotics, etc.) so the
    # reader can tell which wave thesis each stock or ETF belongs to;
    # non-equity tickers (bonds, cash, gold) don't need it because their
    # asset class already says everything.
    if latest_weights is not None and not latest_weights.empty:
        tickers_in_chart = latest_weights["ticker"].tolist()
        def _label_chart3(t: str) -> str:
            cls = TICKER_ASSET_CLASS.get(t, "equity")
            if cls == "equity":
                # Individual stocks: "TICKER / wave"
                wave = WAVE_DISPLAY_LABEL.get(TICKER_WAVE.get(t, "general_markets"), "")
                return f"{t}<br><sub>{wave}</sub>"
            if cls == "equity ETF":
                # ETFs: "TICKER / wave ETF" (e.g., "BOTZ / robotics ETF")
                wave = WAVE_DISPLAY_LABEL.get(TICKER_WAVE.get(t, "general_markets"), "")
                return f"{t}<br><sub>{wave} ETF</sub>"
            # Non-equities (bond, cash, gold, ...): "TICKER / asset class"
            return f"{t}<br><sub>{cls}</sub>"
        ticktext_3 = [_label_chart3(t) for t in tickers_in_chart]
        fig.add_trace(
            go.Bar(x=tickers_in_chart, y=latest_weights["weight"],
                   name=f"As of {latest_weights['date'].iloc[0].date()}",
                   showlegend=False),
            row=3, col=1,
        )
        fig.update_xaxes(
            tickmode="array",
            tickvals=tickers_in_chart,
            ticktext=ticktext_3,
            tickangle=0,
            row=3, col=1,
        )

    # 4. Wave-stage trajectories (one line per wave, from wave_history.csv).
    if wh_path.exists():
        wh = pd.read_csv(wh_path, parse_dates=["date"])
        wh["stage_rank"] = wh["stage"].map(WAVE_STAGE_RANK).fillna(0).astype(int)
        # Order legend by display priority so AI is at the top, general_markets last.
        ordered = sorted(
            wh["wave"].unique(),
            key=lambda w: _WAVE_DISPLAY_ORDER.index(w) if w in _WAVE_DISPLAY_ORDER else 99,
        )
        for wave in ordered:
            sub = wh[wh["wave"] == wave].sort_values("date")
            # Hover shows the actual stage label, not just the rank.
            hover = [f"{wave}<br>stage: {s}<br>tickers: {t}"
                     for s, t in zip(sub["stage"], sub["evidence_tickers"].fillna(""))]
            fig.add_trace(
                go.Scatter(x=sub["date"], y=sub["stage_rank"], mode="lines+markers",
                           name=wave, legendgroup="waves",
                           legendgrouptitle_text="Wave stages",
                           hovertext=hover, hoverinfo="text+x"),
                row=4, col=1,
            )

    # 5. $ by asset class over time and 6. $ by wave over time. Both
    # roll up the per-ticker per-day $ values from snapshots.csv. Each
    # ticker contributes to exactly one bucket in each chart, so the sum
    # of all lines in either chart equals the portfolio total.
    if snap_path.exists():
        snaps_full = pd.read_csv(snap_path, parse_dates=["date"])
        snaps_full["asset_bucket"] = snaps_full["ticker"].map(
            lambda t: ASSET_CLASS_BUCKET.get(TICKER_ASSET_CLASS.get(t, "equity"), "equities")
        )
        snaps_full["wave_bucket"] = snaps_full["ticker"].map(
            lambda t: TICKER_WAVE.get(t, "general_markets")
        )

        # Asset-class chart (row 5). Sum $ per (date, bucket).
        ac = snaps_full.groupby(["date", "asset_bucket"])["value"].sum().unstack(fill_value=0)
        # Stable, intuitive ordering.
        ac_order = [c for c in ["equities", "bonds", "cash", "precious metals", "crypto"]
                    if c in ac.columns]
        for bucket in ac_order:
            fig.add_trace(
                go.Scatter(x=ac.index, y=ac[bucket], mode="lines",
                           name=bucket, legend="legend2"),
                row=5, col=1,
            )

        # Wave chart (row 6). Same shape, different grouping.
        wv = snaps_full.groupby(["date", "wave_bucket"])["value"].sum().unstack(fill_value=0)
        # Use the same display order as elsewhere in the dashboard.
        wv_order = [w for w in _WAVE_DISPLAY_ORDER if w in wv.columns]
        for wave in wv_order:
            fig.add_trace(
                go.Scatter(x=wv.index, y=wv[wave], mode="lines",
                           name=wave, legend="legend3"),
                row=6, col=1,
            )

    fig.update_layout(
        height=1700,
        title_text="Portfolio Wave Rider — dashboard",
        # `closest` shows one trace's popup at a time, so hovering chart 1
        # shows portfolio $ OR SPY but not both (and chart 2 shows one
        # ticker's portfolio % at a time, which is cleaner with 7+ lines).
        hovermode="closest",
        # Per-subplot legends for charts 5 and 6, pinned to the right of
        # each subplot in paper coordinates. The default global legend
        # handles charts 1-4. Subplot row tops with 6 rows and
        # vertical_spacing=0.06: row 5 top ~0.293, row 6 top ~0.117.
        legend2=dict(
            title_text="Asset class $",
            xref="paper", x=1.02,
            yref="paper", y=0.293, yanchor="top",
        ),
        legend3=dict(
            title_text="Wave $",
            xref="paper", x=1.02,
            yref="paper", y=0.117, yanchor="top",
        ),
    )
    fig.update_yaxes(title_text="$", row=1, col=1)
    fig.update_yaxes(title_text="portfolio %", row=2, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="portfolio %", row=3, col=1, tickformat=".0%")
    # Chart 4: y-axis ticks show stage names alongside the numeric rank
    # so a reader can read the trajectory directly without remembering
    # 0=neutral, 1=buildup, 2=surge, etc.
    rank_to_stage = {v: k for k, v in WAVE_STAGE_RANK.items()}
    stage_ticktext = [f"{r} {rank_to_stage.get(r, '')}" for r in range(5)]
    fig.update_yaxes(title_text="stage", row=4, col=1,
                     range=[-0.3, 4.3],
                     tickmode="array",
                     tickvals=list(range(5)),
                     ticktext=stage_ticktext)
    fig.update_yaxes(title_text="$", row=5, col=1)
    # Log scale on chart 6 so small wave allocations (e.g., zero-weighted
    # robotics/biology lines hovering near a few hundred dollars) don't
    # collapse to the floor next to the dominant general_markets line.
    fig.update_yaxes(title_text="$ (log)", row=6, col=1, type="log")

    o_path = Path(out_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(o_path), include_plotlyjs="cdn")

    # Append the news area (daily feed + latest /review-portfolio payload)
    # after Plotly's HTML, if either file is present.
    nf_path = Path(news_feed_path)
    news_html = _render_news_html(Path(news_path), news_feed_path=nf_path)
    news_included = bool(news_html)
    if news_included:
        with o_path.open("a", encoding="utf-8") as f:
            f.write("\n" + news_html + "\n")

    return {
        "out_path": str(o_path),
        "snapshots_rows": int(len(pd.read_csv(snap_path))) if snap_path.exists() else 0,
        "recommendations_rows": int(len(pd.read_csv(rec_path))) if rec_path.exists() else 0,
        "wave_history_rows": int(len(pd.read_csv(wh_path))) if wh_path.exists() else 0,
        "benchmarks_overlaid": list(benchmark_curves.keys()),
        "news_feed_included": nf_path.exists(),
        "news_included": news_included,
    }
