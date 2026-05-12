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


def apply_wave_tilt(
    mu: pd.Series,
    wave_views: dict[str, str],
    tilt_schedule: dict[str, float] | None = None,
) -> pd.Series:
    """Multiply annualized mean returns by each ticker's stage tilt.

    ``tilt_schedule`` maps each stage to its multiplier. Defaults to
    ``WAVE_STAGE_TILT``; pass a different dict (e.g., from the profile's
    `financial_model.wave_stage_tilts` field) to override.
    """
    schedule = tilt_schedule or WAVE_STAGE_TILT
    tilted = mu.copy()
    for ticker, stage in wave_views.items():
        if ticker in tilted.index:
            tilted[ticker] = tilted[ticker] * schedule.get(stage, 1.0)
    return tilted


# ---------------------------------------------------------------------------
# Profile loader. Reads the YAML front matter of investor_profile.md and
# returns the financial_model section. Missing fields fall through to
# hard-coded defaults so old profiles (without the section) still work.
# ---------------------------------------------------------------------------

_FINANCIAL_MODEL_DEFAULTS: dict[str, Any] = {
    "objective": "max_sharpe",
    "risk_aversion": 1.0,
    "risk_free_rate": 0.04,
    "lookback_period": "3y",
    "wave_stage_tilts": dict(WAVE_STAGE_TILT),
}


def load_financial_model(profile_path: str = "investor_profile.md") -> dict[str, Any]:
    """Read `financial_model` from investor_profile.md's YAML front matter.

    Returns a dict with the five fields (`objective`, `risk_aversion`,
    `risk_free_rate`, `lookback_period`, `wave_stage_tilts`); any missing
    field falls back to the hard-coded default. If the profile file
    doesn't exist or has no front matter, all defaults are returned.
    """
    import re
    import yaml

    p = Path(profile_path)
    if not p.exists():
        return dict(_FINANCIAL_MODEL_DEFAULTS)
    text = p.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return dict(_FINANCIAL_MODEL_DEFAULTS)
    data = yaml.safe_load(m.group(1)) or {}
    fm = data.get("financial_model") or {}
    out = dict(_FINANCIAL_MODEL_DEFAULTS)
    out.update(fm)
    # Merge wave_stage_tilts so a partial override only changes the named
    # stages and the rest fall back to defaults.
    if isinstance(fm.get("wave_stage_tilts"), dict):
        merged = dict(WAVE_STAGE_TILT)
        merged.update(fm["wave_stage_tilts"])
        out["wave_stage_tilts"] = merged
    return out


# ---------------------------------------------------------------------------
# Market data: fetch prices and turn them into a returns bundle.
# ---------------------------------------------------------------------------

def _period_to_start(period: str) -> pd.Timestamp | None:
    """Parse a period string like '1.3y' or '6mo' into a start Timestamp.

    Returns None for non-numeric periods like 'max' or 'ytd', which
    yfinance handles natively. Used to support fractional periods
    (e.g., '1.3y') that yfinance's period= argument rejects."""
    import re
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(d|mo|y)", period.strip())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)
    days = {"d": n, "mo": n * 30, "y": n * 365}[unit]
    return pd.Timestamp.today().normalize() - pd.Timedelta(days=days)


