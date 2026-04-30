"""All portfolio math in one file.

Five functions that the Claude Code subagents call through ``src/cli.py``:

- ``fetch_prices`` — download adjusted-close prices from yfinance
- ``compute_returns`` — log-returns + annualized mean + covariance matrix
- ``optimize_portfolio`` — mean-variance optimization via scipy
- ``risk_metrics`` — Sharpe, vol, max drawdown, VaR, CVaR for a weight vector
- ``backtest`` — naive in-sample / out-of-sample split check

A tiny disk-backed ``put`` / ``get`` store at the top lets these functions
share DataFrames across separate Python processes (one per subagent CLI
call). The alternative — passing DataFrames through stdout — would blow
up the subagents' context.
"""

from __future__ import annotations

import os
import pickle
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
    """Multiply annualized mean returns by each ticker's stage tilt.

    Tickers absent from `wave_views` (or tagged with an unknown stage) are
    left unchanged.
    """
    tilted = mu.copy()
    for ticker, stage in wave_views.items():
        if ticker in tilted.index:
            tilted[ticker] = tilted[ticker] * WAVE_STAGE_TILT.get(stage, 1.0)
    return tilted


# ---------------------------------------------------------------------------
# Disk-backed handle store.
# Each ``put`` writes a pickle file under data/state/ and returns a handle
# like "prices_1" or "returns_1". Subsequent ``get`` calls load it back.
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    root = Path(os.environ.get("PORTFOLIO_STATE_DIR") or "data/state")
    root.mkdir(parents=True, exist_ok=True)
    return root


def put(prefix: str, obj: Any) -> str:
    """Save `obj` under a fresh handle with the given prefix; return the handle."""
    existing = [int(p.stem.rpartition("_")[2]) for p in _state_dir().glob(f"{prefix}_*.pkl")
                if p.stem.rpartition("_")[2].isdigit()]
    idx = (max(existing) + 1) if existing else 1
    handle = f"{prefix}_{idx}"
    with (_state_dir() / f"{handle}.pkl").open("wb") as f:
        pickle.dump(obj, f)
    return handle


def get(handle: str) -> Any:
    """Load the object stored under `handle`. Raises KeyError if missing."""
    path = _state_dir() / f"{handle}.pkl"
    if not path.exists():
        raise KeyError(f"Unknown handle: {handle!r}. Looked in {_state_dir()}.")
    with path.open("rb") as f:
        return pickle.load(f)


# ---------------------------------------------------------------------------
# Market data: fetch prices and turn them into a returns bundle.
# ---------------------------------------------------------------------------

def fetch_prices(tickers: list[str], period: str = "3y", interval: str = "1d") -> dict[str, Any]:
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
    prices = prices.dropna(how="all").ffill().dropna()

    handle = put("prices", prices)
    return {
        "prices_handle": handle,
        "tickers": list(prices.columns),
        "start": str(prices.index[0].date()),
        "end": str(prices.index[-1].date()),
        "n_observations": len(prices),
        "last_prices": {t: float(prices[t].iloc[-1]) for t in prices.columns},
    }


def compute_returns(prices_handle: str, frequency: str = "daily") -> dict[str, Any]:
    """Compute log-returns + annualized mean + covariance from a prices handle."""
    factor = {"daily": TRADING_DAYS, "weekly": 52, "monthly": 12}[frequency]
    prices = get(prices_handle)
    log_returns = np.log(prices / prices.shift(1)).dropna()
    mean_annual = log_returns.mean() * factor
    cov_annual = log_returns.cov() * factor

    handle = put("returns", {
        "log_returns": log_returns,
        "mean": mean_annual,
        "cov": cov_annual,
        "annualization": factor,
    })
    return {
        "returns_handle": handle,
        "tickers": list(log_returns.columns),
        "n_observations": len(log_returns),
        "annualized_mean_return": {k: float(v) for k, v in mean_annual.items()},
        "annualized_volatility": {t: float(np.sqrt(cov_annual.loc[t, t])) for t in cov_annual.index},
    }


# ---------------------------------------------------------------------------
# Mean-variance optimizer. Three objectives: max_sharpe, min_variance, target_return.
# Long-only by default, with an optional per-asset cap.
# ---------------------------------------------------------------------------

