"""All portfolio math in one file.

Six public functions plus one orchestrator:

- ``fetch_prices`` — download adjusted-close prices from yfinance
- ``compute_returns`` — log-returns + annualized mean + covariance matrix
- ``optimize_portfolio`` — mean-variance optimization via scipy
- ``risk_metrics`` — Sharpe, vol, max drawdown, VaR, CVaR for a weight vector
- ``analyze`` — one-shot: fetch + returns + optimize + risk in one call
- ``snapshot_holdings`` — append daily $ values to data/snapshots.csv
- ``recommend_portfolio`` — append weekly weights to data/recommendations.csv
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


# ---------------------------------------------------------------------------
# Profile loader. Reads the YAML front matter of investor_profile.md and
# returns the financial_model section. Missing fields fall through to
# hard-coded defaults so old profiles (without the section) still work.
# ---------------------------------------------------------------------------

_FINANCIAL_MODEL_DEFAULTS: dict[str, Any] = {
    "risk_aversion": 1.0,
    "risk_free_rate": 0.04,
    "lookback_period": "3y",
    "rebalance_period": "monthly",
    "max_watchlist_size": 12,
}


def load_financial_model(profile_path: str = "investor_profile.md") -> dict[str, Any]:
    """Read `financial_model` from investor_profile.md's YAML front matter.

    Returns a dict with five fields (`risk_aversion`, `risk_free_rate`,
    `lookback_period`, `rebalance_period`, `max_watchlist_size`); any missing
    field falls back to the hard-coded default. If the profile file doesn't
    exist or has no front matter, all defaults are returned.

    The optimizer objective is intentionally not configurable here: this
    project commits to mean-variance maximization with ``risk_aversion`` (λ)
    as the only investor-facing knob on the return/variance tradeoff.
    Library callers of ``optimize_portfolio`` can still pass an explicit
    ``objective=`` to override per call.
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
    if objective == "mean_variance" and risk_aversion < 0:
        raise ValueError("risk_aversion (lambda) must be >= 0 for mean_variance objective")

    tickers = list(returns["mean"].index)
    mu = returns["mean"].to_numpy(dtype=float)
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
    risk_aversion: float = 1.0,
) -> dict[str, Any]:
    """Run the full pipeline and return a single JSON-serializable dict."""
    prices = fetch_prices(tickers, period=period)
    returns = compute_returns(prices)
    opt = optimize_portfolio(
        returns, objective=objective, risk_free_rate=risk_free_rate,
        max_weight=max_weight, risk_aversion=risk_aversion,
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


# ---------------------------------------------------------------------------
# Watchlist curation: consume a watchlist-curator payload, validate it, and
# mutate holdings.csv + data/curation_history.csv accordingly.
# ---------------------------------------------------------------------------

_VALID_WAVE_BUCKETS = {
    # Technology waves the profile may name as current or next.
    "AI", "robotics", "rockets_spacecraft", "nuclear", "quantum",
    "engineered_biology",
    # Non-technology waves the profile may name (geopolitical realignment,
    # demographic shifts, commodity cycles, regulatory inflections).
    "geopolitical", "demographics", "commodities", "regulatory",
    # Catch-all for tickers that aren't tied to any specific wave thesis
    # (broad-market ETFs, bonds, cash, gold as ballast).
    "general_markets",
}


def _check_ticker_listing_date(ticker: str, as_of_date: str) -> tuple[bool, str]:
    """Return (existed_on_as_of_date, reason). Uses yfinance to fetch a small
    window centered on as_of_date and checks for any returned rows.

    A return of (False, "...") means the ticker either did not exist yet or
    yfinance has no data for it on or near that date. The harness rejects
    such adds. yfinance errors propagate as (False, error_msg) rather than
    crashing, so a transient network problem won't take down the whole
    curate run.
    """
    try:
        d = pd.Timestamp(as_of_date)
    except Exception as e:
        return False, f"unparseable as_of_date: {e}"
    start = (d - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    end = (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start, end=end, interval="1d",
                         auto_adjust=True, progress=False, threads=False)
    except Exception as e:
        return False, f"yfinance error: {e}"
    if df is None or df.empty:
        return False, f"no price data on or before {as_of_date}"
    return True, "ok"


def _validate_curator_payload(
    payload: dict[str, Any],
    current_watchlist: list[str],
    max_watchlist_size: int,
    listing_check: bool = True,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Validate a watchlist-curator JSON payload against the contract rules.

    Returns a dict with `valid_adds`, `valid_removes`, and `rejections`
    (list of {ticker, action, reason}). Does not mutate any files.

    Rules enforced:
      - Top-level shape: as_of_date, adds, removes, no_changes must be present
      - At most 3 adds and 3 removes per call
      - adds must carry ticker, wave_bucket, rationale, news_evidence
      - wave_bucket must be in _VALID_WAVE_BUCKETS
      - news_evidence must be a non-empty list with at least one bullet
      - no ticker can appear in both adds and removes
      - adds cannot target tickers already in current_watchlist
      - removes must target tickers in current_watchlist
      - post-change watchlist size must be <= max_watchlist_size
      - if listing_check, each add's ticker must have yfinance data on
        the as_of_date (either the payload's or the override)
    """
    rejections: list[dict[str, str]] = []
    raw_adds = payload.get("adds") or []
    raw_removes = payload.get("removes") or []
    if not isinstance(raw_adds, list) or not isinstance(raw_removes, list):
        raise ValueError("adds and removes must be lists")
    if len(raw_adds) > 3:
        raise ValueError(f"at most 3 adds per call; got {len(raw_adds)}")
    if len(raw_removes) > 3:
        raise ValueError(f"at most 3 removes per call; got {len(raw_removes)}")

    add_tickers = {a.get("ticker") for a in raw_adds if isinstance(a, dict)}
    remove_tickers = {r.get("ticker") for r in raw_removes if isinstance(r, dict)}
    overlap = add_tickers & remove_tickers
    if overlap:
        raise ValueError(f"ticker(s) appear in both adds and removes: {sorted(overlap)}")

    current_set = set(current_watchlist)
    valid_adds: list[dict[str, Any]] = []
    asof = as_of_date or payload.get("as_of_date")

    for add in raw_adds:
        t = add.get("ticker")
        wb = add.get("wave_bucket")
        rationale = (add.get("rationale") or "").strip()
        evidence = add.get("news_evidence") or []
        if not t:
            rejections.append({"ticker": str(t), "action": "add", "reason": "missing ticker"})
            continue
        if t in current_set:
            rejections.append({"ticker": t, "action": "add",
                               "reason": "already in current_watchlist"})
            continue
        if wb not in _VALID_WAVE_BUCKETS:
            rejections.append({"ticker": t, "action": "add",
                               "reason": f"invalid wave_bucket: {wb!r}"})
            continue
        if not rationale:
            rejections.append({"ticker": t, "action": "add",
                               "reason": "empty rationale"})
            continue
        if not isinstance(evidence, list) or len(evidence) == 0:
            rejections.append({"ticker": t, "action": "add",
                               "reason": "news_evidence must be a non-empty list"})
            continue
        if listing_check and asof:
            ok, msg = _check_ticker_listing_date(t, asof)
            if not ok:
                rejections.append({"ticker": t, "action": "add",
                                   "reason": f"listing-date check failed: {msg}"})
                continue
        valid_adds.append(add)

    valid_removes: list[dict[str, Any]] = []
    for rem in raw_removes:
        t = rem.get("ticker")
        rationale = (rem.get("rationale") or "").strip()
        if not t:
            rejections.append({"ticker": str(t), "action": "remove",
                               "reason": "missing ticker"})
            continue
        if t not in current_set:
            rejections.append({"ticker": t, "action": "remove",
                               "reason": "not in current_watchlist"})
            continue
        if not rationale:
            rejections.append({"ticker": t, "action": "remove",
                               "reason": "empty rationale"})
            continue
        valid_removes.append(rem)

    # Cap check: post-change size = current - removes + adds.
    post_size = len(current_set
                    - {r["ticker"] for r in valid_removes}
                    | {a["ticker"] for a in valid_adds})
    if post_size > max_watchlist_size:
        excess = post_size - max_watchlist_size
        dropped = [a["ticker"] for a in valid_adds[-excess:]]
        for t in dropped:
            rejections.append({"ticker": t, "action": "add",
                               "reason": f"would exceed max_watchlist_size={max_watchlist_size}"})
        valid_adds = valid_adds[:-excess]

    return {
        "valid_adds": valid_adds,
        "valid_removes": valid_removes,
        "rejections": rejections,
    }


def apply_curator_decisions(
    payload: dict[str, Any],
    holdings_path: str = "holdings.csv",
    history_path: str = "data/curation_history.csv",
    profile_path: str = "investor_profile.md",
    listing_check: bool = True,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Validate a watchlist-curator payload and apply it to holdings.csv.

    Adds are appended to holdings.csv at shares=0. Removes delete the row
    (positions with shares>0 are blocked from removal; the user must zero
    out their position first in the brokerage and update holdings.csv).
    Every applied change is appended as a row to curation_history.csv.

    Returns a result dict with applied/rejected lists and the post-change
    watchlist.
    """
    fm = load_financial_model(profile_path)
    max_size = int(fm.get("max_watchlist_size", 12))

    h_path = Path(holdings_path)
    if not h_path.exists():
        raise FileNotFoundError(f"holdings file not found: {h_path}")
    holdings = pd.read_csv(h_path)
    if "ticker" not in holdings.columns or "shares" not in holdings.columns:
        raise ValueError(f"{h_path} must have ticker,shares columns")
    current_watchlist = holdings["ticker"].astype(str).tolist()

    validated = _validate_curator_payload(
        payload, current_watchlist, max_size,
        listing_check=listing_check, as_of_date=as_of_date,
    )
    valid_adds = validated["valid_adds"]
    valid_removes = validated["valid_removes"]
    rejections = validated["rejections"]

    # Block removes for tickers with shares > 0 - the user has a live
    # position that must be liquidated in the brokerage first.
    held = {row["ticker"]: float(row["shares"]) for _, row in holdings.iterrows()}
    safe_removes: list[dict[str, Any]] = []
    for rem in valid_removes:
        t = rem["ticker"]
        if held.get(t, 0.0) > 0.0:
            rejections.append({"ticker": t, "action": "remove",
                               "reason": f"current shares={held[t]} > 0; liquidate first"})
        else:
            safe_removes.append(rem)
    valid_removes = safe_removes

    # Apply adds (append rows at shares=0) and removes (delete rows).
    new_rows = pd.DataFrame([{"ticker": a["ticker"], "shares": 0}
                             for a in valid_adds])
    if not new_rows.empty:
        holdings = pd.concat([holdings, new_rows], ignore_index=True)
    if valid_removes:
        rm_set = {r["ticker"] for r in valid_removes}
        holdings = holdings[~holdings["ticker"].isin(rm_set)].reset_index(drop=True)

    holdings.to_csv(h_path, index=False)

    # Append to curation_history.csv. One row per applied change.
    history_p = Path(history_path)
    history_p.parent.mkdir(parents=True, exist_ok=True)
    asof = as_of_date or payload.get("as_of_date") or pd.Timestamp.today().strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for a in valid_adds:
        urls = ";".join(e.get("url", "") for e in (a.get("news_evidence") or [])
                        if isinstance(e, dict))
        rows.append({
            "date": asof,
            "action": "add",
            "ticker": a["ticker"],
            "wave_bucket": a.get("wave_bucket", ""),
            "rationale": a.get("rationale", "").strip(),
            "news_evidence_urls": urls,
        })
    for r in valid_removes:
        urls = ";".join(e.get("url", "") for e in (r.get("news_evidence") or [])
                        if isinstance(e, dict))
        rows.append({
            "date": asof,
            "action": "remove",
            "ticker": r["ticker"],
            "wave_bucket": "",
            "rationale": r.get("rationale", "").strip(),
            "news_evidence_urls": urls,
        })

    if rows:
        new_history = pd.DataFrame(rows)
        if history_p.exists():
            existing = pd.read_csv(history_p)
            new_history = pd.concat([existing, new_history], ignore_index=True)
        new_history.to_csv(history_p, index=False)

    return {
        "as_of_date": asof,
        "applied_adds": [a["ticker"] for a in valid_adds],
        "applied_removes": [r["ticker"] for r in valid_removes],
        "rejections": rejections,
        "post_watchlist": holdings["ticker"].astype(str).tolist(),
        "holdings_path": str(h_path),
        "history_path": str(history_p),
    }


def reconstruct_watchlist_at(
    target_date: str,
    day_zero_tickers: list[str],
    history_path: str = "data/curation_history.csv",
) -> list[str]:
    """Replay curation_history.csv forward from day 0 up through target_date.

    The caller provides the day-0 starter watchlist (typically the keys of
    thesis_baseline.json's `holdings`, or the day-0 starter list for a
    backtest run). Returns the sorted list of tickers active on target_date.
    """
    watchlist = set(day_zero_tickers)
    history_p = Path(history_path)
    if not history_p.exists():
        return sorted(watchlist)
    df = pd.read_csv(history_p)
    if df.empty:
        return sorted(watchlist)
    target = pd.Timestamp(target_date)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] <= target].sort_values("date")
    for _, row in df.iterrows():
        if row["action"] == "add":
            watchlist.add(str(row["ticker"]))
        elif row["action"] == "remove":
            watchlist.discard(str(row["ticker"]))
    return sorted(watchlist)


def recommend_portfolio(
    holdings_path: str = "holdings.csv",
    out_path: str = "data/recommendations.csv",
    period: str = "3y",
    max_weight: float = 0.25,
    risk_free_rate: float = 0.04,
    objective: str = "max_sharpe",
    risk_aversion: float = 1.0,
    date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run an optimization and append per-ticker weights to a CSV.

    Pure Python, no news pulls, no LLM. Universe = the tickers listed in
    ``holdings_path``.

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

    result = analyze(tickers, period=period, objective=objective,
                     max_weight=max_weight, risk_free_rate=risk_free_rate,
                     risk_aversion=risk_aversion)
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
    lookback_years: float = 1.3,
    max_weight: float = 0.25,
    objective: str = "max_sharpe",
    risk_aversion: float = 1.0,
    risk_free_rate: float = 0.04,
    benchmarks: list[str] | None = None,
    publish_docs: bool = True,
) -> dict[str, Any]:
    """Walk-forward monthly-rebalance backtest of the lightweight Python-only path.

    On the first trading day of each month in [start_date, end_date], runs the
    optimizer with a `lookback_years`-long window ending that day and rebalances
    the portfolio to those weights. Daily snapshots in between record the
    drifting value. No transaction costs are modeled. The point is to verify
    that the math-only system produces stable, profitable recommendations on
    real historical data.

    **Cadence is hardcoded to monthly** in this path; the profile's
    `rebalance_period` field is NOT consulted here. For cadence-aware backtests
    use ``curator_backtest`` (CLI: ``backtest --curator-runs-dir <dir>``),
    which reads ``rebalance_period`` from the runs dir's ``_starter.json`` and
    branches via ``_cadence_period_id`` on monthly / quarterly / semi_annual /
    annual.

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

    # Iterate. Friday = rebalance; every trading day = snapshot.
    snap_rows: list[dict[str, Any]] = []
    rec_rows: list[dict[str, Any]] = []
    current_shares: dict[str, float] | None = None
    last_weights: dict[str, float] | None = None
    weight_l1_distances: list[float] = []
    last_rebalance_month: int | None = None

    for date in daily_dates:
        # Monthly rebalance cadence: fire on the first trading day of each
        # month. Matches the live system's /review-portfolio cadence.
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
        f"- Look-ahead-bias-free: each rebalance's optimizer sees only prices "
        f"up to that date.\n"
        f"- The lookback window is the same one the live system uses, so "
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
    # No nav strip on either backtest copy — backtest is a leaf page
    # reachable only from the README.
    targets = [str(out / "dashboard.html")]
    if publish_docs:
        targets.append("docs/backtest.html")
    rendered: list[str] = []
    for path in targets:
        try:
            build_dashboard(
                snapshots_path=str(out / "snapshots.csv"),
                recommendations_path=str(out / "recommendations.csv"),
                out_path=path,
                benchmarks=benchmarks,
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
# Curator-driven backtest: replay a directory of watchlist-curator JSON
# payloads through the mean-variance optimizer, computing two baselines
# (fixed-watchlist same cadence; buy-and-hold of starter) in the same loop.
# No LLM is invoked here — the agent decisions are pre-collected upstream
# (the /run-curator-backtest skill in stage C2b fires the curator agents).
# ---------------------------------------------------------------------------

def _cadence_period_id(date: pd.Timestamp, cadence: str) -> tuple[int, ...]:
    """Period bucket used to detect rebalance boundaries."""
    if cadence == "monthly":
        return (date.year, date.month)
    if cadence == "quarterly":
        return (date.year, (date.month - 1) // 3)
    if cadence == "semi_annual":
        return (date.year, (date.month - 1) // 6)
    if cadence == "annual":
        return (date.year,)
    raise ValueError(f"unknown cadence: {cadence!r}")


def _optimize_or_equal_weight(
    returns: dict[str, Any], tickers: list[str], objective: str,
    max_weight: float, risk_aversion: float, risk_free_rate: float,
) -> dict[str, float]:
    """Run the optimizer, falling back to equal-weight if it can't converge
    or if max_weight is too tight for the watchlist size.

    Auto-relaxes max_weight so n * max_weight >= 1 always holds; otherwise
    the optimizer raises and a small watchlist (curator trimmed below the
    feasibility floor) would crash the backtest mid-run.
    """
    n = max(1, len(tickers))
    eff_cap = max(max_weight, 1.0 / n + 1e-6)
    eff_cap = min(eff_cap, 1.0)
    opt = optimize_portfolio(
        returns, objective=objective, risk_free_rate=risk_free_rate,
        max_weight=eff_cap, risk_aversion=risk_aversion,
    )
    if opt.get("success"):
        return opt
    # Fall back to equal-weight if the optimizer doesn't converge.
    weights = {t: 1.0 / n for t in tickers}
    return {
        "success": False, "weights": weights,
        "expected_annual_return": 0.0, "annual_volatility": 0.0,
        "sharpe_ratio": 0.0,
    }


def curator_backtest(
    runs_dir: str,
    out_dir: str = "data/backtest/",
    max_weight: float = 0.25,
    objective: str = "mean_variance",
    risk_aversion: float = 1.0,
    risk_free_rate: float = 0.04,
    benchmarks: list[str] | None = None,
    lookback_years_override: float | None = None,
) -> dict[str, Any]:
    """Replay a curator-runs directory through the optimizer.

    Reads ``<runs_dir>/_starter.json`` for the run config (starter watchlist,
    start/end dates, rebalance cadence, initial USD, lookback years). Then
    for each rebalance date reads ``<runs_dir>/<date>-curation.json`` if
    present and applies it via ``apply_curator_decisions`` to a sandboxed
    holdings + history pair under ``<out_dir>/sandbox/``. Runs the optimizer
    on the resulting watchlist and walks forward day-by-day.

    Two baselines are computed in the same loop and emitted as a separate
    totals CSV that the dashboard can overlay later:

      - **Fixed-watchlist**: same cadence and optimizer, watchlist locked
        to the starter set forever. Isolates whether the curation is
        actually adding value vs just the mean-variance rebalancing.
      - **Buy-and-hold**: one optimizer call on day 0 against the starter,
        then no rebalancing. Isolates whether the rebalancing matters.

    Outputs under ``out_dir``:
      - ``snapshots.csv`` — curator strategy, same schema as live data
      - ``recommendations.csv`` — one row block per rebalance, curator strategy
      - ``baselines_totals.csv`` — date, fixed_total, bnh_total
      - ``report.md``
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs = Path(runs_dir)
    if not runs.exists():
        raise FileNotFoundError(f"runs dir not found: {runs}")

    starter_path = runs / "_starter.json"
    if not starter_path.exists():
        raise FileNotFoundError(f"runs dir missing _starter.json: {runs}")
    starter = json.loads(starter_path.read_text())
    starter_watchlist = [t.upper() for t in starter["starter_watchlist"]]
    start_date = pd.Timestamp(starter["start_date"])
    end_date = pd.Timestamp(starter["end_date"])
    cadence = starter.get("rebalance_period", "monthly")
    initial_usd = float(starter.get("initial_usd", 50000.0))
    lookback_years = float(lookback_years_override) if lookback_years_override is not None \
        else float(starter.get("lookback_years", 1.3))
    max_size = int(starter.get("max_watchlist_size", 12))

    # Union of every ticker that could appear across the run, so the
    # bulk yfinance fetch only happens once.
    union: set[str] = set(starter_watchlist)
    curation_files: dict[pd.Timestamp, Path] = {}
    for p in sorted(runs.glob("*-curation.json")):
        d = pd.Timestamp(p.stem.replace("-curation", ""))
        curation_files[d] = p
        payload = json.loads(p.read_text())
        for a in payload.get("adds") or []:
            if isinstance(a, dict) and a.get("ticker"):
                union.add(a["ticker"].upper())
    universe = sorted(union)

    fetch_start = start_date - pd.Timedelta(days=365 * lookback_years + 30)
    raw = yf.download(universe, start=fetch_start,
                      end=end_date + pd.Timedelta(days=1),
                      auto_adjust=True, progress=False, group_by="column")
    if raw.empty:
        raise RuntimeError(f"yfinance returned no data for {universe}")
    full_prices = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
        else raw[["Close"]].rename(columns={"Close": universe[0]})
    full_prices = full_prices.dropna(how="all").ffill()
    daily_dates = full_prices.loc[start_date:end_date].dropna(how="all").index
    if len(daily_dates) < 5:
        raise RuntimeError(f"only {len(daily_dates)} trading days in window")

    # Sandboxed holdings + history files that the curate path mutates.
    sandbox = out / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    sandbox_holdings = sandbox / "holdings.csv"
    sandbox_history = sandbox / "curation_history.csv"
    pd.DataFrame({"ticker": starter_watchlist,
                  "shares": [0] * len(starter_watchlist)}).to_csv(
        sandbox_holdings, index=False)
    if sandbox_history.exists():
        sandbox_history.unlink()
    sandbox_profile = sandbox / "profile.md"
    sandbox_profile.write_text(
        f"---\nfinancial_model:\n  max_watchlist_size: {max_size}\n---\n"
    )

    # Four parallel walk-forwards.
    cur_shares: dict[str, float] = {}
    fix_shares: dict[str, float] = {}
    bnh_shares: dict[str, float] = {}      # optimizer day-0 weights held forever (ablation)
    eq_shares: dict[str, float] = {}       # equal-weight starter held forever (headline)
    snap_rows: list[dict[str, Any]] = []
    rec_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    weight_l1: list[float] = []
    last_weights: dict[str, float] | None = None
    last_period: tuple | None = None
    curation_summary: list[dict[str, Any]] = []

    def _value(shares: dict[str, float], date: pd.Timestamp) -> float:
        return sum(s * float(full_prices.loc[date, t])
                   for t, s in shares.items()
                   if t in full_prices.columns
                   and not pd.isna(full_prices.loc[date, t]))

    for date in daily_dates:
        period = _cadence_period_id(date, cadence)
        is_new_period = period != last_period
        is_first_day = date == daily_dates[0]

        if is_new_period or is_first_day:
            # 1) Apply that date's curation payload (if any) to the sandbox.
            #    Match payload to the trading day on/after its as_of_date.
            applied_keys = [
                k for k in curation_files
                if (last_period is None and k <= date)
                or (last_period is not None and k <= date and k > pd.Timestamp(
                    daily_dates[max(0, list(daily_dates).index(date) - 35)]))
            ]
            for k in sorted(applied_keys):
                if k in curation_files:
                    payload = json.loads(curation_files[k].read_text())
                    try:
                        result = apply_curator_decisions(
                            payload,
                            holdings_path=str(sandbox_holdings),
                            history_path=str(sandbox_history),
                            profile_path=str(sandbox_profile),
                            listing_check=False,  # universe already prefetched
                            as_of_date=str(k.date()),
                        )
                        curation_summary.append({
                            "date": str(k.date()),
                            "adds": result["applied_adds"],
                            "removes": result["applied_removes"],
                            "rejections": len(result["rejections"]),
                        })
                    except Exception as e:  # noqa: BLE001
                        curation_summary.append({
                            "date": str(k.date()),
                            "error": str(e),
                        })
                    del curation_files[k]

            # 2) Current curator watchlist after applying.
            cur_watchlist = pd.read_csv(sandbox_holdings)["ticker"].astype(str).tolist()
            cur_watchlist = [t for t in cur_watchlist if t in full_prices.columns]

            # 3) Lookback slice and optimizer call for curator strategy.
            lookback_start = date - pd.Timedelta(days=365 * lookback_years)
            slice_cur = full_prices.loc[lookback_start:date, cur_watchlist].dropna(how="any", axis=1)
            cur_watchlist = list(slice_cur.columns)
            if len(slice_cur) < 30 or not cur_watchlist:
                # Not enough history yet; carry forward without rebalancing.
                last_period = period
                continue
            returns = compute_returns(slice_cur)
            opt = _optimize_or_equal_weight(
                returns, cur_watchlist, objective, max_weight,
                risk_aversion, risk_free_rate,
            )
            cur_weights = opt["weights"]

            cur_value = _value(cur_shares, date) if cur_shares else initial_usd
            cur_shares = {
                t: (cur_weights[t] * cur_value) / float(full_prices.loc[date, t])
                for t in cur_watchlist
            }
            for t in cur_watchlist:
                rec_rows.append({
                    "date": str(date.date()),
                    "ticker": t,
                    "weight": cur_weights[t],
                    "expected_return": opt.get("expected_annual_return", 0.0),
                    "annual_volatility": opt.get("annual_volatility", 0.0),
                    "sharpe_ratio": opt.get("sharpe_ratio", 0.0),
                    "objective": objective,
                })
            if last_weights is not None:
                l1 = sum(abs(cur_weights.get(t, 0) - last_weights.get(t, 0))
                          for t in set(cur_weights) | set(last_weights))
                weight_l1.append(l1)
            last_weights = cur_weights

            # 4) Fixed-watchlist baseline: same optimizer, locked watchlist.
            slice_fix = full_prices.loc[lookback_start:date, starter_watchlist].dropna(how="any", axis=1)
            fix_watch = list(slice_fix.columns)
            if len(slice_fix) >= 30 and fix_watch:
                fix_returns = compute_returns(slice_fix)
                fix_opt = _optimize_or_equal_weight(
                    fix_returns, fix_watch, objective, max_weight,
                    risk_aversion, risk_free_rate,
                )
                fix_value = _value(fix_shares, date) if fix_shares else initial_usd
                fix_shares = {
                    t: (fix_opt["weights"][t] * fix_value) / float(full_prices.loc[date, t])
                    for t in fix_watch
                }

            # 5) Buy-and-hold baseline: optimize once on day 0, then hold.
            if not bnh_shares:
                bnh_value = initial_usd
                bnh_shares = {
                    t: (fix_opt["weights"][t] * bnh_value) / float(full_prices.loc[date, t])
                    for t in fix_watch
                }
            # 6) Equal-weight buy-and-hold baseline: $initial_usd / N to
            #    each starter ticker on day 0, then hold forever. This is
            #    the headline comparator a typical 2021 retail investor
            #    might actually have built without an optimizer's
            #    concentration tilt.
            if not eq_shares:
                w_eq = 1.0 / len(fix_watch)
                eq_shares = {
                    t: (w_eq * initial_usd) / float(full_prices.loc[date, t])
                    for t in fix_watch
                }

            last_period = period

        # Daily snapshot for the curator strategy.
        if cur_shares:
            day_total = _value(cur_shares, date)
            for t, sh in cur_shares.items():
                px = float(full_prices.loc[date, t])
                snap_rows.append({
                    "date": str(date.date()),
                    "ticker": t,
                    "shares": round(sh, 4),
                    "price": px,
                    "value": round(sh * px, 2),
                    "total_value": round(day_total, 2),
                })
        # Daily baseline totals (single row per date).
        if fix_shares or bnh_shares or eq_shares:
            baseline_rows.append({
                "date": str(date.date()),
                "fixed_total": round(_value(fix_shares, date), 2) if fix_shares else None,
                "bnh_total": round(_value(bnh_shares, date), 2) if bnh_shares else None,
                "eq_total": round(_value(eq_shares, date), 2) if eq_shares else None,
            })

    if not snap_rows:
        raise RuntimeError("curator_backtest produced no snapshots")

    snap_df = pd.DataFrame(snap_rows)
    rec_df = pd.DataFrame(rec_rows)
    baselines_df = pd.DataFrame(baseline_rows)
    snap_df.to_csv(out / "snapshots.csv", index=False)
    rec_df.to_csv(out / "recommendations.csv", index=False)
    baselines_df.to_csv(out / "baselines_totals.csv", index=False)
    (out / "curation_summary.json").write_text(json.dumps(curation_summary, indent=2))

    totals = snap_df.groupby("date")["total_value"].first().sort_index()
    initial_v = float(totals.iloc[0])
    final_v = float(totals.iloc[-1])
    realized_return = (final_v / initial_v) - 1.0
    days = (pd.Timestamp(totals.index[-1]) - pd.Timestamp(totals.index[0])).days or 1
    annualized = (final_v / initial_v) ** (365.0 / days) - 1.0
    equity = totals.values
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = float(dd.min())

    fix_initial = baselines_df["fixed_total"].dropna().iloc[0] if "fixed_total" in baselines_df else None
    fix_final = baselines_df["fixed_total"].dropna().iloc[-1] if "fixed_total" in baselines_df else None
    fix_return = (fix_final / fix_initial - 1.0) if fix_initial else None
    # Headline buy-and-hold = equal-weight starter held forever (eq_total).
    # bnh_total (optimizer day-0 weights held forever) remains in the CSV
    # as a hidden ablation.
    bnh_initial = baselines_df["eq_total"].dropna().iloc[0] if "eq_total" in baselines_df else None
    bnh_final = baselines_df["eq_total"].dropna().iloc[-1] if "eq_total" in baselines_df else None
    bnh_return = (bnh_final / bnh_initial - 1.0) if bnh_initial else None

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
        f"| {b} | {ret * 100:+.2f}% | {(realized_return - ret) * 100:+.2f}pp |\n"
        for b, ret in benchmark_returns.items()
    )

    n_rebalances = len(weight_l1) + 1
    n_curations = sum(1 for c in curation_summary if "error" not in c)
    n_adds = sum(len(c.get("adds", [])) for c in curation_summary)
    n_removes = sum(len(c.get("removes", [])) for c in curation_summary)
    weight_stability = float(np.mean(weight_l1)) if weight_l1 else 0.0
    fix_str = f"{fix_return * 100:+.2f}%" if fix_return is not None else "n/a"
    bnh_str = f"{bnh_return * 100:+.2f}%" if bnh_return is not None else "n/a"
    fix_active = (f"{(realized_return - fix_return) * 100:+.2f}pp"
                  if fix_return is not None else "n/a")
    bnh_active = (f"{(realized_return - bnh_return) * 100:+.2f}pp"
                  if bnh_return is not None else "n/a")
    report = (
        f"# Curator backtest report\n\n"
        f"**Window:** {totals.index[0]} to {totals.index[-1]} "
        f"({days} calendar days, {len(totals)} trading days)\n"
        f"**Starter watchlist:** {', '.join(starter_watchlist)}\n"
        f"**Cadence:** {cadence}\n"
        f"**Optimizer:** `{objective}`, lookback {lookback_years}y, "
        f"max_weight {max_weight:.2f}\n\n"
        f"## Curation activity\n\n"
        f"| Metric | Value |\n|---|---|\n"
        f"| Curation calls applied | {n_curations} |\n"
        f"| Adds executed | {n_adds} |\n"
        f"| Removes executed | {n_removes} |\n"
        f"| Final watchlist size | {len(cur_shares)} |\n"
        f"| Rebalances (optimizer calls) | {n_rebalances} |\n"
        f"| Mean L1 weight distance rebalance-to-rebalance | {weight_stability:.4f} |\n\n"
        f"## Realized performance vs baselines\n\n"
        f"| Strategy | Ending value | Total return | Active vs curator |\n"
        f"|---|---|---|---|\n"
        f"| Curator-driven | ${final_v:,.2f} | {realized_return * 100:+.2f}% | — |\n"
        f"| Buy-and-hold starter (equal-weight, then hold) | "
        f"${bnh_final:,.2f} | {bnh_str} | {bnh_active} |\n\n"
        f"## Risk and benchmarks\n\n"
        f"| Metric | Value |\n|---|---|\n"
        f"| Annualized return (curator) | {annualized * 100:+.2f}% |\n"
        f"| Max drawdown (curator) | {max_dd * 100:.2f}% |\n\n"
        f"### Benchmarks (over the same window)\n\n"
        f"| Benchmark | Return | Active vs curator |\n|---|---|---|\n"
        f"{bench_lines}\n"
        f"## Caveats\n\n"
        f"- No transaction costs or taxes modeled.\n"
        f"- Look-ahead-bias guard: each optimizer call sees prices only up "
        f"to that date; the curator payloads in this run were generated "
        f"with strict as-of-date discipline (see the watchlist-curator agent spec).\n"
        f"- Tickers added by the curator that have less than 30 trading days "
        f"of history at the rebalance date are dropped from the optimizer's "
        f"slice for that rebalance only.\n"
    )
    (out / "report.md").write_text(report)

    return {
        "out_dir": str(out),
        "window": {"start": str(totals.index[0]), "end": str(totals.index[-1]), "days": int(days)},
        "n_rebalances": n_rebalances,
        "n_curations_applied": n_curations,
        "n_adds": n_adds,
        "n_removes": n_removes,
        "final_watchlist": sorted(cur_shares.keys()),
        "initial_value": round(initial_v, 2),
        "final_value": round(final_v, 2),
        "realized_return": round(realized_return, 4),
        "annualized_return": round(annualized, 4),
        "max_drawdown": round(max_dd, 4),
        "weight_stability_l1": round(weight_stability, 4),
        "fixed_baseline_return": round(fix_return, 4) if fix_return is not None else None,
        "bnh_baseline_return": round(bnh_return, 4) if bnh_return is not None else None,
        "benchmark_returns": {b: round(r, 4) for b, r in benchmark_returns.items()},
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
    "quantum", "nuclear_fusion", "general_markets", "cashlike",
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
    "cashlike":           "#0d9488",  # deep teal — distinct from any wave hue
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
WAVE_DISPLAY_LABEL: dict[str, str] = {
    "AI": "AI",
    "robotics": "robotics",
    "rockets_spacecraft": "rockets",
    "engineered_biology": "biology",
    "quantum": "quantum",
    "nuclear_fusion": "nuclear",
    "general_markets": "general_markets",
    "cashlike": "cashlike",
}


def _ticker_label(t: str) -> str:
    """Two-line tick label: ticker on top, wave bucket (equities) or
    asset class (non-equities) on the line below.
    Equities: 'TICKER<br><sub>wave</sub>'; equity ETFs: '... wave ETF';
    non-equities: 'TICKER<br><sub>asset class</sub>'."""
    cls = TICKER_ASSET_CLASS.get(t, "equity")
    if cls == "equity":
        wave = WAVE_DISPLAY_LABEL.get(TICKER_WAVE.get(t, "general_markets"), "")
        return f"{t}<br><sub>{wave}</sub>"
    if cls == "equity ETF":
        wave = WAVE_DISPLAY_LABEL.get(TICKER_WAVE.get(t, "general_markets"), "")
        return f"{t}<br><sub>{wave} ETF</sub>"
    return f"{t}<br><sub>{cls}</sub>"






# Pages and the labels they expose in the cross-page nav strip. Keys are
# the bare filenames (no path) of the published GitHub Pages files.
_NAV_PAGES: list[tuple[str, str]] = [
    ("index.html", "live dashboard"),
    ("backtest_curator.html", "5y backtest"),
    ("sweep_risk_aversion.html", "sweep: risk_aversion"),
    ("sweep_lookback.html", "sweep: lookback"),
    ("sweep_max_weight.html", "sweep: max_weight"),
    ("sweep_max_watchlist_size.html", "sweep: max_watchlist_size"),
]


def _nav_strip(current: str) -> str:
    """Return an HTML <nav> with links to all published pages.
    The entry whose filename matches ``current`` is rendered as bold text
    instead of a link, so a reader can see which page they're on."""
    parts = []
    for fname, label in _NAV_PAGES:
        if fname == current:
            parts.append(f"<strong>{label}</strong>")
        else:
            parts.append(f'<a href="{fname}">{label}</a>')
    return (
        '<nav style="font-size:14px;color:#555;margin:0 0 1em 0;'
        'padding-bottom:0.5em;border-bottom:1px solid #eee;">'
        + " · ".join(parts) +
        '</nav>'
    )


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


def _compute_expected_vs_realized(
    rec_df: pd.DataFrame, snap_df: pd.DataFrame, window_days: int = 365,
) -> pd.DataFrame:
    """For each rebalance date in rec_df, compute the optimizer's
    forward-looking expected_annual_return alongside the realized
    annualized return over the next ``window_days`` days from
    snap_df.total_value. Realized is NaN where there isn't enough
    forward data (most recent rebalances).

    Returns a DataFrame with columns ``date``, ``expected``,
    ``realized`` sorted by date.
    """
    if rec_df.empty or snap_df.empty:
        return pd.DataFrame(columns=["date", "expected", "realized"])

    expected = rec_df.groupby("date")["expected_return"].first().sort_index()
    totals = snap_df.groupby("date")["total_value"].first().sort_index()
    totals.index = pd.to_datetime(totals.index)

    rows: list[dict[str, Any]] = []
    for d_str, exp in expected.items():
        d = pd.Timestamp(d_str)
        # Find the snapshot at or just after the rebalance.
        valid_start = totals.index[totals.index >= d]
        if len(valid_start) == 0:
            continue
        d_start = valid_start[0]
        v_start = float(totals.loc[d_start])
        if v_start <= 0:
            continue
        # Find the snapshot at or just after the rebalance + window.
        d_end_target = d + pd.Timedelta(days=window_days)
        valid_end = totals.index[totals.index >= d_end_target]
        if len(valid_end) == 0:
            realized: float | None = None
        else:
            d_end = valid_end[0]
            v_end = float(totals.loc[d_end])
            actual_days = max(1, (d_end - d_start).days)
            realized = (v_end / v_start) ** (365.0 / actual_days) - 1.0
        rows.append({"date": d, "expected": float(exp), "realized": realized})

    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def build_dashboard(
    snapshots_path: str = "data/snapshots.csv",
    recommendations_path: str = "data/recommendations.csv",
    out_path: str = "docs/index.html",
    benchmarks: list[str] | None = None,
    thesis_baseline_path: str | None = "data/thesis_baseline.json",
) -> dict[str, Any]:
    """Render the time-series + bar charts into one HTML file.

    If ``benchmarks`` is provided (or defaulted to ``["SPY"]``), each
    benchmark ticker's price curve is fetched via yfinance for the
    snapshot date range and overlaid on the portfolio-value chart,
    normalized so that benchmark and portfolio share a starting value.
    Pass an empty list to suppress benchmark overlays."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    snap_path = Path(snapshots_path)
    rec_path = Path(recommendations_path)
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

    # Row layout.
    R_PORTFOLIO       = 1
    R_TURNOVER        = 2
    R_REC_WAVE        = 3
    R_LATEST_WEIGHTS  = 4
    R_TRADE_TABLE     = 5 if is_live else None
    R_ACTUAL_WEIGHTS  = 6 if is_live else None
    R_GAIN_INIT       = 7 if is_live else 5
    R_GAIN_REVIEW     = 8 if is_live else None
    _after_gain       = 9 if is_live else 6
    R_ASSET_USD       = _after_gain
    R_WAVE_USD        = _after_gain + 1
    R_EXP_VS_REAL     = R_WAVE_USD + 1
    n_rows            = R_EXP_VS_REAL

    _chart5_anchor = "/initialize-portfolio executed" if is_live else "backtest start"
    _chart5_tail = (
        "Bars sum to total realized portfolio gain since the thesis was set. Green = winners, red = losers."
        if is_live else
        "Bars sum to total realized portfolio gain over the backtest window. Green = winners, red = losers."
    )

    # Build the title list in row order, numbering as we go.
    titles_list: list[str] = []
    titles_list.append(
        f"{R_PORTFOLIO}. Portfolio value over time"
        "<br><sub><i>Σ(actual shares × close price) per day.</i></sub>"
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
    if R_TRADE_TABLE is not None:
        titles_list.append(
            f"{R_TRADE_TABLE}. Trades to move from actual to recommended"
            "<br><sub><i>Per-ticker buys and sells needed to rebalance from today's actual portfolio (chart 6 below) to the latest recommendation (chart 4 above).</i></sub>"
        )
    if R_ACTUAL_WEIGHTS is not None:
        titles_list.append(
            f"{R_ACTUAL_WEIGHTS}. Today's actual portfolio %"
            "<br><sub><i>Per-ticker share of total portfolio value from today's snapshot. Compare against chart 4 above to see how far the actual portfolio sits from the latest recommendation; the gap is recommendations you haven't acted on yet.</i></sub>"
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
        f"{R_ASSET_USD}. Actual portfolio $ by asset class over time"
        "<br><sub><i>Your real holdings (from holdings.csv × close prices), grouped by asset class. Sums to total portfolio value (chart 1). Log y-axis keeps small allocations visible.</i></sub>"
    )
    titles_list.append(
        f"{R_WAVE_USD}. Actual portfolio $ by wave over time"
        "<br><sub><i>Your real holdings (from holdings.csv × close prices), grouped by wave. This is what you own today — not the optimizer's recommendation. Log y-axis.</i></sub>"
    )
    titles_list.append(
        f"{R_EXP_VS_REAL}. Expected vs realized annualized return per rebalance"
        "<br><sub><i>At each rebalance, the optimizer's forward-looking expected annual return (μᵀw) versus the actual annualized return realized over the next 365 days (computed from total_value in snapshots.csv). Divergence is prediction error: expected high but realized low means the optimizer was over-confident on noisy μ estimates; expected low but realized high means it was too risk-averse for the regime. Recent rebalances show expected only — the 1-year forward window hasn't elapsed yet.</i></sub>"
    )
    titles_all = tuple(titles_list)

    # Pre-compute the trade list so the table row can be sized to fit
    # all rows up front (plotly Tables don't expand within their subplot
    # domain — extra rows get scroll-hidden if the domain is too small).
    # Returns a list of (ticker, action, shares, $, cur_shares, target_shares)
    # tuples plus running totals; empty list when there's no data.
    trade_rows: list[tuple] = []
    trade_total_buy = 0.0
    trade_total_sell = 0.0
    trade_total_value = 0.0
    if R_TRADE_TABLE is not None and snap_path.exists() and rec_path.exists():
        try:
            _snaps_tt = pd.read_csv(snap_path, parse_dates=["date"])
            _recs_tt = pd.read_csv(rec_path, parse_dates=["date"])
            _snap_latest_tt = _snaps_tt[_snaps_tt["date"] == _snaps_tt["date"].max()].copy()
            _rec_latest_tt = _recs_tt[_recs_tt["date"] == _recs_tt["date"].max()].copy()
            trade_total_value = float(_snap_latest_tt["value"].sum())
            if trade_total_value > 0:
                _price_by = dict(zip(_snap_latest_tt["ticker"], _snap_latest_tt["price"]))
                _shares_by = dict(zip(_snap_latest_tt["ticker"], _snap_latest_tt["shares"]))
                _target_w = dict(zip(_rec_latest_tt["ticker"], _rec_latest_tt["weight"]))
                for tk in sorted(set(_snap_latest_tt["ticker"]) | set(_rec_latest_tt["ticker"])):
                    cur_shares = float(_shares_by.get(tk, 0.0))
                    price = float(_price_by.get(tk, float("nan")))
                    if price != price or price <= 0:
                        continue
                    target_dollars = trade_total_value * float(_target_w.get(tk, 0.0))
                    cur_dollars = cur_shares * price
                    delta_dollars = target_dollars - cur_dollars
                    if abs(delta_dollars) < 1.0:
                        continue
                    target_shares = target_dollars / price
                    delta_shares = target_shares - cur_shares
                    action = "BUY" if delta_dollars > 0 else "SELL"
                    if action == "BUY":
                        trade_total_buy += delta_dollars
                    else:
                        trade_total_sell += -delta_dollars
                    trade_rows.append((tk, action, abs(delta_shares), abs(delta_dollars),
                                       cur_shares, target_shares))
                trade_rows.sort(key=lambda r: -r[3])  # biggest $ moves first
        except Exception:
            trade_rows = []

    # Subplot specs: every row is an xy chart except the trade-table row
    # (live dashboard only), which uses Plotly's table trace type. The
    # table row's relative height is sized to fit header + cells with a
    # small buffer; chart rows weight 1.0 = 340px (see fig.update_layout
    # below). Plotly Tables don't scroll within a too-small subplot
    # domain — they just truncate — so the table row must be sized up
    # front to fit every trade row.
    _specs = [[{"type": "xy"}] for _ in range(n_rows)]
    _row_h = [1.0] * n_rows
    if R_TRADE_TABLE is not None:
        _specs[R_TRADE_TABLE - 1] = [{"type": "table"}]
        # Plotly Tables truncate (no internal scroll) when their subplot
        # domain isn't tall enough. The subplot title and vertical
        # spacing eat 150-180px before the table itself gets any space,
        # so the buffer has to be generous. Width units are 340px per
        # chart row (see fig.update_layout height below).
        _table_px = 32 + max(1, len(trade_rows)) * 34 + 180
        _row_h[R_TRADE_TABLE - 1] = max(0.6, _table_px / 340.0)
    fig = make_subplots(
        rows=n_rows, cols=1,
        subplot_titles=titles_all,
        vertical_spacing=0.06,
        specs=_specs,
        row_heights=_row_h,
    )

    # Compute a shared x-axis range from the daily-cadence data
    # (snapshots.csv min/max) and pad each end by a fixed fraction so
    # data points don't sit flush against the axis edges. Applied to
    # every time-series subplot on both the live and backtest dashboards
    # so the charts align visually. No hardcoded dates: the range rolls
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
        # Constant-rate reference curves: dotted lines showing what the
        # thesis baseline portfolio would be worth at 5% / 10% / 15%
        # per week from day zero. Live dashboard only — anchored at the
        # thesis-baseline date.
        if is_live and len(snaps) > 0:
            anchor_date = snaps["date"].min()
            anchor_value = float(totals.iloc[0])
            ref_dates = pd.to_datetime(totals.index)
            ref_shades = {0.05: "#cccccc", 0.10: "#888888", 0.15: "#444444"}
            for rate, color in ref_shades.items():
                days = (ref_dates - anchor_date).days
                ref_vals = anchor_value * (1 + rate) ** (days / 7.0)
                fig.add_trace(
                    go.Scatter(x=ref_dates, y=ref_vals, mode="lines",
                               name=f"{int(rate * 100)}%/wk",
                               line={"width": 1, "color": color, "dash": "dot"},
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
        wv_order = [w for w in _WAVE_DISPLAY_ORDER if w in wv_weight.columns]
        # Stacked bar chart: one vertical bar per rebalance date, each
        # bar's height = 100%, partitioned into wave-colored segments.
        # Reads as a portfolio-composition timeline: how the optimizer
        # allocated across the wave buckets at each monthly rebalance.
        for wave in wv_order:
            fig.add_trace(
                go.Bar(x=wv_weight.index, y=wv_weight[wave],
                       name=WAVE_DISPLAY_LABEL.get(wave, wave),
                       legend="legend5",
                       marker_color=WAVE_COLORS.get(wave),
                       hovertemplate=f"{wave}<br>%{{x|%Y-%m-%d}}"
                                     "<br>%{y:.2%}<extra></extra>"),
                row=R_REC_WAVE, col=1,
            )
        # Force stacking on the chart-4 y-axis. barmode is figure-wide
        # but we only have one set of bar traces in a stack here.
        fig.update_layout(barmode="stack")
        latest_date = recs["date"].max()
        latest_weights = recs[recs["date"] == latest_date].sort_values("weight", ascending=False)

    # _ticker_label is defined at module scope and reused by chart 3 of
    # the live dashboard, chart 4 (gain bars), and the curator backtest's
    # gain-per-holding chart.

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
        # Group tickers by wave and emit one Bar trace per wave so the
        # legend matches chart 4 (wave colors and labels), not a
        # per-ticker spaghetti. Categorical x-axis order is set
        # explicitly so the bars still read in weight-descending order.
        latest_with_wave = latest_weights.copy()
        latest_with_wave["wave_bucket"] = latest_with_wave["ticker"].map(
            lambda t: TICKER_WAVE.get(t, "general_markets")
        )
        fig.update_xaxes(categoryorder="array", categoryarray=tickers_in_chart,
                         row=R_LATEST_WEIGHTS, col=1)
        waves_in_chart = [w for w in _WAVE_DISPLAY_ORDER
                          if w in latest_with_wave["wave_bucket"].values]
        for wave in waves_in_chart:
            sub = latest_with_wave[latest_with_wave["wave_bucket"] == wave]
            fig.add_trace(
                go.Bar(x=sub["ticker"], y=sub["weight"],
                       name=WAVE_DISPLAY_LABEL.get(wave, wave),
                       marker_color=WAVE_COLORS.get(wave),
                       legend="legend7",
                       hovertemplate=f"%{{x}}<br>{wave}<br>%{{y:.2%}}<extra></extra>"),
                row=R_LATEST_WEIGHTS, col=1,
            )
        # Concentration cap reference line, drawn but no longer in the legend.
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
                       mode="lines",
                       line={"color": "#d62728", "width": 1.5, "dash": "dot"},
                       hoverinfo="skip", showlegend=False),
            row=R_LATEST_WEIGHTS, col=1,
        )
        fig.update_xaxes(
            tickmode="array",
            tickvals=tickers_in_chart,
            ticktext=ticktext_3,
            tickangle=0,
            row=R_LATEST_WEIGHTS, col=1,
        )

    # 5. Trade table — per-ticker BUY/SELL needed to rebalance from
    # today's actual (chart 6) to the latest recommendation (chart 4).
    # Uses Plotly's table trace type so the table lives inside the
    # subplot grid between the recommended-weights and actual-weights
    # bar charts. Trade data was computed before make_subplots so the
    # subplot row could be sized to fit every row.
    if R_TRADE_TABLE is not None and trade_rows:
        tickers_col = [r[0] for r in trade_rows]
        action_col = [r[1] for r in trade_rows]
        shares_col = [f"{r[2]:,.2f}" for r in trade_rows]
        dollars_col = [f"${r[3]:,.0f}" for r in trade_rows]
        transition_col = [f"{r[4]:,.2f} → {r[5]:,.2f}" for r in trade_rows]
        action_colors = ["#15803d" if a == "BUY" else "#b91c1c"
                         for a in action_col]
        fig.add_trace(
            go.Table(
                columnwidth=[1, 1, 1.4, 1.4, 2.4],
                header=dict(
                    values=["<b>Ticker</b>", "<b>Action</b>",
                            "<b>Shares</b>", "<b>$ amount</b>",
                            "<b>Shares: current → target</b>"],
                    fill_color="#f3f4f6",
                    align=["left", "left", "right", "right", "right"],
                    font=dict(size=12, color="#222"),
                    height=32,
                ),
                cells=dict(
                    values=[tickers_col, action_col, shares_col,
                            dollars_col, transition_col],
                    align=["left", "left", "right", "right", "right"],
                    fill_color="white",
                    font=dict(
                        color=["#222", action_colors, "#222", "#222", "#888"],
                        size=12,
                    ),
                    height=34,
                ),
            ),
            row=R_TRADE_TABLE, col=1,
        )

    # 6. Today's actual portfolio % — bar chart of value / total_value
    # per ticker from the latest snapshot. Mirrors chart 4's per-wave
    # coloring so the reader can compare recommendation against reality
    # at a glance; the gap is recommendations the user has not yet acted
    # on. Live dashboard only — for the backtest dashboard, "actual" and
    # "recommended" are the same series.
    if R_ACTUAL_WEIGHTS is not None and snap_path.exists():
        _snaps_now = pd.read_csv(snap_path, parse_dates=["date"])
        _latest_date_now = _snaps_now["date"].max()
        _latest_now = _snaps_now[_snaps_now["date"] == _latest_date_now].copy()
        _total_now = float(_latest_now["value"].sum())
        if _total_now > 0 and not _latest_now.empty:
            _latest_now["weight"] = _latest_now["value"] / _total_now
            _latest_now = _latest_now.sort_values("weight", ascending=False)
            _latest_now["wave_bucket"] = _latest_now["ticker"].map(
                lambda t: TICKER_WAVE.get(t, "general_markets")
            )
            _tickers_in_chart5 = _latest_now["ticker"].tolist()
            _ticktext_5 = [_ticker_label(t) for t in _tickers_in_chart5]
            fig.update_xaxes(categoryorder="array", categoryarray=_tickers_in_chart5,
                             row=R_ACTUAL_WEIGHTS, col=1)
            _waves_in_chart5 = [w for w in _WAVE_DISPLAY_ORDER
                                if w in _latest_now["wave_bucket"].values]
            for wave in _waves_in_chart5:
                sub = _latest_now[_latest_now["wave_bucket"] == wave]
                fig.add_trace(
                    go.Bar(x=sub["ticker"], y=sub["weight"],
                           name=WAVE_DISPLAY_LABEL.get(wave, wave),
                           marker_color=WAVE_COLORS.get(wave),
                           showlegend=False,
                           hovertemplate=f"%{{x}}<br>{wave}<br>%{{y:.2%}}<extra></extra>"),
                    row=R_ACTUAL_WEIGHTS, col=1,
                )
            fig.update_xaxes(
                tickmode="array",
                tickvals=_tickers_in_chart5,
                ticktext=_ticktext_5,
                tickangle=0,
                row=R_ACTUAL_WEIGHTS, col=1,
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

        # Asset-class chart. Stacked area on a linear y-axis: top edge
        # of the stack equals total portfolio value over time; each
        # band's thickness is that bucket's $ contribution.
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
                go.Scatter(x=ac.index, y=ac[bucket], mode="lines",
                           name=bucket, legend="legend2",
                           stackgroup="asset",
                           line={"color": ac_colors.get(bucket, "#444"), "width": 0.5},
                           hovertemplate=f"{bucket}<br>%{{x|%Y-%m-%d}}"
                                         "<br>$%{y:,.0f}<extra></extra>"),
                row=R_ASSET_USD, col=1,
            )

        # Wave chart. Same shape (stacked area, linear y-axis). Tickers
        # in cash/bonds/precious-metals/crypto buckets stack into a
        # separate "cashlike" band so general_markets shows only
        # defensive equities (SPY/VIG/DVY/XLU/XLP), not ballast.
        is_cashlike = snaps_full["asset_bucket"].isin(
            ["bonds", "cash", "precious metals", "crypto"]
        )
        snaps_full["display_bucket"] = snaps_full["wave_bucket"].mask(is_cashlike, "cashlike")
        wv = snaps_full.groupby(["date", "display_bucket"])["value"].sum().unstack(fill_value=0)
        wv_order = [w for w in _WAVE_DISPLAY_ORDER if w in wv.columns]
        for wave in wv_order:
            if (wv[wave] <= 0).all():
                continue
            fig.add_trace(
                go.Scatter(x=wv.index, y=wv[wave], mode="lines",
                           name=WAVE_DISPLAY_LABEL.get(wave, wave),
                           legend="legend3",
                           stackgroup="wave",
                           line={"color": WAVE_COLORS.get(wave), "width": 0.5},
                           hovertemplate=f"{WAVE_DISPLAY_LABEL.get(wave, wave)}"
                                         "<br>%{x|%Y-%m-%d}"
                                         "<br>$%{y:,.0f}<extra></extra>"),
                row=R_WAVE_USD, col=1,
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

    # Expected vs realized annualized return per rebalance. Two lines
    # over time. Recent rebalances drop the realized line where the
    # 1-year forward window isn't complete yet — Plotly draws NaN as
    # a gap, so the realized series naturally cuts off at the right.
    if snap_path.exists() and rec_path.exists():
        try:
            _rec_evr = pd.read_csv(rec_path)
            _snap_evr = pd.read_csv(snap_path)
            evr = _compute_expected_vs_realized(_rec_evr, _snap_evr, window_days=365)
        except (OSError, pd.errors.EmptyDataError):
            evr = pd.DataFrame()
        if not evr.empty:
            fig.add_trace(
                go.Scatter(
                    x=evr["date"], y=evr["expected"],
                    name="Expected (optimizer μᵀw)",
                    mode="lines+markers", legend="legend8",
                    line={"color": "#3b82f6", "width": 2},
                    hovertemplate="%{x|%Y-%m-%d}<br>expected %{y:.1%}<extra></extra>",
                ),
                row=R_EXP_VS_REAL, col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=evr["date"], y=evr["realized"],
                    name="Realized (1y forward)",
                    mode="lines+markers", legend="legend8",
                    line={"color": "#d97706", "width": 2},
                    hovertemplate="%{x|%Y-%m-%d}<br>realized %{y:.1%}<extra></extra>",
                ),
                row=R_EXP_VS_REAL, col=1,
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
            yref="paper", y=_row_top(R_PORTFOLIO), yanchor="top",
        ),
        legend5=dict(
            title_text="Portfolio % by wave",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(R_REC_WAVE), yanchor="top",
        ),
        # Latest-recommended-weights chart: wave-colored bars, same legend
        # title as chart 4 so the reader sees the parallel.
        legend7=dict(
            title_text="Wave (latest weights)",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(R_LATEST_WEIGHTS), yanchor="top",
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
        legend8=dict(
            title_text="Expected vs realized",
            xref="paper", x=1.02,
            yref="paper", y=_row_top(R_EXP_VS_REAL), yanchor="top",
        ),
    )
    fig.update_yaxes(title_text="$", row=R_PORTFOLIO, col=1)
    fig.update_yaxes(title_text="portfolio %", row=R_REC_WAVE, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="portfolio %", row=R_LATEST_WEIGHTS, col=1, tickformat=".0%")
    if R_ACTUAL_WEIGHTS is not None:
        fig.update_yaxes(title_text="portfolio %", row=R_ACTUAL_WEIGHTS, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="$ gain", row=R_GAIN_INIT, col=1, zeroline=True,
                     zerolinewidth=1, zerolinecolor="#888")
    if R_GAIN_REVIEW is not None:
        fig.update_yaxes(title_text="$ gain", row=R_GAIN_REVIEW, col=1, zeroline=True,
                         zerolinewidth=1, zerolinecolor="#888")
    fig.update_yaxes(title_text="$", row=R_ASSET_USD, col=1, tickformat="$,.0f")
    fig.update_yaxes(title_text="$", row=R_WAVE_USD, col=1, tickformat="$,.0f")
    fig.update_yaxes(title_text="turnover (%)", row=R_TURNOVER, col=1, rangemode="tozero")
    fig.update_yaxes(title_text="annualized return", row=R_EXP_VS_REAL, col=1,
                     tickformat=".0%", zeroline=True,
                     zerolinewidth=1, zerolinecolor="#888")

    # Apply the padded snapshots-derived range to every time-series
    # subplot so data points don't sit flush against the axis edges
    # and all time-series charts share the same visual window. Charts 3
    # (latest weights) and 4 (gain bars) are bar charts with categorical
    # x-axes so the range setter is a no-op there.
    if xrange is not None:
        xrange_rows = [R_PORTFOLIO, R_TURNOVER, R_REC_WAVE,
                       R_ASSET_USD, R_WAVE_USD, R_EXP_VS_REAL]
        for r in xrange_rows:
            fig.update_xaxes(range=list(xrange), row=r, col=1)

    o_path = Path(out_path)
    o_path.parent.mkdir(parents=True, exist_ok=True)
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    # Append a small table showing the live curator's add/remove history
    # since the thesis baseline date (the user's own /review-portfolio
    # decisions, not the backtest replay).
    live_curation = ""
    live_history = Path("data/curation_history.csv")
    if is_live and live_history.exists():
        try:
            hist = pd.read_csv(live_history, parse_dates=["date"])
            # Scope to entries on or after the thesis baseline date so the
            # table tracks live decisions, not pre-thesis bootstrapping.
            cutoff = pd.Timestamp(json.loads(Path(thesis_baseline_path).read_text())["date"])
            hist = hist[hist["date"] >= cutoff].sort_values(["date", "action", "ticker"])
            tbl_rows = []
            for d, sub in hist.groupby("date"):
                adds = sub[sub["action"] == "add"]
                rems = sub[sub["action"] == "remove"]
                adds_s = ", ".join(
                    f"{r.ticker} <span style='color:#888;'>({r.wave_bucket})</span>"
                    for r in adds.itertuples()
                ) or "—"
                rems_s = ", ".join(r.ticker for r in rems.itertuples()) or "—"
                tbl_rows.append(
                    f"<tr><td style='padding:4px 12px;white-space:nowrap;'>{d.date()}</td>"
                    f"<td style='padding:4px 12px;'>{adds_s}</td>"
                    f"<td style='padding:4px 12px;'>{rems_s}</td></tr>"
                )
            if tbl_rows:
                live_curation = (
                    "<h2 style='margin-top:2em;'>Curation log</h2>"
                    "<p style='font-size:14px;color:#555;max-width:780px;'>"
                    f"Every add and remove the curator has applied since the "
                    f"thesis baseline date ({cutoff.date()}). Each row is one "
                    "<code>/review-portfolio</code> run that produced at least one "
                    "watchlist change.</p>"
                    "<table style='border-collapse:collapse;font-size:14px;'>"
                    "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
                    "<th style='padding:4px 12px;'>Date</th>"
                    "<th style='padding:4px 12px;'>Adds</th>"
                    "<th style='padding:4px 12px;'>Removes</th></tr></thead>"
                    f"<tbody>{''.join(tbl_rows)}</tbody></table>"
                )
        except Exception:
            pass  # silently skip the section if the file is malformed

    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Portfolio Wave Rider — live dashboard</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1280px;margin:0 auto;padding:1em 1.5em;color:#222;}'
        'th,td{border-bottom:1px solid #eee;}</style>'
        '</head><body>'
        + _nav_strip("index.html")
        + chart_html
        + live_curation +
        '</body></html>'
    )
    o_path.write_text(page, encoding="utf-8")

    return {
        "out_path": str(o_path),
        "snapshots_rows": int(len(pd.read_csv(snap_path))) if snap_path.exists() else 0,
        "recommendations_rows": int(len(pd.read_csv(rec_path))) if rec_path.exists() else 0,
        "benchmarks_overlaid": list(benchmark_curves.keys()),
    }




# ---------------------------------------------------------------------------
# Curator-backtest dashboard. Reads the curator_backtest output dir plus the
# runs dir, renders three baseline curves on one chart and a watchlist-
# composition timeline on a second, into a single static HTML file.
# ---------------------------------------------------------------------------

_STARTER_WAVE_DEFAULTS: dict[str, str] = {
    "AAPL": "AI", "MSFT": "AI", "GOOGL": "AI", "NVDA": "AI", "TSM": "AI",
    "SMH": "AI",
    "SPY": "general_markets", "AGG": "general_markets",
    "BIL": "general_markets", "IAU": "general_markets",
    "VIG": "general_markets",
}


def _build_ticker_periods(
    runs_dir: str, starter_tickers: list[str], end_date: pd.Timestamp,
) -> tuple[list[tuple[str, pd.Timestamp, pd.Timestamp, str]], pd.Timestamp]:
    """Reconstruct each ticker's on-watchlist period(s) from the runs dir.

    Returns a list of (ticker, start, end, wave_bucket) tuples, sorted by
    earliest start date, and the first add date across the run. A ticker
    re-added after a remove gets multiple entries in the list.
    """
    runs = Path(runs_dir)
    starter_json = runs / "_starter.json"
    if starter_json.exists():
        cfg = json.loads(starter_json.read_text())
        run_start = pd.Timestamp(cfg["start_date"])
    else:
        run_start = pd.Timestamp("1900-01-01")

    # Collect curation events in chronological order from the runs dir.
    files = sorted(runs.glob("*-curation.json"))
    open_periods: dict[str, tuple[pd.Timestamp, str]] = {}
    completed: list[tuple[str, pd.Timestamp, pd.Timestamp, str]] = []

    for t in starter_tickers:
        open_periods[t] = (run_start, _STARTER_WAVE_DEFAULTS.get(t, "general_markets"))

    for f in files:
        payload = json.loads(f.read_text())
        d = pd.Timestamp(payload.get("as_of_date") or f.stem.replace("-curation", ""))
        for a in (payload.get("adds") or []):
            if not isinstance(a, dict): continue
            tk = a.get("ticker")
            wb = a.get("wave_bucket") or "general_markets"
            if not tk or tk in open_periods:
                continue  # invalid or duplicate-of-open
            open_periods[tk] = (d, wb)
        for r in (payload.get("removes") or []):
            if not isinstance(r, dict): continue
            tk = r.get("ticker")
            if not tk or tk not in open_periods:
                continue
            start, wb = open_periods.pop(tk)
            completed.append((tk, start, d, wb))

    # Tickers still open at end of run get end_date as their close.
    for tk, (start, wb) in open_periods.items():
        completed.append((tk, start, end_date, wb))

    completed.sort(key=lambda x: (x[1], x[0]))
    return completed, run_start


def build_curator_dashboard(
    backtest_dir: str = "data/backtest_curator_5y",
    runs_dir: str = "data/curator_runs/5y-quarterly",
    out_path: str = "docs/backtest_curator.html",
    benchmarks: list[str] | None = None,
) -> dict[str, Any]:
    """Render a single static HTML dashboard for one curator-backtest run.

    Two main charts:
      1. Equity-curve race: curator strategy vs fixed-watchlist baseline
         vs buy-and-hold baseline vs benchmarks (default SPY).
      2. Watchlist composition over time: a Gantt-style timeline showing
         when each ticker entered and exited the watchlist, color-coded
         by wave bucket.

    Also includes a small summary table of curation events. No interactive
    backend - this is one static HTML file readable by any browser.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    bd = Path(backtest_dir)
    snaps_path = bd / "snapshots.csv"
    baselines_path = bd / "baselines_totals.csv"
    summary_path = bd / "curation_summary.json"
    if not snaps_path.exists() or not baselines_path.exists():
        raise FileNotFoundError(
            f"backtest dir missing required files: {snaps_path} or {baselines_path}"
        )

    snaps = pd.read_csv(snaps_path, parse_dates=["date"])
    baselines = pd.read_csv(baselines_path, parse_dates=["date"])
    totals = snaps.groupby("date")["total_value"].first().sort_index()
    start = totals.index[0]
    end = totals.index[-1]
    initial = float(totals.iloc[0])

    # Benchmark curves, normalized to the same starting value.
    if benchmarks is None:
        benchmarks = ["SPY"]
    bench_curves = _fetch_benchmark_curves(benchmarks, start, end, initial) if benchmarks else {}

    # Watchlist periods for the Gantt chart.
    starter_tickers: list[str] = []
    runs_starter = Path(runs_dir) / "_starter.json"
    if runs_starter.exists():
        starter_tickers = json.loads(runs_starter.read_text()).get("starter_watchlist", [])
    periods, _ = _build_ticker_periods(runs_dir, starter_tickers, end)

    # Realized return numbers for the headline summary.
    final = float(totals.iloc[-1])
    cur_return = (final / initial) - 1.0
    fix_initial = float(baselines["fixed_total"].dropna().iloc[0]) if "fixed_total" in baselines else initial
    fix_final = float(baselines["fixed_total"].dropna().iloc[-1]) if "fixed_total" in baselines else initial
    fix_return = (fix_final / fix_initial) - 1.0
    # Headline buy-and-hold curve = equal-weight starter held forever
    # (eq_total). bnh_total is the optimizer-day-0-weights ablation kept
    # only in the CSV for researchers.
    bnh_initial = float(baselines["eq_total"].dropna().iloc[0]) if "eq_total" in baselines else initial
    bnh_final = float(baselines["eq_total"].dropna().iloc[-1]) if "eq_total" in baselines else initial
    bnh_return = (bnh_final / bnh_initial) - 1.0

    fig = make_subplots(
        rows=6, cols=1, vertical_spacing=0.07,
        row_heights=[0.20, 0.26, 0.13, 0.12, 0.12, 0.17],
        subplot_titles=(
            "1. Realized portfolio value: curator vs baselines vs benchmark",
            "2. Watchlist composition over time (one row per ticker; color = wave bucket)",
            "3. Cumulative $ gain per holding over the 5y window",
            "4. Actual portfolio $ by asset class over time",
            "5. Actual portfolio $ by wave over time<br>"
            "<span style='font-size:0.8em;color:#666;font-weight:400;'>"
            "general_markets = defensive equity ETFs (broad-market / dividend / "
            "utilities / staples); cashlike = bonds + cash-equivalents + precious "
            "metals (e.g., AGG, BIL, IAU)"
            "</span>",
            "6. Expected vs realized annualized return per rebalance "
            "(divergence is optimizer prediction error)",
        ),
    )

    # Chart 1: equity-curve race.
    fig.add_trace(
        go.Scatter(x=totals.index, y=totals.values, name="Curator-driven",
                   mode="lines", line={"color": "#d97706", "width": 2.5}),
        row=1, col=1,
    )
    if "eq_total" in baselines.columns:
        eq = baselines.dropna(subset=["eq_total"])
        fig.add_trace(
            go.Scatter(x=eq["date"], y=eq["eq_total"],
                       name="Buy-and-hold (20% each)",
                       mode="lines", line={"color": "#3b82f6", "width": 1.8}),
            row=1, col=1,
        )
    for b, curve in bench_curves.items():
        fig.add_trace(
            go.Scatter(x=curve.index, y=curve.values, name=f"{b} benchmark",
                       mode="lines", line={"color": "#10b981", "width": 1.5, "dash": "dot"}),
            row=1, col=1,
        )
    fig.update_yaxes(title_text="portfolio value ($)", tickformat="$,.0f", row=1, col=1)

    # Chart 2: watchlist Gantt. One row per ticker, color = wave_bucket.
    # Sort tickers so the first-added is at the top, latest at the bottom.
    seen: list[str] = []
    for tk, _s, _e, _wb in periods:
        if tk not in seen: seen.append(tk)
    seen.reverse()  # so top of chart is first-added
    y_index = {tk: i for i, tk in enumerate(seen)}

    # Per-wave shade variation: each ticker within a wave bucket gets a
    # distinct lightness step of the wave's base color, so adjacent rows
    # in the Gantt that share a wave (e.g., 8 AI tickers) are visually
    # distinguishable while still grouping as the same hue family.
    import colorsys
    wave_tickers: dict[str, list[str]] = {}
    for tk, _, _, wb in periods:
        if tk not in wave_tickers.setdefault(wb, []):
            wave_tickers[wb].append(tk)

    def _shade(base_hex: str, idx: int, n: int) -> str:
        h = base_hex.lstrip("#")
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
        hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
        # Spread lightness across [0.30, 0.70] so all variants stay legible.
        new_l = ll if n <= 1 else 0.30 + (idx / (n - 1)) * 0.40
        nr, ng, nb = colorsys.hls_to_rgb(hh, new_l, ss)
        return f"#{int(nr*255):02x}{int(ng*255):02x}{int(nb*255):02x}"

    legend_seen: set[str] = set()
    for tk, p_start, p_end, wb in periods:
        members = wave_tickers[wb]
        idx = members.index(tk)
        color = _shade(WAVE_COLORS.get(wb, "#888888"), idx, len(members))
        show_legend = wb not in legend_seen
        legend_seen.add(wb)
        fig.add_trace(
            go.Scatter(
                x=[p_start, p_end], y=[y_index[tk], y_index[tk]],
                mode="lines",
                line={"color": color, "width": 14},
                name=wb, legendgroup=wb, showlegend=show_legend,
                legend="legend5",
                hovertemplate=f"<b>{tk}</b><br>{wb}<br>"
                              f"%{{x|%Y-%m-%d}}<extra></extra>",
            ),
            row=2, col=1,
        )

    fig.update_yaxes(
        tickmode="array", tickvals=list(range(len(seen))), ticktext=seen,
        autorange="reversed", row=2, col=1,
    )
    fig.update_xaxes(range=[start, end], row=2, col=1)

    # Chart 3: cumulative $ gain per ticker over the 5y window. Daily
    # P&L = prior_day_shares × price_change, summed across the window.
    # Mirrors the live dashboard's chart 5 attribution. Tickers ordered
    # by gain descending, colored green (positive) or red (negative).
    snaps_sorted = snaps.sort_values(["ticker", "date"])
    _gain_by_ticker: dict[str, float] = {}
    for _tk, _sub in snaps_sorted.groupby("ticker"):
        _sub = _sub.sort_values("date").reset_index(drop=True)
        _pc = _sub["price"].diff()
        _ps = _sub["shares"].shift(1)
        _gain_by_ticker[_tk] = float((_ps * _pc).fillna(0.0).sum())
    _gain_items = sorted(_gain_by_ticker.items(), key=lambda kv: kv[1], reverse=True)
    _gain_tickers = [t for t, _ in _gain_items]
    _gain_values = [v for _, v in _gain_items]
    _bar_colors = ["#2ca02c" if v >= 0 else "#d62728" for v in _gain_values]
    fig.add_trace(
        go.Bar(x=_gain_tickers, y=_gain_values, marker_color=_bar_colors,
               name="$ gain", showlegend=False,
               hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>"),
        row=3, col=1,
    )
    fig.update_xaxes(
        tickmode="array",
        tickvals=_gain_tickers,
        ticktext=[_ticker_label(t) for t in _gain_tickers],
        tickangle=0,
        row=3, col=1,
    )
    fig.update_yaxes(title_text="$ gain", tickformat="$,.0f",
                     zeroline=True, zerolinewidth=1, zerolinecolor="#888",
                     row=3, col=1)

    # Charts 4 and 5: actual portfolio $ by asset class and by wave over
    # time. Stacked area on linear y-axis: top edge = total portfolio
    # value; each band's thickness = that bucket's $ contribution.
    snaps_full = snaps.copy()
    snaps_full["asset_bucket"] = snaps_full["ticker"].map(
        lambda t: ASSET_CLASS_BUCKET.get(TICKER_ASSET_CLASS.get(t, "equity"), "equities")
    )
    snaps_full["wave_bucket"] = snaps_full["ticker"].map(
        lambda t: TICKER_WAVE.get(t, "general_markets")
    )
    ac_colors = {
        "equities":        "#1f77b4",
        "bonds":           "#9467bd",
        "cash":            "#7f7f7f",
        "precious metals": "#bcbd22",
        "crypto":          "#17becf",
    }
    ac = snaps_full.groupby(["date", "asset_bucket"])["value"].sum().unstack(fill_value=0)
    ac_order = [c for c in ["equities", "bonds", "cash", "precious metals", "crypto"]
                if c in ac.columns]
    for bucket in ac_order:
        fig.add_trace(
            go.Scatter(x=ac.index, y=ac[bucket], mode="lines",
                       name=bucket, legend="legend3",
                       stackgroup="asset",
                       line={"color": ac_colors.get(bucket, "#444"), "width": 0.5},
                       hovertemplate=f"{bucket}<br>%{{x|%Y-%m-%d}}"
                                     "<br>$%{y:,.0f}<extra></extra>"),
            row=4, col=1,
        )
    # Split cash/bonds/precious-metals/crypto out of general_markets into
    # a separate "cashlike" band so general_markets shows only defensive
    # equities (SPY/VIG/DVY/XLU/XLP), not ballast.
    is_cashlike = snaps_full["asset_bucket"].isin(
        ["bonds", "cash", "precious metals", "crypto"]
    )
    snaps_full["display_bucket"] = snaps_full["wave_bucket"].mask(is_cashlike, "cashlike")
    wv = snaps_full.groupby(["date", "display_bucket"])["value"].sum().unstack(fill_value=0)
    wv_order = [w for w in _WAVE_DISPLAY_ORDER if w in wv.columns]
    for wave in wv_order:
        if (wv[wave] <= 0).all():
            continue
        fig.add_trace(
            go.Scatter(x=wv.index, y=wv[wave], mode="lines",
                       name=WAVE_DISPLAY_LABEL.get(wave, wave),
                       legend="legend4",
                       stackgroup="wave",
                       line={"color": WAVE_COLORS.get(wave), "width": 0.5},
                       hovertemplate=f"{WAVE_DISPLAY_LABEL.get(wave, wave)}"
                                     "<br>%{x|%Y-%m-%d}"
                                     "<br>$%{y:,.0f}<extra></extra>"),
            row=5, col=1,
        )
    fig.update_yaxes(title_text="$", tickformat="$,.0f", row=4, col=1)
    fig.update_yaxes(title_text="$", tickformat="$,.0f", row=5, col=1)
    fig.update_xaxes(range=[start, end], row=4, col=1)
    fig.update_xaxes(range=[start, end], row=5, col=1)

    # Chart 6: expected vs realized annualized return per rebalance.
    # Reads recommendations.csv (per-rebalance expected_return) plus
    # snapshots.csv total_value; realized = forward-1y annualized return
    # from each rebalance date. The last few rebalances will have NaN
    # realized (1y forward window not complete within the backtest window).
    rec_path = bd / "recommendations.csv"
    if rec_path.exists() and snaps_path.exists():
        try:
            _rec = pd.read_csv(rec_path)
            _snap = pd.read_csv(snaps_path)
            evr = _compute_expected_vs_realized(_rec, _snap, window_days=365)
        except (OSError, pd.errors.EmptyDataError):
            evr = pd.DataFrame()
        if not evr.empty:
            fig.add_trace(
                go.Scatter(x=evr["date"], y=evr["expected"],
                           name="Expected (optimizer μᵀw)",
                           mode="lines+markers", legend="legend2",
                           line={"color": "#3b82f6", "width": 2},
                           hovertemplate="%{x|%Y-%m-%d}<br>expected %{y:.1%}<extra></extra>"),
                row=6, col=1,
            )
            fig.add_trace(
                go.Scatter(x=evr["date"], y=evr["realized"],
                           name="Realized (1y forward)",
                           mode="lines+markers", legend="legend2",
                           line={"color": "#d97706", "width": 2},
                           hovertemplate="%{x|%Y-%m-%d}<br>realized %{y:.1%}<extra></extra>"),
                row=6, col=1,
            )
            fig.update_yaxes(title_text="annualized return", tickformat=".0%",
                             zeroline=True, zerolinewidth=1, zerolinecolor="#888",
                             row=6, col=1)
            fig.update_xaxes(range=[start, end], row=6, col=1)

    fig.update_layout(
        height=2200, margin={"t": 90, "b": 60, "l": 80, "r": 30},
        title={
            "text": (
                f"<span style='font-size:14px;color:#555;'>"
                f"Curator-driven: {cur_return * 100:+.0f}%  "
                f"·  Buy/hold: {bnh_return * 100:+.0f}%  "
                f"·  (Curator - buy/hold): {(cur_return - bnh_return) * 100:+.0f}%  "
                f"·  (Curator - buy/hold)/(buy/hold): "
                f"{(cur_return - bnh_return) / bnh_return:.2f}"
                f"</span>"
            ),
            "x": 0.5, "xanchor": "center",
        },
        plot_bgcolor="#fafafa",
        # Per-row legends, each pinned to its chart's vertical position
        # in paper coords (1.0 = top, 0.0 = bottom).
        legend=dict(
            title_text="Portfolio value",
            xref="paper", x=1.02, yref="paper", y=0.98, yanchor="top",
        ),
        legend5=dict(
            title_text="Wave bucket",
            xref="paper", x=1.02,
            yref="paper", y=0.716, yanchor="middle",
        ),
        legend3=dict(
            title_text="Asset class",
            xref="paper", x=1.02,
            yref="paper", y=0.407, yanchor="top",
        ),
        legend4=dict(
            title_text="Wave bucket",
            xref="paper", x=1.02,
            yref="paper", y=0.259, yanchor="top",
        ),
        legend2=dict(
            title_text="Expected vs realized",
            xref="paper", x=1.02,
            yref="paper", y=0.111, yanchor="top",
        ),
    )

    # Curation event log table at the bottom.
    log_html = ""
    if summary_path.exists():
        log = json.loads(summary_path.read_text())
        rows = []
        for ev in log:
            d = ev.get("date", "")
            adds = ", ".join(ev.get("adds") or []) or "—"
            removes = ", ".join(ev.get("removes") or []) or "—"
            rej = ev.get("rejections", 0)
            rej_cell = str(rej) if rej else "—"
            rows.append(
                f"<tr><td>{_html.escape(d)}</td>"
                f"<td style='color:#0a7a3a;'>{_html.escape(adds)}</td>"
                f"<td style='color:#b91c1c;'>{_html.escape(removes)}</td>"
                f"<td>{_html.escape(rej_cell)}</td></tr>"
            )
        log_html = (
            "<h2 style='margin-top:2em;'>Curation log</h2>"
            "<p style='color:#555;'>Each row is one quarterly curator call. "
            "The <em>Rejections</em> column counts adds and removes the validator "
            "dropped as invalid (see "
            "<a href='https://github.com/joehahn/portfolio-wave-rider/blob/main/REFERENCE.md#cli-reference'>"
            "REFERENCE.md</a>).</p>"
            "<table style='border-collapse:collapse;width:100%;font-size:14px;'>"
            "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
            "<th style='padding:6px;'>Date</th>"
            "<th style='padding:6px;'>Adds</th>"
            "<th style='padding:6px;'>Removes</th>"
            "<th style='padding:6px;'>Rejections</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Portfolio Wave Rider — curator backtest</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5;}'
        'h1,h2{color:#111;}'
        'table{margin-top:0.5em;}'
        'th,td{border-bottom:1px solid #eee;}'
        '</style></head><body>'
        + _nav_strip("backtest_curator.html") +
        f'<h1>Curator-driven backtest '
        f'<span style="font-size:0.55em;color:#666;font-weight:400;">'
        f'— {start.date()} to {end.date()}</span></h1>'
        '<p style="color:#555;max-width:780px;">The watchlist-curator agent was called quarterly over a 5 year historical window. '
        'At each rebalance it read the news of the preceding quarter and proposed '
        'adds and removes against the active watchlist; the optimizer then ran '
        'mean-variance on the revised watchlist. The buy-and-hold curve below is '
        'the value of the initial portfolio (which never gets rebalanced or '
        'optimized) over time. The buy-and-hold portfolio has equal amounts of '
        '<code>[AAPL, MSFT, GOOGL, NVDA, SPY]</code> and is held without any '
        'rebalancing across the 5 year window.</p>'
        + chart_html
        + log_html
        + '</body></html>'
    )
    o = Path(out_path)
    o.parent.mkdir(parents=True, exist_ok=True)
    o.write_text(page, encoding="utf-8")
    return {
        "out_path": str(o),
        "n_tickers_ever_held": len(seen),
        "curator_return": round(cur_return, 4),
        "fixed_baseline_return": round(fix_return, 4),
        "bnh_baseline_return": round(bnh_return, 4),
        "benchmarks": {b: float(c.iloc[-1] / c.iloc[0] - 1.0) for b, c in bench_curves.items()},
    }