def fetch_prices(tickers: list[str], period: str = "3y", interval: str = "1d") -> pd.DataFrame:
    """Download adjusted-close prices for the given tickers via yfinance."""
    if not tickers:
        raise ValueError("tickers must be non-empty")
    clean = [t.upper().strip() for t in tickers]
    # yfinance's period= only accepts canonical strings (1y, 2y, 5y...).
    # For fractional periods like '1.3y' we convert to explicit start/end.
    start = _period_to_start(period)
    if start is not None:
        end = pd.Timestamp.today().normalize() + pd.Timedelta(days=1)
        data = yf.download(clean, start=start, end=end, interval=interval,
                           auto_adjust=True, progress=False, group_by="column")
    else:
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
    tilt_schedule: dict[str, float] | None = None,
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
    if objective == "mean_variance" and risk_aversion < 0:
        raise ValueError("risk_aversion (lambda) must be >= 0 for mean_variance objective")

    tickers = list(returns["mean"].index)
    mean_series = apply_wave_tilt(returns["mean"], wave_views, tilt_schedule) if wave_views else returns["mean"]
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
    tilt_schedule: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run the full pipeline and return a single JSON-serializable dict."""
    prices = fetch_prices(tickers, period=period)
    returns = compute_returns(prices)
    opt = optimize_portfolio(
        returns, objective=objective, risk_free_rate=risk_free_rate,
        max_weight=max_weight, wave_views=wave_views,
        risk_aversion=risk_aversion, tilt_schedule=tilt_schedule,
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
# Thesis setup. Convert a thesis-driven dollar allocation to shares and
# write the initial holdings.csv. Pure function: prices are passed in so
# the unit test stays offline.
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
    wave_history_path: str = "data/wave_history.csv",
    period: str = "3y",
    max_weight: float = 0.25,
    risk_free_rate: float = 0.04,
    objective: str = "max_sharpe",
    risk_aversion: float = 1.0,
    tilt_schedule: dict[str, float] | None = None,
    date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run an optimization with the most recent wave-stage tilts and
    append per-ticker weights to a CSV.

    The cron-friendly sibling of /review-portfolio: pure Python, no news
    pulls, no LLM. Tilts are applied from the most recent
    ``wave_history.csv`` row dated on or before today (same as-of-date
    lookup used by ``backtest`` and the sweep scripts), so the weekly
    cron's recommendation stays consistent with the wave thesis between
    monthly /review-portfolio runs without needing to re-classify the
    waves itself. Universe = the tickers listed in ``holdings_path``.

    Schema appended to ``out_path``:
        date, ticker, weight, expected_return, annual_volatility,
        sharpe_ratio, objective

    Idempotent on date (skip unless force=True).
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

    # Build wave_views from the most recent wave_history row at or before
    # rec_date. Mirrors the as-of-date lookup helpers in backtest() and
    # the sweep scripts.
    wave_views: dict[str, str] | None = None
    wh_path = Path(wave_history_path)
    if wh_path.exists():
        wh_df = pd.read_csv(wh_path, parse_dates=["date"])
        relevant = wh_df[wh_df["date"] <= pd.Timestamp(rec_date)]
        if not relevant.empty:
            latest_date = relevant["date"].max()
            latest = relevant[relevant["date"] == latest_date]
            wave_to_stage = dict(zip(latest["wave"], latest["stage"]))
            wave_views = {
                t: wave_to_stage.get(TICKER_WAVE.get(t, "general_markets"), "neutral")
                for t in tickers
            }

    result = analyze(tickers, period=period, objective=objective,
                     max_weight=max_weight, risk_free_rate=risk_free_rate,
                     wave_views=wave_views,
                     risk_aversion=risk_aversion, tilt_schedule=tilt_schedule)
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
        "wave_views_applied": wave_views,
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
    lookback_years: float = 1.3,
    max_weight: float = 0.25,
    objective: str = "max_sharpe",
    risk_aversion: float = 1.0,
    tilt_schedule: dict[str, float] | None = None,
    risk_free_rate: float = 0.04,
    benchmarks: list[str] | None = None,
    wave_history_path: str | None = None,
    publish_docs: bool = True,
) -> dict[str, Any]:
    """Walk-forward monthly-rebalance backtest of the lightweight Python-only path.

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
    # Default to a rolling 12-month window ending today. yfinance silently
    # clips to whatever trading day actually has data (so running before
    # today's market close just stops at yesterday's price).
    end = pd.Timestamp(end_date) if end_date else pd.Timestamp.today().normalize()
    start = pd.Timestamp(start_date) if start_date else end - pd.DateOffset(years=1)
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

    # Time-varying wave views, if a wave_history file is given. At each
    # rebalance we look up the most recent classification at or before
    # that date, then map each ticker to its wave's stage. Tickers whose
    # wave_bucket isn't classified yet (or whose wave is missing) get
    # `neutral` (no tilt).
    wh_df = None
    if wave_history_path is not None:
        wh_path_obj = Path(wave_history_path)
        if wh_path_obj.exists():
            wh_df = pd.read_csv(wh_path_obj, parse_dates=["date"])

    def _wave_views_at(date: pd.Timestamp) -> dict[str, str] | None:
        if wh_df is None:
            return None
        relevant = wh_df[wh_df["date"] <= date]
        if relevant.empty:
            return None
        latest_date = relevant["date"].max()
        latest = relevant[relevant["date"] == latest_date]
        wave_to_stage = dict(zip(latest["wave"], latest["stage"]))
        return {
            t: wave_to_stage.get(TICKER_WAVE.get(t, "general_markets"), "neutral")
            for t in tickers
        }

    # Iterate. Friday = rebalance; every trading day = snapshot.
    snap_rows: list[dict[str, Any]] = []
    rec_rows: list[dict[str, Any]] = []
    current_shares: dict[str, float] | None = None
    last_weights: dict[str, float] | None = None
    weight_l1_distances: list[float] = []
    last_rebalance_month: int | None = None

    for date in daily_dates:
        # Monthly rebalance cadence: fire on the first trading day of each
        # month. Matches the live system's /review-portfolio cadence and the
        # /review-portfolio-driven update of wave_history.csv (which has at
        # most one row per month, so weekly rebalances between month-ends
        # added no new wave information anyway).
        is_new_month = date.month != last_rebalance_month
        is_first_day = date == daily_dates[0]

        if is_new_month or (is_first_day and current_shares is None):
            # Run optimizer with a `lookback_years`-long window ending today.
            lookback_start = date - pd.Timedelta(days=365 * lookback_years)
            slice_prices = full_prices.loc[lookback_start:date]
            if len(slice_prices) < 30:
                continue
            returns = compute_returns(slice_prices)
            opt = optimize_portfolio(
                returns, objective=objective, risk_free_rate=risk_free_rate,
                max_weight=max_weight, risk_aversion=risk_aversion,
                wave_views=_wave_views_at(date), tilt_schedule=tilt_schedule,
            )
            if not opt.get("success"):
                continue
            weights = opt["weights"]

            # Track month-over-month weight stability (L1 distance between weight vectors).
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
            last_rebalance_month = date.month

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

    # No-tilts companion walk-forward: same monthly-rebalance loop but
    # with wave_views=None at every rebalance, so the AI's per-month
    # wave-stage classifications never enter μ. Lets the dashboard
    # render an "AI tilt isolation" curve: gap to the main curve is the
    # AI contribution; gap to buy-and-hold is the pure-math
    # re-optimization contribution.
    nt_totals: list[dict[str, Any]] = []
    nt_shares: dict[str, float] | None = None
    nt_last_month: int | None = None
    for date in daily_dates:
        is_new_month = date.month != nt_last_month
        is_first_day = date == daily_dates[0]
        if is_new_month or (is_first_day and nt_shares is None):
            lookback_start = date - pd.Timedelta(days=365 * lookback_years)
            slice_prices = full_prices.loc[lookback_start:date]
            if len(slice_prices) < 30:
                continue
            returns = compute_returns(slice_prices)
            nt_opt = optimize_portfolio(
                returns, objective=objective, risk_free_rate=risk_free_rate,
                max_weight=max_weight, risk_aversion=risk_aversion,
                wave_views=None, tilt_schedule=tilt_schedule,
            )
            if not nt_opt.get("success"):
                continue
            nt_w = nt_opt["weights"]
            nt_pv = (initial_usd if nt_shares is None
                     else sum(nt_shares[t] * float(full_prices.loc[date, t]) for t in tickers))
            nt_shares = {t: nt_w[t] * nt_pv / float(full_prices.loc[date, t]) for t in tickers}
            nt_last_month = date.month
        if nt_shares is not None:
            nt_totals.append({
                "date": str(date.date()),
                "total_value": round(
                    sum(nt_shares[t] * float(full_prices.loc[date, t]) for t in tickers), 2),
            })
    if nt_totals:
        pd.DataFrame(nt_totals).to_csv(out / "no_tilts_totals.csv", index=False)

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
        f"**Rebalance cadence:** monthly (first trading day of each month)\n"
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

    # Auto-render the backtest dashboard at the standard path under
    # ``out_dir`` plus a public copy at ``docs/backtest.html`` so the
    # GitHub Pages two-page architecture stays in sync without a
    # manual second invocation. Pass thesis_baseline_path=None so the
    # full yearlong window is preserved (the backtest predates any
    # thesis allocation by design).
    # The docs/ copy is the GitHub Pages-served version; tests and ad-hoc
    # callers that don't want to clobber the public dashboard can pass
    # publish_docs=False.
    targets = [(str(out / "dashboard.html"), None)]
    if publish_docs:
        targets.append(("docs/backtest.html", "backtest"))
    rendered: list[str] = []
    for path, nav in targets:
        try:
            build_dashboard(
                snapshots_path=str(out / "snapshots.csv"),
                recommendations_path=str(out / "recommendations.csv"),
                out_path=path,
                wave_history_path="data/wave_history.csv",
                benchmarks=benchmarks,
                nav_current=nav,
                thesis_baseline_path=None,
            )
            rendered.append(path)
        except Exception:  # noqa: BLE001 — rendering shouldn't fail the backtest
            continue

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
        "dashboards_rendered": rendered,
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

# Stable wave -> color mapping so the same wave shows in the same color
# across all dashboard charts that group by wave (chart 2 = % by wave,
# chart 5 = wave-stage trajectories, chart 6 = articles per wave,
# chart 8 = $ by wave). Reader can scan vertically and track a wave's
# behavior across all four charts by color alone.
WAVE_COLORS: dict[str, str] = {
    "AI":                 "#1f77b4",  # blue
    "rockets_spacecraft": "#ff7f0e",  # orange
    "robotics":           "#2ca02c",  # green
    "engineered_biology": "#d62728",  # red
    "quantum":            "#9467bd",  # purple
    "nuclear_fusion":     "#8c564b",  # brown
    "general_markets":    "#7f7f7f",  # gray
}

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
    "nuclear_fusion": "nuclear",
    "general_markets": "defensive",
}