def optimize_portfolio(
    returns_handle: str,
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

    bundle = get(returns_handle)
    tickers = list(bundle["mean"].index)
    mean_series = apply_wave_tilt(bundle["mean"], wave_views) if wave_views else bundle["mean"]
    mu = mean_series.to_numpy(dtype=float)
    sigma = bundle["cov"].to_numpy(dtype=float)
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
# Risk metrics + backtest. Both apply a weight vector to a returns bundle.
# ---------------------------------------------------------------------------

def _summary(port_returns: pd.Series, risk_free_rate: float) -> dict[str, Any]:
    """Shared summary stats for a portfolio return series."""
    ann_ret = float(port_returns.mean() * TRADING_DAYS)
    ann_vol = float(port_returns.std() * np.sqrt(TRADING_DAYS))
    sharpe = (ann_ret - risk_free_rate) / ann_vol if ann_vol > 1e-10 else None
    equity = (1 + port_returns).cumprod()
    max_dd = float(((equity - equity.cummax()) / equity.cummax()).min())
    return {
        "annual_return": ann_ret,
        "annual_volatility": ann_vol,
        "sharpe_ratio": float(sharpe) if sharpe is not None else None,
        "max_drawdown": max_dd,
        "n_observations": len(port_returns),
        "period_start": str(port_returns.index[0].date()),
        "period_end": str(port_returns.index[-1].date()),
    }


def _portfolio_series(returns_handle: str, weights: dict[str, float]) -> pd.Series:
    """Apply the weights to a returns bundle and return the portfolio return series."""
    bundle = get(returns_handle)
    returns = bundle["log_returns"]
    missing = [t for t in returns.columns if t not in weights]
    if missing:
        raise ValueError(f"weights missing for tickers: {missing}")
    w = np.array([weights[t] for t in returns.columns], dtype=float)
    return pd.Series(returns.values @ w, index=returns.index)


def risk_metrics(
    returns_handle: str,
    weights: dict[str, float],
    risk_free_rate: float = 0.04,
    var_confidence: float = 0.95,
) -> dict[str, Any]:
    """Portfolio Sharpe, vol, max drawdown, VaR, CVaR for the given weights."""
    port = _portfolio_series(returns_handle, weights)
    out = _summary(port, risk_free_rate)
    alpha = 1 - var_confidence
    var = float(np.quantile(port.values, alpha))
    below_var = port.values[port.values <= var]
    out["var_1d"] = var
    out["cvar_1d"] = float(below_var.mean()) if below_var.size else var
    out["var_confidence"] = var_confidence
    return out


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

    The "automation" sibling of the /optimize-portfolio skill: pure
    Python, no news-researcher, no wave-stage tilts. Universe = the
    tickers listed in `holdings_path`. Schema appended to `out_path`:
        date, ticker, weight, expected_return, annual_volatility,
        sharpe_ratio, objective

    Idempotent on date (skip unless force=True). Run the full
    /optimize-portfolio skill when you want fresh wave-stage tilts
    and a written report.
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

    prices = fetch_prices(tickers, period=period)
    returns = compute_returns(prices["prices_handle"])
    opt = optimize_portfolio(
        returns["returns_handle"], objective=objective,
        risk_free_rate=risk_free_rate, max_weight=max_weight,
    )
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
    appears in the snapshot file, the call is a no-op unless force=True
    (in which case existing rows for that date are dropped first).
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


def backtest(
    returns_handle: str,
    weights: dict[str, float],
    train_fraction: float = 0.7,
    risk_free_rate: float = 0.04,
) -> dict[str, Any]:
    """Split the return series in two, report in-sample vs out-of-sample stats."""
    if not 0.1 < train_fraction < 0.95:
        raise ValueError("train_fraction must be between 0.1 and 0.95")
    port = _portfolio_series(returns_handle, weights)
    split = int(len(port) * train_fraction)
    train_stats = _summary(port.iloc[:split], risk_free_rate)
    test_stats = _summary(port.iloc[split:], risk_free_rate)

    # Sharpe degradation: a large drop suggests the in-sample result was partly luck.
    degradation = (
        test_stats["sharpe_ratio"] - train_stats["sharpe_ratio"]
        if train_stats["sharpe_ratio"] is not None and test_stats["sharpe_ratio"] is not None
        else None
    )
    return {
        "in_sample": train_stats,
        "out_of_sample": test_stats,
        "sharpe_degradation": degradation,
        "note": "A drop below -0.5 in Sharpe suggests estimation luck rather than signal.",
    }