def _render_news_section(payload: dict, title: str, intro: str) -> str:
    """Render one news section (title + intro + per-ticker click-to-expand bullets).

    Returns an empty string if payload has no per_ticker bullets. Used by
    ``render_news_page`` for the dashboard's wave-stage news section.
    Schema: per_ticker -> {bullets: [{headline, summary, source, url,
    date, optional wave_bucket}]}.
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


_NAV_LINKS = [
    ("live",       "Live dashboard",       "index.html"),
    ("backtest",   "12-month backtest",    "backtest.html"),
    ("lambda",     "Lambda sweep",         "lambda_comparison.html"),
    ("max_weight", "Concentration sweep",  "max_weight_comparison.html"),
    ("lookback",   "Lookback sweep",       "lookback_comparison.html"),
    ("news",       "News",                 "news.html"),
]


def _render_nav_strip(current: str | None) -> str:
    """Small navigation block prepended to each dashboard HTML so a
    visitor can jump between the live dashboard, backtest dashboard,
    and the parameter-sweep comparison pages. ``current`` highlights
    one of `_NAV_LINKS`; pass None to omit the strip entirely.
    """
    if not current:
        return ""
    items = []
    for key, label, href in _NAV_LINKS:
        if key == current:
            items.append(
                f'<span style="padding:0.4em 0.9em;background:#1f77b4;color:#fff;'
                f'border-radius:4px;font-weight:600;">{label}</span>'
            )
        else:
            items.append(
                f'<a href="{href}" style="padding:0.4em 0.9em;color:#1f77b4;'
                f'text-decoration:none;border:1px solid #1f77b4;border-radius:4px;">'
                f'{label}</a>'
            )
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:980px;margin:1em auto 0.5em;padding:0 1.5em;display:flex;'
        f'gap:0.6em;flex-wrap:wrap;align-items:center;">{"".join(items)}</div>\n'
    )


def build_dashboard(
    snapshots_path: str = "data/snapshots.csv",
    recommendations_path: str = "data/recommendations.csv",
    out_path: str = "docs/index.html",
    wave_history_path: str = "data/wave_history.csv",
    benchmarks: list[str] | None = None,
    nav_current: str | None = None,
    thesis_baseline_path: str | None = "data/thesis_baseline.json",
) -> dict[str, Any]:
    """Render the time-series + bar charts into one HTML file.

    News content is rendered separately by ``render_news_page`` into
    docs/news.html; the dashboard CLI calls both together so cron and
    /review-portfolio refresh both files in one go.

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

    # is_live: when the live dashboard renders, thesis_baseline_path is
    # set and chart 6 (gain since most recent /review-portfolio rebalance)
    # is included. The backtest dashboard passes thesis_baseline_path=None
    # and skips chart 6 — a backtest has no "most recent /review-portfolio"
    # anchor in the live sense, so the chart is meaningless there.
    is_live = thesis_baseline_path is not None and Path(thesis_baseline_path).exists()
    # Backtest dashboards get an AI-lift chart (= portfolio$ / no-tilts$)
    # inserted as row 2 when the no-tilts companion series exists on
    # disk. Live dashboards don't (live state hasn't been replayed
    # without AI tilts).
    has_ai_lift = (not is_live) and (snap_path.parent / "no_tilts_totals.csv").exists()

    # Row layout: charts shift down by 1 when AI-lift is inserted.
    R_PORTFOLIO       = 1
    R_AI_LIFT         = 2 if has_ai_lift else None
    _shift            = 1 if has_ai_lift else 0
    R_TURNOVER        = 2 + _shift
    R_REC_WAVE        = 3 + _shift
    R_LATEST_WEIGHTS  = 4 + _shift
    R_GAIN_INIT       = 5 + _shift
    R_GAIN_REVIEW     = (6 + _shift) if is_live else None
    R_WAVE_STAGE      = (7 + _shift) if is_live else (6 + _shift)
    R_ARTICLES        = R_WAVE_STAGE + 1
    R_ASSET_USD       = R_WAVE_STAGE + 2
    R_WAVE_USD        = R_WAVE_STAGE + 3
    n_rows            = R_WAVE_USD

    _chart5_anchor = "/initialize-portfolio executed" if is_live else "backtest start"
    _chart5_tail = (
        "Bars sum to total realized portfolio gain since the thesis was set. Green = winners, red = losers."
        if is_live else
        "Bars sum to total realized portfolio gain over the backtest window. Green = winners, red = losers."
    )

    # Build the title list in row order, numbering as we go.
    titles_list: list[str] = []
    _chart1_extra = (
        "<br>monthly rebalance (no AI tilt) re-runs the optimizer each month with all wave-stage multipliers set to 1.0, so the LLM's news-driven tilts never enter μ."
        if has_ai_lift else ""
    )
    titles_list.append(
        f"{R_PORTFOLIO}. Portfolio value over time"
        "<br><sub><i>Σ(actual shares × close price) per day."
        f"{_chart1_extra}</i></sub>"
    )
    if R_AI_LIFT is not None:
        titles_list.append(
            f"{R_AI_LIFT}. AI lift = portfolio $ / monthly rebalance (no AI tilt)"
            "<br><sub><i>Ratio of the with-tilt portfolio value to the no-AI-tilt counterpart at each business day. Above 1.0 means the LLM's wave-stage tilts added value; below 1.0 means they cost value; 1.0 means no contribution.</i></sub>"
        )
    titles_list.append(
        f"{R_TURNOVER}. Rebalance turnover (% of portfolio dollars that changed holdings)"
        "<br><sub><i>At each rebalance, ½·||Δw||₁ — half the L1 distance between consecutive weight vectors. Equals the fraction of portfolio value that moved between tickers."
        "<br>Step-function: each value holds until the next rebalance.</i></sub>"
    )
    titles_list.append(
        f"{R_REC_WAVE}. Recommended portfolio % segregated by wave, versus time"
        "<br><sub><i>Each optimizer run produces target weights per ticker; this chart sums them by wave bucket so each line is the wave's total target allocation.</i></sub>"
    )
    titles_list.append(
        f"{R_LATEST_WEIGHTS}. Latest recommended portfolio %"
        "<br><sub><i>The most recent optimizer's target weight per ticker. Bars at the cap signal the optimizer wanted more than the concentration constraint allowed.</i></sub>"
    )
    titles_list.append(
        f"{R_GAIN_INIT}. Cumulative $ gain per holding since {_chart5_anchor}"
        "<br><sub><i>Per-ticker P&L attribution from the day-zero snapshot through today: Σ(prior-day shares × price change)."
        f"<br>{_chart5_tail}</i></sub>"
    )
    if R_GAIN_REVIEW is not None:
        titles_list.append(
            f"{R_GAIN_REVIEW}. Cumulative $ gain per holding since the most recent /review-portfolio rebalance"
            "<br><sub><i>Per-ticker P&L attribution from the most recent /review-portfolio rebalance date through today."
            "<br>Shows how the current allocation has performed since the latest optimizer fire.</i></sub>"
        )
    titles_list.append(
        f"{R_WAVE_STAGE}. Wave-stage trajectories (0=neutral, 1=buildup, 2=surge, 3=peak, 4=digestion)"
        "<br><sub><i>How the news-researcher classified each wave's cycle stage. Forward-filled across the snapshot window so each business day shows the most-recent-at-or-before classification."
        "<br>Right axis shows the tilt multiplier applied to that wave's tickers' expected returns.</i></sub>"
    )
    titles_list.append(
        f"{R_ARTICLES}. Articles harvested per wave over time"
        "<br><sub><i>Bullet count per wave per /review-portfolio run, from the archived news payloads. Forward-filled across the window so the latest count holds until the next /review-portfolio refreshes the payload.</i></sub>"
    )
    titles_list.append(
        f"{R_ASSET_USD}. Actual portfolio $ by asset class over time"
        "<br><sub><i>Your real holdings (from holdings.csv × close prices), grouped by asset class. Sums to total portfolio value (chart 1). Log y-axis keeps small allocations visible.</i></sub>"
    )
    titles_list.append(
        f"{R_WAVE_USD}. Actual portfolio $ by wave over time"
        "<br><sub><i>Your real holdings (from holdings.csv × close prices), grouped by wave. This is what you own today — not the optimizer's recommendation. Compare to chart 4 (latest recommended %) to see how far the actual portfolio sits from the latest recommendation. Log y-axis.</i></sub>"
    )
    titles_all = tuple(titles_list)

    specs = [[{}] for _ in range(n_rows)]
    specs[R_WAVE_STAGE - 1] = [{"secondary_y": True}]

    fig = make_subplots(
        rows=n_rows, cols=1,
        subplot_titles=titles_all,
        vertical_spacing=0.06,
        specs=specs,
    )

    # Compute a shared x-axis range from the daily-cadence data
    # (snapshots.csv min/max) and pad each end by a fixed fraction so
    # data points don't sit flush against the axis edges. Applied to
    # every time-series subplot on both the live and backtest dashboards
    # so the charts align visually even when sparser data sources
    # (wave_history.csv updates on /review-portfolio cadence;
    # data/news/ accumulates one file per /review-portfolio) extend
    # beyond the snapshots range. No hardcoded dates: the range rolls
    # forward each business day as the cron appends new snapshots.
    xrange: tuple[pd.Timestamp, pd.Timestamp] | None = None
    latest_snap_date: pd.Timestamp | None = None
    if snap_path.exists():
        try:
            _snaps_dates = pd.read_csv(snap_path, parse_dates=["date"])["date"]
            if not _snaps_dates.empty:
                d_min, d_max = _snaps_dates.min(), _snaps_dates.max()
                span = d_max - d_min
                pad = max(pd.Timedelta(days=1), span * 0.03)
                xrange = (d_min - pad, d_max + pad)
                latest_snap_date = d_max
        except (OSError, pd.errors.EmptyDataError):
            xrange = None

    # 1. Portfolio total value over time (from snapshots.csv).
    benchmark_curves: dict[str, pd.Series] = {}
    if benchmarks is None:
        benchmarks = ["SPY"]
    # Live dashboard has only a few snapshots (since /initialize-portfolio),
    # so "lines+markers" shows each day as a visible dot. Backtest has
    # ~250 snapshots; markers would be cluttered, so it stays lines-only.
    _ts_mode = "lines+markers" if is_live else "lines"
    if snap_path.exists():
        snaps = pd.read_csv(snap_path, parse_dates=["date"])
        totals = snaps.groupby("date")["total_value"].first().sort_index()
        fig.add_trace(
            go.Scatter(x=totals.index, y=totals.values, mode=_ts_mode,
                       name="Portfolio $", line={"width": 2, "color": "#1f77b4"},
                       legend="legend"),
            row=1, col=1,
        )
        # Mark each rebalance date (where the optimizer fired) with a
        # large open-square symbol so the cadence is visually obvious.
        # Rebalance dates come from recommendations.csv; the markers sit
        # on top of the portfolio-value line at each fire date.
        if rec_path.exists():
            rec_dates = pd.read_csv(rec_path, parse_dates=["date"])["date"].unique()
            rebalance_totals = totals[totals.index.isin(rec_dates)]
            if not rebalance_totals.empty:
                fig.add_trace(
                    go.Scatter(x=rebalance_totals.index, y=rebalance_totals.values,
                               mode="markers", name="Rebalance",
                               marker={"size": 11, "symbol": "square-open",
                                       "color": "#ff7f0e", "line": {"width": 2}},
                               legend="legend"),
                    row=1, col=1,
                )
        # Benchmark overlays normalized to the portfolio's starting value.
        # SPY-style benchmarks are rendered in light green and dashed so
        # they're visually distinct from the portfolio (blue) and the
        # no-rebalance counterfactual (brown, dash-dot, below).
        if benchmarks and len(totals) > 1:
            benchmark_curves = _fetch_benchmark_curves(
                benchmarks, totals.index[0], totals.index[-1], float(totals.iloc[0]),
            )
            for b, curve in benchmark_curves.items():
                fig.add_trace(
                    go.Scatter(x=curve.index, y=curve.values, mode=_ts_mode,
                               name=f"{b} (rescaled)",
                               line={"width": 1.5, "color": "#66c266", "dash": "dash"},
                               legend="legend"),
                    row=1, col=1,
                )
        # No-rebalance counterfactual: hold the first-snapshot share
        # counts for the entire window. Backtest only — in live mode the
        # snapshots span the post-/initialize-portfolio period during
        # which the user has manually rebalanced, so a single buy-and-hold
        # comparison from day 1 is moot.
        if not is_live and len(snaps) > 0:
            first_date = snaps["date"].min()
            initial_shares = (snaps[snaps["date"] == first_date]
                              .set_index("ticker")["shares"])
            pivot = snaps.pivot_table(index="date", columns="ticker", values="price").sort_index()
            common = [t for t in initial_shares.index if t in pivot.columns]
            no_rebal = (pivot[common] * initial_shares[common]).sum(axis=1)
            fig.add_trace(
                go.Scatter(x=no_rebal.index, y=no_rebal.values, mode="lines",
                           name="buy-and-hold",
                           line={"width": 1.5, "color": "#8c564b", "dash": "dashdot"},
                           legend="legend"),
                row=1, col=1,
            )
            # AI-tilt isolation: monthly-rebalance walk-forward with
            # wave_views=None. Written by backtest() to no_tilts_totals.csv.
            # Gap between the main portfolio line and this curve is the
            # AI tilt contribution; gap between this curve and buy-and-hold
            # is the pure-math re-optimization contribution.
            nt_path = snap_path.parent / "no_tilts_totals.csv"
            if nt_path.exists():
                nt = pd.read_csv(nt_path, parse_dates=["date"]).set_index("date")["total_value"]
                fig.add_trace(
                    go.Scatter(x=nt.index, y=nt.values, mode="lines",
                               name="monthly rebalance (no AI tilt)",
                               line={"width": 1.5, "color": "#9467bd", "dash": "dot"},
                               legend="legend"),
                    row=R_PORTFOLIO, col=1,
                )
                # AI-lift ratio chart (row 2 in backtest mode): main
                # portfolio $ divided by no-tilt portfolio $, at each
                # business day. > 1.0 means the LLM's wave-stage tilts
                # added value; < 1.0 means they cost value.
                if R_AI_LIFT is not None:
                    common = totals.index.intersection(nt.index)
                    if len(common) > 0:
                        ratio = totals.loc[common] / nt.loc[common]
                        fig.add_trace(
                            go.Scatter(x=ratio.index, y=ratio.values, mode="lines",
                                       name="AI lift",
                                       line={"width": 2, "color": "#9467bd"},
                                       showlegend=False,
                                       hovertemplate="%{x|%Y-%m-%d}<br>%{y:.3f}×<extra></extra>"),
                            row=R_AI_LIFT, col=1,
                        )
                        fig.add_hline(y=1.0, line_dash="dot", line_width=1,
                                      line_color="#888", row=R_AI_LIFT, col=1)

    # 2. Recommended portfolio % segregated by wave, versus time.
    # Sum each wave's tickers' weights into one line per wave so the
    # chart reads as ~6 lines (one per wave bucket) instead of ~12-line
    # ticker spaghetti. Per-ticker latest weights still get extracted
    # below for chart 3.
    latest_weights: pd.DataFrame | None = None
    if rec_path.exists():
        recs = pd.read_csv(rec_path, parse_dates=["date"])
        recs["wave_bucket"] = recs["ticker"].map(
            lambda t: TICKER_WAVE.get(t, "general_markets")
        )
        wv_weight = recs.groupby(["date", "wave_bucket"])["weight"].sum().unstack(fill_value=0)
        # Extend the most recent rebalance horizontally to the right
        # edge of the x-axis window, so the chart shows that the latest
        # weights remain in effect until the next /review-portfolio.
        # Implemented as a step trace ("hv" line shape) with the last
        # row's values repeated at xrange[1].
        if latest_snap_date is not None and not wv_weight.empty and wv_weight.index.max() < latest_snap_date:
            wv_weight = pd.concat([wv_weight,
                                   wv_weight.iloc[[-1]].rename(index={wv_weight.index[-1]: latest_snap_date})])
        wv_order = [w for w in _WAVE_DISPLAY_ORDER if w in wv_weight.columns]
        # Add a tiny vertical offset per wave so traces with the same
        # value (often 0%) don't pile on top of each other and become
        # invisible. The offset is cosmetic; hover text reports the
        # true weight.
        wv_offset_step = 0.002  # 0.2 percentage points per trace
        for i, wave in enumerate(wv_order):
            offset = i * wv_offset_step
            true_pct = [v * 100 for v in wv_weight[wave]]
            fig.add_trace(
                go.Scatter(x=wv_weight.index, y=wv_weight[wave] + offset,
                           mode="lines+markers",
                           name=WAVE_DISPLAY_LABEL.get(wave, wave),
                           legend="legend5",
                           line={"color": WAVE_COLORS.get(wave), "shape": "hv"},
                           customdata=true_pct,
                           hovertemplate=f"{wave}<br>%{{x|%Y-%m-%d}}"
                                         "<br>%{customdata:.2f}%<extra></extra>"),
                row=R_REC_WAVE, col=1,
            )
        latest_date = recs["date"].max()
        latest_weights = recs[recs["date"] == latest_date].sort_values("weight", ascending=False)

    # Label helper used by chart 3 (Latest recommended portfolio %) and
    # chart 4 (Cumulative $ gain per holding). Equities get "TICKER /
    # wave"; equity ETFs get "TICKER / wave ETF"; non-equities get
    # "TICKER / asset class".
    def _ticker_label(t: str) -> str:
        cls = TICKER_ASSET_CLASS.get(t, "equity")
        if cls == "equity":
            wave = WAVE_DISPLAY_LABEL.get(TICKER_WAVE.get(t, "general_markets"), "")
            return f"{t}<br><sub>{wave}</sub>"
        if cls == "equity ETF":
            wave = WAVE_DISPLAY_LABEL.get(TICKER_WAVE.get(t, "general_markets"), "")
            return f"{t}<br><sub>{wave} ETF</sub>"
        return f"{t}<br><sub>{cls}</sub>"

    # 3. Latest recommended weights (bar chart). The x-axis tick text
    # shows ticker plus a small asset-class label so a reader can scan
    # "what kind of thing is this" without consulting the holdings file.
    # Equities also get a wave annotation (AI, robotics, etc.) so the
    # reader can tell which wave thesis each stock or ETF belongs to;
    # non-equity tickers (bonds, cash, gold) don't need it because their
    # asset class already says everything.
    if latest_weights is not None and not latest_weights.empty:
        tickers_in_chart = latest_weights["ticker"].tolist()
        ticktext_3 = [_ticker_label(t) for t in tickers_in_chart]
        fig.add_trace(
            go.Bar(x=tickers_in_chart, y=latest_weights["weight"],
                   name=f"As of {latest_weights['date'].iloc[0].date()}",
                   showlegend=False),
            row=R_LATEST_WEIGHTS, col=1,
        )
        # Horizontal dashed line at the concentration cap (read from
        # investor_profile.md top-level YAML). Plotted as a Scatter trace
        # so it shows up in the legend with a descriptive label.
        try:
            import yaml as _yaml
            import re as _re
            _profile_text = Path("investor_profile.md").read_text()
            _m = _re.match(r"^---\s*\n(.*?)\n---\s*\n", _profile_text, _re.DOTALL)
            _cap = float((_yaml.safe_load(_m.group(1)) or {}).get(
                "concentration_cap", 0.25)) if _m else 0.25
        except (OSError, ValueError, AttributeError):
            _cap = 0.25
        fig.add_trace(
            go.Scatter(x=tickers_in_chart, y=[_cap] * len(tickers_in_chart),
                       mode="lines", name=f"Concentration cap ({_cap*100:.0f}%)",
                       line={"color": "#d62728", "width": 1.5, "dash": "dot"},
                       hoverinfo="skip", showlegend=True, legend="legend7"),
            row=R_LATEST_WEIGHTS, col=1,
        )
        fig.update_xaxes(
            tickmode="array",
            tickvals=tickers_in_chart,
            ticktext=ticktext_3,
            tickangle=0,
            row=R_LATEST_WEIGHTS, col=1,
        )

    # 4. Cumulative $ gain per holding over the snapshot window. For each
    # ticker, daily P&L = shares_t * (price_t - price_{t-1}); cumulative
    # gain = sum across the window. This properly attributes gain when the
    # optimizer rebalances (shares change weekly), since each day's price
    # change is multiplied by that day's share count. Sums to the
    # portfolio's total realized gain (modulo numerical noise).
    if snap_path.exists():
        snaps_full = pd.read_csv(snap_path, parse_dates=["date"]).sort_values(["ticker", "date"])
        gain_by_ticker: dict[str, float] = {}
        for ticker, sub in snaps_full.groupby("ticker"):
            sub = sub.sort_values("date").reset_index(drop=True)
            price_change = sub["price"].diff()
            # On rebalance days the prior-day shares are what earned the price
            # change; using sub["shares"].shift(1) avoids attributing a price
            # move to the new (post-rebalance) share count. On the very first
            # day (NaN diff) contribution is zero, which is correct.
            prior_shares = sub["shares"].shift(1)
            daily_pnl = (prior_shares * price_change).fillna(0.0)
            gain_by_ticker[ticker] = float(daily_pnl.sum())
        # Sort tickers by gain descending. Use the same x-axis labels as
        # chart 3 so the reader can scan the two side by side.
        gain_items = sorted(gain_by_ticker.items(), key=lambda kv: kv[1], reverse=True)
        gain_tickers = [t for t, _ in gain_items]
        gain_values = [v for _, v in gain_items]
        ticktext_4 = [_ticker_label(t) for t in gain_tickers]
        # Color positive bars green, negative red so a glance reads
        # winners vs losers without consulting the y-axis number.
        bar_colors = ["#2ca02c" if v >= 0 else "#d62728" for v in gain_values]
        fig.add_trace(
            go.Bar(x=gain_tickers, y=gain_values,
                   marker_color=bar_colors,
                   name="Cumulative $ gain", showlegend=False),
            row=R_GAIN_INIT, col=1,
        )
        fig.update_xaxes(
            tickmode="array",
            tickvals=gain_tickers,
            ticktext=ticktext_4,
            tickangle=0,
            row=R_GAIN_INIT, col=1,
        )
        # Inject total gain into the chart's title via the annotation at
        # position (R_GAIN_INIT - 1) — subplot titles map 1:1 to the
        # figure's annotations in row order.
        _total_init = sum(gain_values)
        _chart5_prefix = f"{R_GAIN_INIT}. Cumulative $ gain per holding since {_chart5_anchor}"
        fig.layout.annotations[R_GAIN_INIT - 1].update(
            text=fig.layout.annotations[R_GAIN_INIT - 1].text.replace(
                _chart5_prefix,
                f"{_chart5_prefix} (total: ${_total_init:+,.0f})",
            )
        )

    # 6. Cumulative $ gain per holding since the most recent /review-portfolio
    # rebalance. Same daily-PnL math as chart 5, but the snapshot window is
    # restricted to dates >= the latest rebalance in recommendations.csv.
    # Shows how the current allocation has performed since the last
    # optimizer fire — answers "is the latest recommendation working?"
    # Backtest dashboard skips this chart since it has no "most recent
    # /review-portfolio" anchor.
    if is_live and snap_path.exists() and rec_path.exists():
        recs_for_recent = pd.read_csv(rec_path, parse_dates=["date"])
        if not recs_for_recent.empty:
            last_rebalance = recs_for_recent["date"].max()
            recent = snaps_full[snaps_full["date"] >= last_rebalance].copy()
            recent_gain_by_ticker: dict[str, float] = {}
            for ticker, sub in recent.groupby("ticker"):
                sub = sub.sort_values("date").reset_index(drop=True)
                price_change = sub["price"].diff()
                prior_shares = sub["shares"].shift(1)
                daily_pnl = (prior_shares * price_change).fillna(0.0)
                recent_gain_by_ticker[ticker] = float(daily_pnl.sum())
            # Preserve the same ticker order as chart 5 so the eye can
            # compare each ticker's since-init bar to its since-rebalance bar.
            recent_values = [recent_gain_by_ticker.get(t, 0.0) for t in gain_tickers]
            recent_colors = ["#2ca02c" if v >= 0 else "#d62728" for v in recent_values]
            fig.add_trace(
                go.Bar(x=gain_tickers, y=recent_values,
                       marker_color=recent_colors,
                       name="Since last rebalance", showlegend=False),
                row=R_GAIN_REVIEW, col=1,
            )
            fig.update_xaxes(
                tickmode="array",
                tickvals=gain_tickers,
                ticktext=ticktext_4,
                tickangle=0,
                row=R_GAIN_REVIEW, col=1,
            )
            # Inject total gain into the chart's title.
            _total_recent = sum(recent_values)
            _chart6_prefix = f"{R_GAIN_REVIEW}. Cumulative $ gain per holding since the most recent /review-portfolio rebalance"
            fig.layout.annotations[R_GAIN_REVIEW - 1].update(
                text=fig.layout.annotations[R_GAIN_REVIEW - 1].text.replace(
                    _chart6_prefix,
                    f"{_chart6_prefix} (total: ${_total_recent:+,.0f})",
                )
            )

    # 7. Wave-stage trajectories (one line per wave, from wave_history.csv).
    # Forward-filled across the snapshot window so each business day
    # carries the most-recent-at-or-before classification, even though
    # wave_history.csv itself only updates on /review-portfolio cadence.
    # Produces a step-function trace per wave that's visible across the
    # whole window instead of degenerating to single dots on the few
    # actual classification dates.
    if wh_path.exists() and xrange is not None:
        wh_full = pd.read_csv(wh_path, parse_dates=["date"])
        # Snapshot dates anchor the x-axis. Use unique trading days
        # in the snapshot window.
        if snap_path.exists():
            snap_dates = pd.read_csv(snap_path, parse_dates=["date"])["date"].drop_duplicates().sort_values()
            window_dates = snap_dates[(snap_dates >= xrange[0]) & (snap_dates <= xrange[1])]
        else:
            window_dates = pd.Series(dtype="datetime64[ns]")
        # Order legend by display priority so AI is at the top, general_markets last.
        ordered = sorted(
            wh_full["wave"].unique(),
            key=lambda w: _WAVE_DISPLAY_ORDER.index(w) if w in _WAVE_DISPLAY_ORDER else 99,
        )
        # Per-wave vertical offset so multiple waves at the same stage
        # rank don't render on top of each other. Cosmetic only; hover
        # shows the actual stage label.
        wh_offset_step = 0.05  # in stage-rank units (0..4)
        for i, wave in enumerate(ordered):
            wave_history = (wh_full[wh_full["wave"] == wave]
                            .sort_values("date")
                            .set_index("date"))
            if window_dates.empty or wave_history.empty:
                continue
            # For each business day in the window, look up the most
            # recent classification at or before that day.
            ff = wave_history.reindex(pd.DatetimeIndex(window_dates), method="ffill").dropna(subset=["stage"])
            if ff.empty:
                continue
            ff = ff.reset_index().rename(columns={"index": "date"})
            ff["stage_rank"] = ff["stage"].map(WAVE_STAGE_RANK).fillna(0).astype(int)
            sub = ff
            offset = i * wh_offset_step
            # Hover shows the actual stage label, not just the rank.
            wave_label = WAVE_DISPLAY_LABEL.get(wave, wave)
            hover = [f"{wave_label}<br>stage: {s}<br>tickers: {t}"
                     for s, t in zip(sub["stage"], sub["evidence_tickers"].fillna(""))]
            fig.add_trace(
                go.Scatter(x=sub["date"], y=sub["stage_rank"] + offset,
                           mode=_ts_mode,
                           name=wave_label, legend="legend6",
                           line={"color": WAVE_COLORS.get(wave)},
                           hovertext=hover, hoverinfo="text+x"),
                row=R_WAVE_STAGE, col=1,
            )

    # 6. Articles harvested per wave over time. Reads each archived
    # data/news/<date>-news.json file and counts bullets per wave_bucket.
    # This answers "is the wave-stage classification (chart 5 above)
    # backed by lots of evidence on each date, or thin coverage?"
    # Backtest dashboard reads the as-of-date pilot archive (the actual
    # inputs the simulated wave classifications drew from); live dashboard
    # reads the rolling /review-portfolio archive.
    news_dir = Path("data/news" if is_live else "data/news_asof")
    if news_dir.is_dir():
        article_rows: list[dict[str, Any]] = []
        for f in sorted(news_dir.glob("*-news.json")):
            try:
                payload = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            d = payload.get("date")
            if not d:
                continue
            counts: dict[str, int] = {}
            for ticker_info in (payload.get("per_ticker") or {}).values():
                wave = ticker_info.get("wave_bucket", "general_markets")
                # Normalize legacy synthetic_biology label so the chart
                # legend doesn't split into two near-identical lines.
                if wave == "synthetic_biology":
                    wave = "engineered_biology"
                counts[wave] = counts.get(wave, 0) + len(ticker_info.get("bullets") or [])
            for wave, n in counts.items():
                article_rows.append({"date": pd.Timestamp(d), "wave": wave, "count": n})
        if article_rows:
            adf = pd.DataFrame(article_rows)
            if xrange is not None:
                adf = adf[(adf["date"] >= xrange[0]) & (adf["date"] <= xrange[1])]
            for wave in [w for w in _WAVE_DISPLAY_ORDER if w in adf["wave"].unique()]:
                sub = adf[adf["wave"] == wave].sort_values("date")
                # Extend the most recent bullet count horizontally to
                # the right edge of the window so the chart spans the
                # full snapshot range. Markers stay only at real
                # /review-portfolio dates.
                xs = list(sub["date"])
                ys = list(sub["count"])
                if latest_snap_date is not None and xs and xs[-1] < latest_snap_date:
                    xs.append(latest_snap_date)
                    ys.append(ys[-1])
                fig.add_trace(
                    go.Scatter(x=xs, y=ys, mode="lines",
                               name=WAVE_DISPLAY_LABEL.get(wave, wave),
                               legend="legend4",
                               line={"color": WAVE_COLORS.get(wave)}),
                    row=R_ARTICLES, col=1,
                )
                fig.add_trace(
                    go.Scatter(x=sub["date"], y=sub["count"], mode="markers",
                               name=f"{WAVE_DISPLAY_LABEL.get(wave, wave)} mark", legend="legend4",
                               marker={"color": WAVE_COLORS.get(wave), "size": 7},
                               hoverinfo="skip", showlegend=False),
                    row=R_ARTICLES, col=1,
                )

    # 7. $ by asset class over time and 8. $ by wave over time. Both
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

        # Asset-class chart (row 9). Sum $ per (date, bucket). Explicit
        # colors so bonds (purple) and precious metals (gold) don't
        # collide on the log y-axis when their dollar values are close.
        ac_colors = {
            "equities":        "#1f77b4",  # blue
            "bonds":           "#9467bd",  # purple
            "cash":            "#7f7f7f",  # gray
            "precious metals": "#bcbd22",  # gold/olive
            "crypto":          "#17becf",  # cyan
        }
        ac = snaps_full.groupby(["date", "asset_bucket"])["value"].sum().unstack(fill_value=0)
        # Stable, intuitive ordering.
        ac_order = [c for c in ["equities", "bonds", "cash", "precious metals", "crypto"]
                    if c in ac.columns]
        for bucket in ac_order:
            fig.add_trace(
                go.Scatter(x=ac.index, y=ac[bucket], mode=_ts_mode,
                           name=bucket, legend="legend2",
                           line={"color": ac_colors.get(bucket, "#444")}),
                row=R_ASSET_USD, col=1,
            )

        # Wave chart (row 10). Same shape, different grouping. Waves
        # with zero $ in the watchlist (e.g., the user has shares=0
        # for every ticker in that wave) don't render on a log axis,
        # so we list them in an annotation at the chart's top-right
        # rather than silently dropping them.
        wv = snaps_full.groupby(["date", "wave_bucket"])["value"].sum().unstack(fill_value=0)
        # Use the same display order as elsewhere in the dashboard.
        wv_order = [w for w in _WAVE_DISPLAY_ORDER if w in wv.columns]
        zero_waves = [w for w in wv_order if (wv[w] <= 0).all()]
        for wave in wv_order:
            if wave in zero_waves:
                continue
            fig.add_trace(
                go.Scatter(x=wv.index, y=wv[wave], mode=_ts_mode,
                           name=WAVE_DISPLAY_LABEL.get(wave, wave),
                           legend="legend3",
                           line={"color": WAVE_COLORS.get(wave)}),
                row=R_WAVE_USD, col=1,
            )
        if zero_waves:
            # Anchored bottom-right of chart 10. On a log scale rising
            # over time, the bottom-right is the lowest non-zero wave
            # at the most recent date — typically the smallest active
            # wave bucket — so this corner has the most empty space.
            # Opaque white background occludes any line that does
            # happen to pass through.
            fig.add_annotation(
                xref=f"x{R_WAVE_USD} domain", yref=f"y{R_WAVE_USD} domain",
                x=0.99, y=0.03, xanchor="right", yanchor="bottom",
                text="At $0 today: " + ", ".join(WAVE_DISPLAY_LABEL.get(w, w) for w in zero_waves),
                showarrow=False,
                font={"size": 11, "color": "#666"},
                bgcolor="rgba(255,255,255,0.95)",
                bordercolor="#bbb", borderwidth=1, borderpad=4,
            )

    # 9. Rebalance turnover. Computed from recommendations.csv: at each
    # rebalance the dollar-fraction-of-portfolio that moved between
    # tickers equals (½ × ||w_new - w_prev||₁) where the weight vectors
    # are normalized to sum to 1. Step-function via line_shape="hv": the
    # value at each rebalance holds horizontally until the next one.
    if rec_path.exists():
        recs_for_turnover = pd.read_csv(rec_path, parse_dates=["date"])
        wide = (recs_for_turnover.pivot_table(index="date", columns="ticker",
                                              values="weight", fill_value=0)
                .sort_index())
        if len(wide) >= 2:
            diffs = wide.diff().abs().sum(axis=1) / 2.0
            diffs = diffs.dropna()
            if not diffs.empty:
                # Extend the last turnover value horizontally to the
                # right edge of the chart window so the step function
                # makes clear "this is the most recent turnover and it
                # remains in effect until the next rebalance". Markers
                # only appear at real rebalance dates.
                x_vals = list(diffs.index)
                y_vals = list(diffs.values * 100)
                marker_x = list(diffs.index)
                marker_y = list(diffs.values * 100)
                if latest_snap_date is not None and diffs.index[-1] < latest_snap_date:
                    x_vals.append(latest_snap_date)
                    y_vals.append(diffs.values[-1] * 100)
                fig.add_trace(
                    go.Scatter(x=x_vals, y=y_vals,
                               mode="lines",
                               name="Turnover",
                               line={"color": "#1f77b4", "width": 2, "shape": "hv"},
                               hovertemplate="%{x|%Y-%m-%d}<br>%{y:.1f}%<extra></extra>",
                               showlegend=False),
                    row=R_TURNOVER, col=1,
                )
                fig.add_trace(
                    go.Scatter(x=marker_x, y=marker_y,
                               mode="markers",
                               name="Turnover marker",
                               marker={"size": 9, "symbol": "square-open",
                                       "color": "#ff7f0e", "line": {"width": 2}},
                               hoverinfo="skip",
                               showlegend=False),
                    row=R_TURNOVER, col=1,
                )

    # Per-row top y in paper coords: row_top_k = 1 - (k-1) * (row_h + vsp)
    # where row_h = (1 - (n-1)*vsp) / n, vsp = 0.06.
    _vsp = 0.06
    _row_h = (1.0 - (n_rows - 1) * _vsp) / n_rows
    def _row_top(k: int) -> float:
        return 1.0 - (k - 1) * (_row_h + _vsp)

    title_text = "Portfolio Wave Rider — dashboard"
    if not is_live and latest_snap_date is not None:
        title_text = (f"Portfolio Wave Rider — backtest "
                      f"(executed {latest_snap_date.date()})")

    fig.update_layout(
        height=340 * n_rows,
        # Pin the page title above the plotting area and reserve top
        # margin space, so it doesn't overlap chart 1's multi-line
        # subplot title.
        title={"text": title_text, "y": 0.995, "yanchor": "top"},
        margin={"t": 100},
        # `closest` shows one trace's popup at a time, so hovering chart 1
        # shows portfolio $ OR SPY but not both (and chart 3 shows one
        # ticker's portfolio % at a time, which is cleaner with 7+ lines).
        hovermode="closest",
        # Per-subplot legends, one per chart, anchored at each row's top
        # in paper coordinates. Charts 4 (cap line), 5 / 6 (gain bars),
        # and the turnover trace use showlegend=False on their traces
        # so they need no separate legend dict.
        legend=dict(
            title_text="Portfolio value",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(1), yanchor="top",
        ),
        legend5=dict(
            title_text="Portfolio % by wave",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(3), yanchor="top",
        ),
        # Chart 4 (latest weights): small legend just for the cap line.
        legend7=dict(
            xref="paper", x=1.02,
            yref="paper", y=_row_top(4), yanchor="top",
        ),
        # Wave-stage chart has a secondary y-axis on the right; push this
        # legend further out (x=1.06) to clear that axis.
        legend6=dict(
            title_text="Wave stages",
            xref="paper", x=1.06,
            yref="paper", y=_row_top(R_WAVE_STAGE), yanchor="top",
        ),
        legend4=dict(
            title_text="Articles per wave",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(R_ARTICLES), yanchor="top",
        ),
        legend2=dict(
            title_text="Asset class $",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(R_ASSET_USD), yanchor="top",
        ),
        legend3=dict(
            title_text="Wave $",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(R_WAVE_USD), yanchor="top",
        ),
    )
    fig.update_yaxes(title_text="$", row=R_PORTFOLIO, col=1)
    if R_AI_LIFT is not None:
        fig.update_yaxes(title_text="ratio", row=R_AI_LIFT, col=1)
    fig.update_yaxes(title_text="portfolio %", row=R_REC_WAVE, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="portfolio %", row=R_LATEST_WEIGHTS, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="$ gain", row=R_GAIN_INIT, col=1, zeroline=True,
                     zerolinewidth=1, zerolinecolor="#888")
    if R_GAIN_REVIEW is not None:
        fig.update_yaxes(title_text="$ gain", row=R_GAIN_REVIEW, col=1, zeroline=True,
                         zerolinewidth=1, zerolinecolor="#888")
    # Chart 5: y-axis ticks show stage names alongside the numeric rank
    # so a reader can read the trajectory directly without remembering
    # 0=neutral, 1=buildup, 2=surge, etc.
    rank_to_stage = {v: k for k, v in WAVE_STAGE_RANK.items()}
    stage_ticktext = [f"{r} {rank_to_stage.get(r, '')}" for r in range(5)]
    fig.update_yaxes(title_text="stage", row=R_WAVE_STAGE, col=1, secondary_y=False,
                     range=[-0.3, 4.3],
                     tickmode="array",
                     tickvals=list(range(5)),
                     ticktext=stage_ticktext)
    # Right-side y-axis: the wave_stage_tilts multiplier for each rank,
    # loaded from the profile's financial_model section (falls back to
    # the WAVE_STAGE_TILT defaults). Same range and tickvals as the
    # primary y-axis so the rows line up. (No-op on the backtest
    # dashboard, which has no wave_history.csv input — chart 5 has no
    # primary traces there, so the secondary axis renders empty.)
    try:
        _tilts = load_financial_model()["wave_stage_tilts"]
    except Exception:  # noqa: BLE001 — profile is optional; fall back to defaults
        _tilts = WAVE_STAGE_TILT
    multiplier_ticktext = [
        f"×{_tilts.get(rank_to_stage.get(r, ''), 1.0):.2f}" for r in range(5)
    ]
    fig.update_yaxes(title_text="tilt", row=R_WAVE_STAGE, col=1, secondary_y=True,
                     range=[-0.3, 4.3],
                     tickmode="array",
                     tickvals=list(range(5)),
                     ticktext=multiplier_ticktext,
                     showgrid=False,
                     automargin=True)
    fig.update_yaxes(title_text="articles", row=R_ARTICLES, col=1, rangemode="tozero")
    fig.update_yaxes(title_text="$ (log)", row=R_ASSET_USD, col=1, type="log")
    # Log scale on chart 8 so small wave allocations (e.g., zero-weighted
    # robotics/biology lines hovering near a few hundred dollars) don't
    # collapse to the floor next to the dominant general_markets line.
    fig.update_yaxes(title_text="$ (log)", row=R_WAVE_USD, col=1, type="log")
    fig.update_yaxes(title_text="turnover (%)", row=R_TURNOVER, col=1, rangemode="tozero")

    # Apply the padded snapshots-derived range to every time-series
    # subplot so data points don't sit flush against the axis edges
    # and all 6 time-series charts share the same visual window even
    # when wave_history (chart 5) and articles (chart 6) update on
    # /review-portfolio cadence rather than daily. Charts 3 (latest
    # weights) and 4 (gain bars) are bar charts with categorical
    # x-axes so the range setter is a no-op there.
    if xrange is not None:
        xrange_rows = (R_PORTFOLIO, R_TURNOVER, R_REC_WAVE,
                       R_WAVE_STAGE, R_ARTICLES, R_ASSET_USD, R_WAVE_USD)
        if R_AI_LIFT is not None:
            xrange_rows = xrange_rows + (R_AI_LIFT,)
        for r in xrange_rows:
            fig.update_xaxes(range=list(xrange), row=r, col=1)

    o_path = Path(out_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(o_path), include_plotlyjs="cdn")

    # Inject the cross-page nav strip (between dashboards in docs/) just
    # inside <body>, if requested. Plotly's write_html doesn't expose a
    # body-injection hook, so we read the file back and rewrite it.
    nav_html = _render_nav_strip(nav_current)
    if nav_html:
        html = o_path.read_text(encoding="utf-8")
        html = html.replace("<body>", "<body>\n" + nav_html, 1)
        o_path.write_text(html, encoding="utf-8")

    # News content is no longer rendered into the dashboard HTML. The
    # latest /review-portfolio news payload (the one that drives the
    # wave-stage classifications) lives on its own page at
    # docs/news.html — see render_news_page().

    return {
        "out_path": str(o_path),
        "snapshots_rows": int(len(pd.read_csv(snap_path))) if snap_path.exists() else 0,
        "recommendations_rows": int(len(pd.read_csv(rec_path))) if rec_path.exists() else 0,
        "wave_history_rows": int(len(pd.read_csv(wh_path))) if wh_path.exists() else 0,
        "benchmarks_overlaid": list(benchmark_curves.keys()),
    }


def render_news_page(
    news_path: str = "data/news_latest.json",
    out_path: str = "docs/news.html",
    nav_current: str | None = "news",
) -> dict[str, Any]:
    """Render docs/news.html: a standalone page showing the latest
    /review-portfolio news payload (the bullets the news-researcher
    surfaced and used to classify each wave's stage).

    Companion to build_dashboard; the dashboard CLI calls this after
    writing index.html so cron + /review-portfolio refresh both files
    together. The news payload at ``news_path`` is overwritten on each
    /review-portfolio run, so this page always reflects the most recent
    classification rationale.
    """
    n_path = Path(news_path)
    if not n_path.exists():
        # No /review-portfolio has run yet. Write a minimal placeholder
        # so the nav-strip link doesn't break.
        body = (
            '<h2 style="margin-top:1.5em;">No wave-stage news yet</h2>'
            '<p style="color:#666;">Run <code>/review-portfolio</code> '
            'to surface the news that drives the optimizer\'s wave-stage '
            'tilts. The latest run\'s bullets will appear here.</p>'
        )
    else:
        try:
            payload = json.loads(n_path.read_text())
            body = _render_news_section(
                payload,
                title="Wave-stage news from last /review-portfolio",
                intro="The bullets below are the evidence the news-researcher "
                      "surfaced for each ticker on the most recent "
                      "<code>/review-portfolio</code> run. They drive each wave's "
                      "stage classification (chart 5 of the live dashboard) and "
                      "the per-ticker tilt the optimizer applies. Click any "
                      "headline to expand the LLM-written, portfolio-relevance "
                      "summary plus a link to the source. Tickers grouped by wave bucket.",
            )
            if not body:
                body = (
                    '<h2 style="margin-top:1.5em;">News payload is empty</h2>'
                    '<p style="color:#666;">The latest <code>news_latest.json</code> '
                    'has no per-ticker bullets.</p>'
                )
        except (json.JSONDecodeError, OSError) as e:
            body = (
                '<h2 style="margin-top:1.5em;">Could not read news payload</h2>'
                f'<p style="color:#666;">{_html.escape(type(e).__name__)}: '
                f'{_html.escape(str(e))}</p>'
            )

    nav_html = _render_nav_strip(nav_current)
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Portfolio Wave Rider — news</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:980px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5;}</style>'
        '</head><body>\n'
        + nav_html
        + body
        + '\n</body></html>'
    )
    o_path = Path(out_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    o_path.write_text(page, encoding="utf-8")
    return {"out_path": str(o_path), "news_payload_present": n_path.exists()}
