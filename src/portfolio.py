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
import re
from datetime import datetime
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
    # optimizer_lookback_days / news_lookback_days are the day-denominated
    # successors to the fractional-year ``lookback_period`` string. When
    # optimizer_lookback_days is set in the profile it is the source of truth for
    # the μ/Σ estimation window; load_financial_model derives the legacy
    # ``lookback_period`` string from it so older readers keep working.
    # news_lookback_days is the trailing news window the curator reads each
    # rebalance (typically == the rebalance cadence in days).
    "optimizer_lookback_days": None,
    "news_lookback_days": None,
    "rebalance_period": "monthly",
    "max_watchlist_size": 12,
    # concentration_cap is the optimizer's per-position max weight (the
    # --max-weight default). It lives INSIDE the financial_model block (with the
    # other optimizer knobs); load_financial_model still honors a legacy
    # top-level key as a fallback for older profiles.
    "concentration_cap": 0.25,
    # min_trade_size_frac is the smallest trade the optimizer will propose,
    # expressed as a FRACTION of current portfolio value (0.1 = 10%); proposed
    # trades below frac * portfolio_value are filtered out of the recommendation.
    # It lives at the profile's top level, not inside the financial_model block.
    "min_trade_size_frac": 0.1,
    # always_include: tickers permanently injected into the optimizer universe
    # as safe-haven / diversification anchors (e.g. SPY, AGG, IAU). They live
    # as shares=0 rows in holdings.csv but are OUTSIDE the curator's
    # max_watchlist_size budget: the curator never manages them, cannot remove
    # them, and they do not count toward the cap. Top-level profile key.
    "always_include": [],
    # initial_investment_usd: total dollars allocated on day 0 (top-level profile key).
    "initial_investment_usd": 50000.0,
    # starter_watchlist: the inception holdings, equal-weight (top-level profile key). Managed names
    # only; always_include anchors come on top and are outside max_watchlist_size.
    "starter_watchlist": [],
}


def load_financial_model(profile_path: str = "investor_profile.md") -> dict[str, Any]:
    """Read `financial_model` from investor_profile.md's YAML front matter.

    Returns a dict with the `financial_model` fields (`risk_aversion`,
    `risk_free_rate`, `lookback_period`, `rebalance_period`,
    `max_watchlist_size`, `concentration_cap`) plus the top-level keys
    `min_trade_size_frac` and `always_include`; any missing field falls back
    to the hard-coded default. `concentration_cap` now lives inside
    `financial_model`; a legacy top-level key is still honored as a fallback.
    If the profile file doesn't exist or has no front matter, all defaults are
    returned.

    Backtest-only knobs (window dates, execution lag) live in a separate
    `backtest` section and are read by ``load_backtest_config`` instead, since
    they never affect the live analyze/recommend path.

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
    # concentration_cap is an optimizer constraint whose home is `financial_model` (picked up by the
    # out.update(fm) above). A legacy TOP-LEVEL concentration_cap is still honored for older profiles,
    # but only as a fallback — the financial_model value wins.
    if "concentration_cap" not in fm and "concentration_cap" in data:
        out["concentration_cap"] = data["concentration_cap"]
    # min_trade_size_frac is an optimizer/rebalancing knob (smallest proposed trade, as a fraction of
    # portfolio value) whose home is `financial_model` (picked up by out.update(fm) above). A legacy
    # TOP-LEVEL key is still honored as a fallback for older profiles, but the financial_model value wins.
    if "min_trade_size_frac" not in fm and "min_trade_size_frac" in data:
        out["min_trade_size_frac"] = data["min_trade_size_frac"]
    # always_include is also a top-level key; normalize to uppercase tickers.
    if "always_include" in data:
        out["always_include"] = [str(t).upper().strip()
                                 for t in (data["always_include"] or [])]
    # initial_investment_usd + starter_watchlist are top-level keys too (portfolio inception).
    if "initial_investment_usd" in data:
        out["initial_investment_usd"] = float(data["initial_investment_usd"])
    if "starter_watchlist" in data:
        out["starter_watchlist"] = [str(t).upper().strip() for t in (data["starter_watchlist"] or [])]
    # Back-compat: if the profile uses the day-denominated optimizer_lookback_days,
    # derive the legacy fractional-year ``lookback_period`` string from it so every
    # existing reader (CLI --period default, dashboard labels) keeps working off a
    # single source of truth.
    if out.get("optimizer_lookback_days"):
        out["lookback_period"] = f"{float(out['optimizer_lookback_days']) / 365.0:.4f}y"
    return out


_BACKTEST_DEFAULTS: dict[str, Any] = {
    # Window for /run-backtest and the parameter sweeps. None => fall back to
    # the run dir's _starter.json window (or a rolling default in the helper).
    "start_date": None,
    "end_date": None,
    # Trading-day lag from a rebalance signal (decided on the rebalance date's
    # close) to the trade actually landing. 1 = next session. Backtest-only.
    "t_update_days": 1,
    # max_articles: retrieval knob — max ranked articles fed to the curator per rebalance pool.
    # Backtest-only (the live/forward retriever sets its own per-query result count), so it lives in
    # the backtest section next to the other backtest-only knobs rather than in financial_model.
    "max_articles": 100,
    # Optional BACKTEST-ONLY optimizer overrides. None => use the live
    # financial_model / concentration_cap values. Set these to run the backtest
    # with different optimizer knobs than the live recommend path (e.g. a
    # candidate λ / lookback / cap), WITHOUT changing what the live optimizer
    # recommends with real money.
    "risk_aversion": None,
    "lookback_years": None,
    "concentration_cap": None,
    # Optional forward-test split. When set to a date, the backtest report and
    # dashboard split realized performance into an IN-sample segment (rebalances
    # on/before this date, where the LLM's training may already "know" the
    # outcomes) and an OUT-of-sample segment (rebalances strictly after it,
    # genuinely unknowable when decided) -- the honest overfitting check. None =>
    # no split shown. Reporting-only; never affects optimizer math or live recs.
    "forward_split_date": None,
    # LLM that drives the backtest curator (scripts/backtest_sdk.py). "claude-*" ids route to the
    # Anthropic API; anything with a "/" (e.g. "deepseek/deepseek-v4-flash") routes to OpenRouter. This
    # is a BACKTEST-only choice — the forward /review-portfolio curator runs as a Claude Code subagent on
    # the session model, so this key never touches live recommendations.
    "curator_model": "claude-sonnet-5",
}


def load_wave_thesis(profile_path: str = "investor_profile.md") -> str:
    """The wave thesis the curator reasons against: the profile's ``# Strategy & beliefs`` markdown section.
    FAIL LOUD -- raise if the profile or that section is missing, so the thesis always comes from the profile
    and is never silently replaced by a hardcoded default (which is how the thesis got ignored historically)."""
    p = Path(profile_path)
    if not p.exists():
        raise FileNotFoundError(f"{profile_path} not found; the curator's wave thesis must come from it")
    m = re.search(r"^#\s+Strategy\s*&\s*beliefs\s*$(.*?)(?=^#\s+\S|\Z)", p.read_text(),
                  re.MULTILINE | re.DOTALL)
    if not m or not m.group(1).strip():
        raise ValueError(f"{profile_path} has no '# Strategy & beliefs' section (the curator's wave thesis)")
    return m.group(1).strip()


def load_exclusions(profile_path: str = "investor_profile.md") -> str:
    """The profile's ``exclusions:`` YAML list, joined into the comma string the curator expects (``''`` if
    none). Fail loud only on a missing profile; an absent/empty exclusions list is allowed."""
    import yaml
    p = Path(profile_path)
    if not p.exists():
        raise FileNotFoundError(f"{profile_path} not found; exclusions must come from it")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", p.read_text(), re.DOTALL)
    data = (yaml.safe_load(m.group(1)) if m else {}) or {}
    return ", ".join(str(e).strip() for e in (data.get("exclusions") or []))


def load_backtest_config(profile_path: str = "investor_profile.md") -> dict[str, Any]:
    """Read the `backtest` section from investor_profile.md's YAML front matter.

    These knobs (`start_date`, `end_date`, `t_update_days`) only shape
    /run-backtest and the parameter sweeps -- never the live analyze/recommend
    path. Any missing field falls back to the hard-coded default; `start_date`
    / `end_date` of None mean "use the run dir's own window". Dates are
    normalized to ``YYYY-MM-DD`` strings (PyYAML parses bare dates to
    ``datetime.date``).
    """
    import re
    import yaml

    out = dict(_BACKTEST_DEFAULTS)
    p = Path(profile_path)
    if not p.exists():
        return out
    text = p.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return out
    data = yaml.safe_load(m.group(1)) or {}
    bt = data.get("backtest") or {}
    out.update({k: bt[k] for k in _BACKTEST_DEFAULTS if k in bt})
    for k in ("start_date", "end_date", "forward_split_date"):
        if out[k] is not None:
            out[k] = str(out[k])
    return out


# Forward-usage (live) defaults. The `forward` section of investor_profile.md configures the
# forward loop: the daily WebSearch news pull and the periodic curate+recommend. Kept SEPARATE
# from `backtest` so the two paths can diverge deliberately (e.g. a different curator model)
# while sharing one prompt / validator / optimizer / dashboard. `news_lookback_days` falls back
# to the financial_model value when unset here.
_FORWARD_DEFAULTS: dict[str, Any] = {
    "curator_model": "moonshotai/kimi-k2.5",       # LLM that curates the watchlist (OpenRouter or claude-*)
    "retrieval_model": "claude-haiku-4-5-20251001",  # cheap model that DRIVES the WebSearch pull (fixed queries)
    "retriever": "websearch",                       # forward news source: "websearch" (Anthropic web_search tool)
    "news_lookback_days": None,                     # trailing news window the curator reads; None => financial_model
    "inception_date": "2026-07-01",                 # nominal date forward news ingestion began (out-of-sample start)
}


def load_forward_config(profile_path: str = "investor_profile.md") -> dict[str, Any]:
    """Read the `forward` section from investor_profile.md's YAML front matter.

    Configures the live/forward loop (daily news pull + periodic curate). Missing section or
    fields fall back to `_FORWARD_DEFAULTS`; `news_lookback_days` of None resolves to the
    financial_model's value so there is a single default for that window.
    """
    import re
    import yaml

    out = dict(_FORWARD_DEFAULTS)
    p = Path(profile_path)
    if p.exists():
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", p.read_text(), re.DOTALL)
        if m:
            data = yaml.safe_load(m.group(1)) or {}
            fw = data.get("forward") or {}
            out.update({k: fw[k] for k in _FORWARD_DEFAULTS if k in fw})
    if out["news_lookback_days"] is None:
        out["news_lookback_days"] = int(load_financial_model(profile_path).get("news_lookback_days", 21))
    out["inception_date"] = str(out["inception_date"])   # PyYAML parses a bare date to datetime.date
    return out


# Default constant-rate reference curves drawn on dashboard plot 1, in
# percent-per-week. Pure cosmetic yardstick -- never touches the optimizer.
_DASHBOARD_GROWTH_GUIDES_PCT_PER_WEEK = [0.5, 1.0, 1.5]


def load_dashboard_guides(profile_path: str = "investor_profile.md") -> list[float]:
    """Read the top-level `dashboard_growth_guides_pct_per_week` list.

    These are the dotted constant-growth reference lines on the live
    dashboard's value chart (plot 1), expressed in percent per week. They
    are a visual yardstick only -- they do not affect any recommendation,
    so they live at the profile's top level rather than in `financial_model`.
    Missing / malformed => the hard-coded default [0.5, 1.0, 1.5].
    """
    import re
    import yaml

    p = Path(profile_path)
    if not p.exists():
        return list(_DASHBOARD_GROWTH_GUIDES_PCT_PER_WEEK)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", p.read_text(), re.DOTALL)
    if not m:
        return list(_DASHBOARD_GROWTH_GUIDES_PCT_PER_WEEK)
    data = yaml.safe_load(m.group(1)) or {}
    rates = data.get("dashboard_growth_guides_pct_per_week")
    if not isinstance(rates, list) or not rates:
        return list(_DASHBOARD_GROWTH_GUIDES_PCT_PER_WEEK)
    return [float(r) for r in rates]


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


def fetch_prices(tickers: list[str], period: str = "3y", interval: str = "1d",
                 min_history: bool = False) -> pd.DataFrame:
    """Download adjusted-close prices for the given tickers via yfinance.

    With ``min_history=True``, drop any ticker whose own history does not span
    ~the full lookback window before the final row-wise dropna. This matters
    because that dropna intersects all tickers onto their shared dates: ffill
    cannot backfill the leading NaNs of a recently-listed ticker, so a single
    days-old IPO would otherwise truncate the *entire* panel back to its first
    trading day (collapsing the covariance estimate — at the extreme to one row,
    which makes the optimizer fail). Excluded tickers are reported on the
    returned frame's ``.attrs['excluded_short_history']`` so callers can surface
    them. The optimization path turns this on; init-holdings (which just needs
    latest prices) leaves it off."""
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
    prices = prices.dropna(how="all").ffill()

    excluded: list[str] = []
    if min_history and start is not None and len(prices) > 0:
        # Eligible = first trade no later than 5% into the lookback window.
        # A ticker missing more than that would, after the join below, shrink
        # every other ticker's estimation window down to its own start date.
        window_days = (prices.index[-1] - start).days
        cutoff = start + pd.Timedelta(days=round(0.05 * window_days))
        eligible = [t for t in prices.columns
                    if (fv := prices[t].first_valid_index()) is not None and fv <= cutoff]
        excluded = [t for t in prices.columns if t not in eligible]
        if not eligible:
            raise RuntimeError(
                f"no ticker has enough history to span the {period} lookback; "
                f"excluded: {excluded}")
        prices = prices[eligible]

    prices = prices.dropna()
    prices.attrs["excluded_short_history"] = excluded
    return prices


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
    prices = fetch_prices(tickers, period=period, min_history=True)
    returns = compute_returns(prices)
    opt = optimize_portfolio(
        returns, objective=objective, risk_free_rate=risk_free_rate,
        max_weight=max_weight, risk_aversion=risk_aversion,
    )
    risk = risk_metrics(returns, opt["weights"], risk_free_rate=risk_free_rate) \
        if opt.get("success") else None

    return {
        "tickers": list(prices.columns),
        "excluded_short_history": prices.attrs.get("excluded_short_history", []),
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
    watchlist_path: str = "watchlist.csv",
    profile_path: str = "investor_profile.md",
) -> dict[str, Any]:
    """Convert ticker -> dollars + ticker -> price into ticker -> shares,
    then overwrite ``holdings_path`` with a fresh ``ticker, shares`` CSV, and
    SEED ``watchlist_path`` (single ``ticker`` column) with the same tickers
    minus the profile's always_include anchors.

    ``holdings.csv`` holds the real positions; ``watchlist.csv`` is the
    curator-managed optimizer universe (anchors enter the universe from the
    profile, not the watchlist -- see ``_optimizer_universe``). Allocations and
    prices must cover the same tickers. Shares are stored as floats (4 decimals)
    since most modern brokers support fractional shares. Tickers with $0
    allocated keep shares=0.
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

    # Seed the curator-managed watchlist (single 'ticker' column) with the same
    # tickers minus the always_include anchors (which enter the universe from the
    # profile, not the watchlist).
    anchors = {t.upper() for t in load_financial_model(profile_path).get("always_include", [])}
    wl = [r["ticker"] for r in rows if r["ticker"] not in anchors]
    w_path = Path(watchlist_path)
    w_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": wl}).to_csv(w_path, index=False)

    return {
        "out_path": str(o_path),
        "watchlist_path": str(w_path),
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
    # demographic shifts, commodity cycles, regulatory inflections). The 2026
    # geopolitical realignment is split into 4 sub-waves (own catalysts/names);
    # "geopolitical" is kept for back-compat with older curations.
    "geopolitical", "geo_defense", "geo_drones", "geo_tankers", "geo_reconstruction",
    "demographics", "commodities", "regulatory",
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
    # Per-call churn throttle: at most 3 adds / 3 removes. An over-eager curator (common at large
    # max_watchlist_size, where many slots are free) proposing more is not a malformed payload — keep
    # its best 3 (the ones it listed first) and REJECT the excess so the reject-and-retry loop can feed
    # the reason back. Raising here would crash the whole run on one greedy proposal.
    if len(raw_adds) > 3:
        for extra in raw_adds[3:]:
            rejections.append({"ticker": (extra.get("ticker") if isinstance(extra, dict) else str(extra)),
                               "action": "add", "reason": "at most 3 adds per call; excess dropped"})
        raw_adds = raw_adds[:3]
    if len(raw_removes) > 3:
        for extra in raw_removes[3:]:
            rejections.append({"ticker": (extra.get("ticker") if isinstance(extra, dict) else str(extra)),
                               "action": "remove", "reason": "at most 3 removes per call; excess dropped"})
        raw_removes = raw_removes[:3]

    add_tickers = {a.get("ticker") for a in raw_adds if isinstance(a, dict)}
    remove_tickers = {r.get("ticker") for r in raw_removes if isinstance(r, dict)}
    overlap = add_tickers & remove_tickers
    if overlap:
        # A ticker in BOTH adds and removes is a contradictory no-op. Drop it from both lists and record a
        # rejection (mirrors the excess-adds handling above) so the reject-and-retry loop can ask the curator
        # to clarify -- rather than raising and crashing the whole run (previously a hard ValueError).
        for _t in sorted(overlap):
            rejections.append({"ticker": _t, "action": "add/remove",
                               "reason": "appeared in both adds and removes; dropped from both (contradictory)"})
        raw_adds = [a for a in raw_adds if not (isinstance(a, dict) and a.get("ticker") in overlap)]
        raw_removes = [r for r in raw_removes if not (isinstance(r, dict) and r.get("ticker") in overlap)]

    current_set = set(current_watchlist)
    valid_adds: list[dict[str, Any]] = []
    asof = as_of_date or payload.get("as_of_date")

    def _as_text(v: Any) -> str:
        """Coerce an LLM field to a string: some models emit rationale as a LIST of strings."""
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v).strip()
        return str(v or "").strip()

    for add in raw_adds:
        t = add.get("ticker")
        wb = add.get("wave_bucket")
        rationale = _as_text(add.get("rationale"))
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
        rationale = _as_text(rem.get("rationale"))
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
    holdings_path: str = "watchlist.csv",
    history_path: str = "data/curation_history.csv",
    profile_path: str = "investor_profile.md",
    listing_check: bool = True,
    as_of_date: str | None = None,
    max_watchlist_size: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate a watchlist-curator payload and apply it to the watchlist file.

    Adds append a ticker row; removes delete it. Every applied change is
    appended as a row to curation_history.csv. Returns a result dict with
    applied/rejected lists and the post-change watchlist.

    ``holdings_path`` is the file this mutates and is SCHEMA-AWARE:
      - live path passes ``watchlist.csv`` (a single ``ticker`` column);
      - the backtest/sweep sandbox passes a ``ticker,shares`` file.
    The "block removing a ticker with shares>0" rule applies ONLY when a
    ``shares`` column is present (the sandbox). In the live model real
    positions live in the separate ``holdings.csv`` and the optimizer universe
    (watchlist ∪ held) keeps a dropped-but-held ticker recommendable for sale,
    so removing it from the watchlist is allowed.
    """
    fm = load_financial_model(profile_path)
    # max_watchlist_size override lets a backtest/sweep run a cap different from the profile's; without it
    # the validator would silently cap at the profile value and reject every add past it (freezing the
    # watchlist and starving the curator's later picks). None => use the profile.
    max_size = int(max_watchlist_size if max_watchlist_size is not None else fm.get("max_watchlist_size", 12))
    anchors = set(fm.get("always_include", []))

    h_path = Path(holdings_path)
    if not h_path.exists():
        raise FileNotFoundError(f"watchlist/holdings file not found: {h_path}")
    holdings = pd.read_csv(h_path)
    if "ticker" not in holdings.columns:
        raise ValueError(f"{h_path} must have a 'ticker' column")
    has_shares = "shares" in holdings.columns   # sandbox (ticker,shares) vs live watchlist (ticker only)
    current_watchlist = holdings["ticker"].astype(str).tolist()

    # The always_include anchors sit in holdings.csv but are OUTSIDE the
    # curator's managed set: they do not count toward max_watchlist_size and
    # the curator may not add (already permanent) or remove (protected) them.
    # Intercept any anchor-targeting proposal up front with a clear reason,
    # then validate the rest against the managed (non-anchor) watchlist so the
    # cap accounting and membership checks ignore the anchors entirely.
    anchor_rejections: list[dict[str, Any]] = []
    filtered_payload = dict(payload)
    kept_adds, kept_removes = [], []
    for a in payload.get("adds") or []:
        if isinstance(a, dict) and str(a.get("ticker", "")).upper() in anchors:
            anchor_rejections.append({"ticker": a.get("ticker"), "action": "add",
                                      "reason": "already a permanent always_include anchor"})
        else:
            kept_adds.append(a)
    for r in payload.get("removes") or []:
        if isinstance(r, dict) and str(r.get("ticker", "")).upper() in anchors:
            anchor_rejections.append({"ticker": r.get("ticker"), "action": "remove",
                                      "reason": "protected always_include anchor; cannot be removed"})
        else:
            kept_removes.append(r)
    filtered_payload["adds"] = kept_adds
    filtered_payload["removes"] = kept_removes
    managed_watchlist = [t for t in current_watchlist if t.upper() not in anchors]

    validated = _validate_curator_payload(
        filtered_payload, managed_watchlist, max_size,
        listing_check=listing_check, as_of_date=as_of_date,
    )
    valid_adds = validated["valid_adds"]
    valid_removes = validated["valid_removes"]
    rejections = anchor_rejections + validated["rejections"]

    # Block removes for tickers with shares > 0 ONLY when this file carries share
    # counts (the backtest sandbox). In the live model real positions live in the
    # separate holdings.csv, so removing a held ticker from the watchlist is allowed
    # -- the optimizer universe (watchlist ∪ held) keeps it recommendable for sale.
    held = {row["ticker"]: float(row["shares"]) for _, row in holdings.iterrows()} if has_shares else {}
    safe_removes: list[dict[str, Any]] = []
    for rem in valid_removes:
        t = rem["ticker"]
        if held.get(t, 0.0) > 0.0:
            rejections.append({"ticker": t, "action": "remove",
                               "reason": f"current shares={held[t]} > 0; liquidate first"})
        else:
            safe_removes.append(rem)
    valid_removes = safe_removes

    # Apply adds (append rows) and removes (delete rows). Preserve the file's schema:
    # a single-column live watchlist gets ticker-only rows; the sandbox keeps ticker,shares.
    add_row = (lambda t: {"ticker": t, "shares": 0}) if has_shares else (lambda t: {"ticker": t})
    new_rows = pd.DataFrame([add_row(a["ticker"]) for a in valid_adds])
    if not new_rows.empty:
        holdings = pd.concat([holdings, new_rows], ignore_index=True)
    if valid_removes:
        rm_set = {r["ticker"] for r in valid_removes}
        holdings = holdings[~holdings["ticker"].isin(rm_set)].reset_index(drop=True)

    if not dry_run:                          # dry_run: validate + return applied/rejected, but DON'T persist
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

    if rows and not dry_run:
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


def _optimizer_universe(
    watchlist_path: str = "watchlist.csv",
    holdings_path: str = "holdings.csv",
    anchors: list[str] | None = None,
) -> list[str]:
    """The optimizer's ticker universe: the curator-managed WATCHLIST, UNION the
    tickers currently HELD in holdings.csv (shares>0), UNION the always_include
    anchors. The union keeps a ticker recommendable for sale after the curator
    drops it from the watchlist but before the user has sold it out of holdings.
    Uppercased, de-duped, order-stable (watchlist first, then held, then anchors).
    """
    seen: list[str] = []

    def _add(tks: "Any") -> None:
        for t in tks:
            u = str(t).upper().strip()
            if u and u not in seen:
                seen.append(u)

    wl = Path(watchlist_path)
    if wl.exists():
        _add(pd.read_csv(wl)["ticker"].tolist())
    hp = Path(holdings_path)
    if hp.exists():
        h = pd.read_csv(hp)
        if "shares" in h.columns:                                  # real positions: only shares>0 count as held
            _add(h[h["shares"].astype(float) > 0]["ticker"].tolist())
        else:
            _add(h["ticker"].tolist())
    _add(anchors or [])
    return seen


def recommend_portfolio(
    holdings_path: str = "holdings.csv",
    watchlist_path: str = "watchlist.csv",
    out_path: str = "data/recommendations.csv",
    period: str = "3y",
    max_weight: float = 0.25,
    risk_free_rate: float = 0.04,
    objective: str = "max_sharpe",
    risk_aversion: float = 1.0,
    date: str | None = None,
    force: bool = False,
    profile_path: str = "investor_profile.md",
) -> dict[str, Any]:
    """Run an optimization and append per-ticker weights to a CSV.

    Pure Python, no news pulls, no LLM. Universe = ``watchlist.csv`` UNION the
    tickers HELD in ``holdings.csv`` (shares>0) UNION the profile's
    always_include anchors (see ``_optimizer_universe``).

    Schema appended to ``out_path``:
        date, ticker, weight, expected_return, annual_volatility,
        sharpe_ratio, objective

    Idempotent on date (skip unless force=True).
    """
    anchors = load_financial_model(profile_path).get("always_include", [])
    tickers = _optimizer_universe(watchlist_path, holdings_path, anchors)
    if not tickers:
        raise FileNotFoundError(
            f"empty optimizer universe: none of {watchlist_path} / {holdings_path} / anchors yielded tickers")

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
        "excluded_short_history": result.get("excluded_short_history", []),
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
    if cadence == "weekly":
        iso = date.isocalendar()          # (ISO year, ISO week, weekday) — buckets by calendar week
        return (int(iso[0]), int(iso[1]))
    if cadence == "biweekly":
        iso = date.isocalendar()
        return (int(iso[0]), int(iso[1]) // 2)
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


def _rebalance_with_min_trade(target_w, target_watch, cur_shares, cur_value, prices, thr_frac):
    """Rebalance the curator book toward ``target_w`` but SUPPRESS any per-ticker trade whose dollar size is
    below ``thr_frac * cur_value`` (a no-trade band). Suppressed tickers keep their current shares; the tickers
    that ARE traded split the remaining budget by their relative target weights, so the book stays fully
    invested and sums to ``cur_value``. ``thr_frac == 0`` reduces to a full rebalance.
    """
    thr = thr_frac * cur_value
    px = {t: float(prices[t]) for t in set(target_watch) | set(cur_shares)
          if t in prices.index and not pd.isna(prices[t])}
    held: dict[str, float] = {}     # suppressed: keep current shares
    trade: list[str] = []           # rebalance these to fill the remaining budget
    for t in target_watch:
        if t not in px:
            continue
        tgt_sh = (target_w[t] * cur_value) / px[t]
        if abs(tgt_sh - cur_shares.get(t, 0.0)) * px[t] >= thr:
            trade.append(t)
        else:
            held[t] = cur_shares.get(t, 0.0)
    # currently-held tickers dropped from the target: sell if the sell clears the band, else keep holding.
    for t, sh in cur_shares.items():
        if t not in target_watch and sh > 0 and t in px and sh * px[t] < thr:
            held[t] = sh
    held_val = sum(sh * px[t] for t, sh in held.items())
    budget = cur_value - held_val
    tw = sum(target_w[t] for t in trade)
    new = {t: sh for t, sh in held.items() if sh > 0}
    if tw > 0 and budget > 0:
        for t in trade:
            new[t] = (target_w[t] / tw * budget) / px[t]
    else:                                   # nothing tradable / no budget -> hold current for the trade set
        for t in trade:
            new[t] = cur_shares.get(t, 0.0)
    return {t: sh for t, sh in new.items() if sh > 0}


def curator_backtest(
    runs_dir: str,
    out_dir: str = "data/backtest/",
    max_weight: float = 0.25,
    objective: str = "mean_variance",
    risk_aversion: float = 1.0,
    risk_free_rate: float = 0.04,
    benchmarks: list[str] | None = None,
    lookback_years_override: float | None = None,
    t_update_days: int = 1,
    forward_split_date: str | None = None,
    always_include: list[str] | None = None,
    min_trade_frac: float = 0.0,
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
    # Permanent safe-haven anchors: always in the curator strategy's universe
    # and protected from removal, outside the max_watchlist_size budget. This
    # is the FULL always_include set even if some anchor is also a starter
    # ticker (so it is protected regardless). Baselines below use
    # starter_watchlist as-is, so any anchor NOT already in the starter stays
    # out of them (the lift figure then reflects curation + the anchor policy).
    anchors = [t.upper() for t in (always_include or [])]
    start_date = pd.Timestamp(starter["start_date"])
    end_date = pd.Timestamp(starter["end_date"])
    cadence = starter.get("rebalance_period", "monthly")
    initial_usd = float(starter.get("initial_usd", 50000.0))
    lookback_years = float(lookback_years_override) if lookback_years_override is not None \
        else float(starter.get("lookback_years", 1.3))
    # Minimum trading-day observations required to (re)optimize at a rebalance. Must SCALE with the
    # lookback window: a short optimizer_lookback (e.g. 30 calendar days ~= 21 trading days) can never
    # clear a fixed 30-obs floor, so every rebalance would be skipped and the backtest would produce
    # no snapshots. Use ~half the lookback's expected trading days, with a small absolute floor so a
    # covariance over a handful of assets is still estimable.
    min_obs = max(10, int(round(lookback_years * 252 * 0.5)))
    max_size = int(starter.get("max_watchlist_size", 12))
    # Optional CBS extras (from _starter.json): seed the curator's day-0 holdings with a fixed weight vector
    # (initial_weights, e.g. the CBT portfolio at the CBS start) instead of the day-0 optimize; and a naive
    # equal-weight buy-and-hold benchmark over a given ticker list (naive_benchmark, e.g. AAPL/GOOGL/AMZN).
    initial_weights = starter.get("initial_weights") or None
    naive_benchmark = [str(t).upper() for t in (starter.get("naive_benchmark") or [])]

    # Union of every ticker that could appear across the run, so the
    # bulk yfinance fetch only happens once. Anchors are always in the fetch.
    union: set[str] = set(starter_watchlist) | set(anchors) | set(naive_benchmark)
    if initial_weights:
        union |= {str(t).upper() for t in initial_weights}
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
    # Anchors seed the sandbox holdings alongside the starter watchlist, so the
    # curator strategy's universe always includes them; apply_curator_decisions
    # (via the sandbox profile's always_include) keeps them out of the cap
    # count and protects them from removal.
    sandbox_tickers = starter_watchlist + [a for a in anchors if a not in starter_watchlist]
    pd.DataFrame({"ticker": sandbox_tickers,
                  "shares": [0] * len(sandbox_tickers)}).to_csv(
        sandbox_holdings, index=False)
    if sandbox_history.exists():
        sandbox_history.unlink()
    sandbox_profile = sandbox / "profile.md"
    _anchor_yaml = "[" + ", ".join(anchors) + "]"
    sandbox_profile.write_text(
        f"---\nalways_include: {_anchor_yaml}\n"
        f"financial_model:\n  max_watchlist_size: {max_size}\n---\n"
    )

    # Four parallel walk-forwards.
    cur_shares: dict[str, float] = {}
    fix_shares: dict[str, float] = {}
    bnh_shares: dict[str, float] = {}      # optimizer day-0 weights held forever (ablation)
    eq_shares: dict[str, float] = {}       # equal-weight starter held forever (headline)
    naive_shares: dict[str, float] = {}    # equal-weight naive_benchmark held forever (e.g. AAPL/GOOGL/AMZN)
    snap_rows: list[dict[str, Any]] = []
    rec_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    weight_l1: list[float] = []
    _turnover_usd = 0.0                     # actual $ traded on rebalances (post-min_trade suppression)
    last_weights: dict[str, float] | None = None
    last_period: tuple | None = None
    curation_summary: list[dict[str, Any]] = []

    def _value(shares: dict[str, float], date: pd.Timestamp) -> float:
        return sum(s * float(full_prices.loc[date, t])
                   for t, s in shares.items()
                   if t in full_prices.columns
                   and not pd.isna(full_prices.loc[date, t]))

    # Execution lag. Weights are decided from prices through the rebalance
    # date's close (the "signal"), but a real user runs /review-portfolio and
    # only places the trade later that day or 1-3 sessions on. t_update_days is
    # how many trading days after the signal the trade actually lands; the
    # position is held unchanged until then and re-bought at that day's close
    # (see the deferred-execution block in the loop). The active strategies
    # (curator + the fixed-watchlist optimizer) are lagged because they
    # re-decide weights every rebalance; the passive buy-and-hold baselines are
    # NOT lagged -- they make one day-0 entry and have no recurring decision to
    # delay. t_update_days=0 reduces to "transact at the signal close" (the
    # optimistic "smart money" assumption); the default of 1 (next session) is
    # the realistic case a live user actually experiences.
    _price_index = full_prices.index

    def _exec_date(date: pd.Timestamp) -> pd.Timestamp:
        # The one-time initial deployment (first rebalance) is capital setup,
        # not a reaction to a fresh signal, so it is not lagged -- this keeps
        # all curves and the benchmark window anchored at day 0. Every
        # subsequent rebalance is lagged by t_update_days.
        if date == daily_dates[0]:
            return date
        pos = _price_index.get_loc(date)
        return _price_index[min(pos + t_update_days, len(_price_index) - 1)]

    # A scheduled-but-not-yet-executed rebalance for the curator + fixed
    # strategies: {"exec_date", "cur_w", "cur_watch", "fix_w", "fix_watch"}.
    pending: dict[str, Any] | None = None

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
                            "rejections": len(result["rejections"]),        # count (back-compat)
                            "rejections_detail": result["rejections"],      # [{ticker, action, reason}, ...]
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
            if len(slice_cur) < min_obs or not cur_watchlist:
                # Not enough history yet; carry forward without rebalancing.
                last_period = period
                continue
            returns = compute_returns(slice_cur)
            opt = _optimize_or_equal_weight(
                returns, cur_watchlist, objective, max_weight,
                risk_aversion, risk_free_rate,
            )
            cur_weights = opt["weights"]
            if is_first_day and initial_weights:
                # Seed the curator's day-0 portfolio with a FIXED allocation (e.g. the CBT portfolio at the
                # CBS start date) instead of the day-0 optimize. The curator drives normally from the next
                # rebalance, so day 0 = the given distribution and the first real curator move lands next period.
                _iw = {str(t).upper(): float(w) for t, w in initial_weights.items()
                       if str(t).upper() in full_prices.columns}
                _tot = sum(_iw.values())
                if _tot > 0:
                    cur_weights = {t: w / _tot for t, w in _iw.items()}
                    cur_watchlist = list(cur_weights)

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
            #    Re-decides each rebalance, so it is lagged like the curator.
            slice_fix = full_prices.loc[lookback_start:date, starter_watchlist].dropna(how="any", axis=1)
            fix_watch = list(slice_fix.columns)
            fix_weights = None
            if len(slice_fix) >= min_obs and fix_watch:
                fix_returns = compute_returns(slice_fix)
                fix_opt = _optimize_or_equal_weight(
                    fix_returns, fix_watch, objective, max_weight,
                    risk_aversion, risk_free_rate,
                )
                fix_weights = fix_opt["weights"]

            # 5) Buy-and-hold baseline: optimize once on day 0, then hold.
            #    Passive (no recurring decision), so its single entry is NOT
            #    lagged -- it buys at the day-0 close regardless of t_update_days.
            if not bnh_shares and fix_weights is not None:
                bnh_shares = {
                    t: (fix_weights[t] * initial_usd) / float(full_prices.loc[date, t])
                    for t in fix_watch
                }
            # 6) Equal-weight buy-and-hold baseline: $initial_usd / N to each
            #    starter ticker on day 0, then hold forever. Also passive, also
            #    entered at the day-0 close (not lagged). This is the headline
            #    comparator a typical 2021 retail investor might actually have
            #    built without an optimizer's concentration tilt.
            if not eq_shares and fix_watch:
                w_eq = 1.0 / len(fix_watch)
                eq_shares = {
                    t: (w_eq * initial_usd) / float(full_prices.loc[date, t])
                    for t in fix_watch
                }
            # Naive equal-weight buy-and-hold benchmark over an explicit ticker list (e.g. AAPL/GOOGL/AMZN):
            # $initial_usd / N to each on day 0, then held. Passive, entered at the day-0 close (not lagged).
            if not naive_shares and naive_benchmark:
                _nb = [t for t in naive_benchmark if t in full_prices.columns
                       and not pd.isna(full_prices.loc[date, t])]
                if _nb:
                    _wn = 1.0 / len(_nb)
                    naive_shares = {t: (_wn * initial_usd) / float(full_prices.loc[date, t]) for t in _nb}

            # Schedule the curator + fixed-optimizer execution for the lagged
            # day. Holdings stay put until then.
            pending = {
                "exec_date": _exec_date(date),
                "cur_w": cur_weights, "cur_watch": cur_watchlist,
                "fix_w": fix_weights, "fix_watch": fix_watch,
            }
            last_period = period

        # Deferred execution: once the lagged execution day arrives, value the
        # current book and re-buy to target weights at that day's close. Both
        # legs use the same day's price, so the book value is preserved across
        # the trade (no curve blip) and the first invested snapshot equals the
        # capital actually deployed.
        if pending is not None and date >= pending["exec_date"]:
            p = pending
            cur_value = _value(cur_shares, date) if cur_shares else initial_usd
            _old_shares = dict(cur_shares)      # pre-rebalance book, for actual-turnover tracking
            if min_trade_frac > 0 and cur_shares:
                # Suppress rebalancing trades below min_trade_frac of the book (no-trade band). The one-time
                # initial deployment (empty book) is never suppressed -- you must buy in.
                cur_shares = _rebalance_with_min_trade(
                    p["cur_w"], p["cur_watch"], cur_shares, cur_value,
                    full_prices.loc[date], min_trade_frac)
            else:
                cur_shares = {
                    t: (p["cur_w"][t] * cur_value) / float(full_prices.loc[date, t])
                    for t in p["cur_watch"]
                }
            # Actual $ traded on REBALANCES (post-suppression), summed for the turnover metric. The one-time
            # initial deployment (empty prior book) is excluded so the metric is pure ongoing rebalancing.
            if _old_shares:
                _turnover_usd += sum(
                    abs(cur_shares.get(t, 0.0) - _old_shares.get(t, 0.0)) * float(full_prices.loc[date, t])
                    for t in set(cur_shares) | set(_old_shares)
                    if t in full_prices.columns and not pd.isna(full_prices.loc[date, t]))
            if p["fix_w"] is not None:
                fix_value = _value(fix_shares, date) if fix_shares else initial_usd
                fix_shares = {
                    t: (p["fix_w"][t] * fix_value) / float(full_prices.loc[date, t])
                    for t in p["fix_watch"]
                }
            pending = None

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
        if fix_shares or bnh_shares or eq_shares or naive_shares:
            baseline_rows.append({
                "date": str(date.date()),
                "fixed_total": round(_value(fix_shares, date), 2) if fix_shares else None,
                "bnh_total": round(_value(bnh_shares, date), 2) if bnh_shares else None,
                "eq_total": round(_value(eq_shares, date), 2) if eq_shares else None,
                "naive_total": round(_value(naive_shares, date), 2) if naive_shares else None,
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

    # Forward-test split (REPORTING ONLY -- does not touch optimizer math or any
    # live recommendation). Splits realized performance into in-sample (rebalances
    # on/before forward_split_date, where the LLM's training may already know the
    # outcomes) and out-of-sample (strictly after it, genuinely unknowable when
    # decided). A curator edge that survives out-of-sample is evidence of real
    # signal; one that collapses toward buy-and-hold is evidence of hindsight.
    forward_split: dict[str, Any] | None = None
    if forward_split_date:
        _split = pd.Timestamp(forward_split_date)
        _t = totals.copy()
        _t.index = pd.to_datetime(_t.index)
        _eq = None
        if "eq_total" in baselines_df:
            _e = baselines_df.dropna(subset=["eq_total"]).copy()
            _e["date"] = pd.to_datetime(_e["date"])
            _eq = _e.set_index("date")["eq_total"].sort_index()
        _w0, _w1 = _t.index[0], _t.index[-1]

        def _seg_ret(series, a, b):
            if series is None:
                return None
            va, vb = series.asof(a), series.asof(b)
            if pd.isna(va) or pd.isna(vb) or va == 0:
                return None
            return float(vb / va - 1.0)

        _reb = [pd.Timestamp(c["date"]) for c in curation_summary
                if isinstance(c, dict) and c.get("date") and "error" not in c]
        n_in = sum(1 for d in _reb if d <= _split)
        n_out = sum(1 for d in _reb if d > _split)
        if _w0 <= _split <= _w1:
            cur_in, cur_out = _seg_ret(_t, _w0, _split), _seg_ret(_t, _split, _w1)
            bnh_in, bnh_out = _seg_ret(_eq, _w0, _split), _seg_ret(_eq, _split, _w1)
            lift_in = (cur_in - bnh_in) if (cur_in is not None and bnh_in is not None) else None
            lift_out = (cur_out - bnh_out) if (cur_out is not None and bnh_out is not None) else None
            forward_split = {
                "split_date": str(_split.date()),
                "in_sample": {"curator": cur_in, "buy_and_hold": bnh_in,
                              "lift_pp": (lift_in * 100 if lift_in is not None else None),
                              "n_rebalances": n_in},
                "out_sample": {"curator": cur_out, "buy_and_hold": bnh_out,
                               "lift_pp": (lift_out * 100 if lift_out is not None else None),
                               "n_rebalances": n_out},
                "populated": n_out > 0,
            }
        else:
            forward_split = {
                "split_date": str(_split.date()), "populated": False,
                "note": "split date is outside the backtest window; "
                        "extend end_date past it to populate the out-of-sample segment",
            }
        (out / "forward_split.json").write_text(json.dumps(forward_split, indent=2))

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

    # Optional forward-test section for report.md (reporting only).
    fwd_section = ""
    if forward_split and forward_split.get("in_sample"):
        _fi, _fo = forward_split["in_sample"], forward_split["out_sample"]
        _sd = forward_split["split_date"]
        _p = lambda x: f"{x * 100:+.2f}%" if x is not None else "n/a"
        _pp = lambda x: f"{x:+.2f}pp" if x is not None else "n/a"
        fwd_section = (
            f"## Forward test (out-of-sample split at {_sd})\n\n"
            f"In-sample = rebalances on/before the split, where the curator LLM's "
            f"training may already know the outcomes; out-of-sample = rebalances "
            f"strictly after it, genuinely unknowable when decided. A curator lift "
            f"that holds out-of-sample is evidence of real signal; one that collapses "
            f"toward buy-and-hold is evidence the in-sample result was hindsight.\n\n"
            f"| Segment | Rebalances | Curator | Buy-and-hold | Lift |\n|---|---|---|---|---|\n"
            f"| In-sample (≤ {_sd}) | {_fi['n_rebalances']} | {_p(_fi['curator'])} | "
            f"{_p(_fi['buy_and_hold'])} | {_pp(_fi['lift_pp'])} |\n"
            f"| Out-of-sample (> {_sd}) | {_fo['n_rebalances']} | {_p(_fo['curator'])} | "
            f"{_p(_fo['buy_and_hold'])} | {_pp(_fo['lift_pp'])} |\n\n"
        )
        if not forward_split.get("populated"):
            fwd_section += (
                "_No out-of-sample rebalances yet — extend the backtest window's "
                "`end_date` past the split date to populate the out-of-sample row._\n\n"
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
        f"{fwd_section}"
        f"## Caveats\n\n"
        f"- No transaction costs or taxes modeled.\n"
        f"- Execution lag: t_update_days={t_update_days}. Each rebalance is "
        f"decided on the rebalance date's close but executed {t_update_days} "
        f"trading day(s) later at that day's close (the one-time initial "
        f"deployment is not lagged). This models the gap between running a "
        f"review and placing the trade. 0 = the optimistic same-close run. "
        f"How much the lag matters is window-dependent (near-noise over long "
        f"windows, material over short ones); compare against the t=0 run.\n"
        f"- Look-ahead bias is only partly controlled. The optimizer math is "
        f"clean: it sees prices only up to the rebalance date. The curator's "
        f"news/selection path is NOT fully clean: the agent's strict as-of-date "
        f"discipline (persona reset, WebSearch `before:` filters, suppression "
        f"list, self-critique) suppresses explicit citation of post-date facts, "
        f"but it cannot remove (a) the LLM's own training-cutoff foreknowledge "
        f"of which tickers later won, (b) selection bias from fame-weighted "
        f"search ranking that surfaces eventual winners' early coverage, or (c) "
        f"survivorship/revision bias in today's edited/deleted web record. This "
        f"run uses plain WebSearch + `before:` filters only (no Wayback as-of "
        f"snapshots or date-honored corpus), so treat the result as a "
        f"hindsight-tinted upper bound, not a clean out-of-sample backtest.\n"
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
        "actual_turnover_usd": round(_turnover_usd, 2),
        # Total $ actually traded on rebalances / initial capital -> a unit-free churn number that DROPS as
        # min_trade_frac rises (the point of the sweep). Excludes the one-time initial deployment.
        "turnover_ratio": round(_turnover_usd / initial_v, 3) if initial_v else 0.0,
        "forward_split": forward_split,
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
    "quantum", "nuclear", "nuclear_fusion", "demographics",
    "geo_defense", "geo_drones", "geo_tankers", "geo_reconstruction", "geopolitical",
    "general_markets", "cashlike",
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
    "nuclear":            "#eab308",  # gold (curator's bucket name)
    "nuclear_fusion":     "#eab308",  # gold (alias used by TICKER_WAVE)
    "demographics":       "#17becf",  # cyan
    "geopolitical":       "#e377c2",  # pink (legacy single geo bucket)
    "geo_defense":        "#e377c2",  # pink
    "geo_drones":         "#c2185b",  # deep magenta
    "geo_tankers":        "#8c564b",  # brown (energy / shipping)
    "geo_reconstruction": "#b5651d",  # ochre (construction)
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
    "BWET": "equity ETF",
    "HUMN": "equity ETF", "SPCX": "equity ETF",
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
    "TSM": "AI", "AVGO": "AI", "AMZN": "AI", "AMD": "AI",
    "ASML": "AI", "VRT": "AI", "PLTR": "AI", "ORCL": "AI",
    # Robotics
    "BOTZ": "robotics", "ROBO": "robotics",
    "ISRG": "robotics", "SYM": "robotics", "HUMN": "robotics",
    "TER": "robotics", "ROK": "robotics", "KOID": "robotics",
    # Rockets / spacecraft
    "RKLB": "rockets_spacecraft", "ARKX": "rockets_spacecraft",
    "ASTS": "rockets_spacecraft", "SPCX": "rockets_spacecraft",
    "LUNR": "rockets_spacecraft", "KRMN": "rockets_spacecraft",
    "VOYG": "rockets_spacecraft", "FLY": "rockets_spacecraft",
    "PL": "rockets_spacecraft",
    # Engineered biology
    "ARKG": "engineered_biology",
    # Quantum computing
    "QTUM": "quantum", "IONQ": "quantum", "QBTS": "quantum", "RGTI": "quantum",
    # Nuclear (NUKZ is a fission-heavy nuclear-energy ETF; nuclear_fusion
    # kept as an alias for forward compatibility with pure-play fusion
    # firms like Commonwealth Fusion or Helion if they go public).
    "NUKZ": "nuclear", "NLR": "nuclear", "URA": "nuclear", "CCJ": "nuclear",
    "CEG": "nuclear", "VST": "nuclear", "BWXT": "nuclear", "SMR": "nuclear",
    "GEV": "nuclear", "LEU": "nuclear", "OKLO": "nuclear",
    # Demographics (aging-population thesis: GLP-1 leaders, healthcare,
    # eldercare REITs, automation that backfills labor shortages).
    "LLY": "demographics", "XLV": "demographics", "WELL": "demographics",
    "CTRE": "demographics",
    # Geopolitical realignment, split into 4 sub-waves (own catalysts/names).
    "STNG": "geo_tankers", "FRO": "geo_tankers", "INSW": "geo_tankers", "DHT": "geo_tankers",
    "BWET": "geo_tankers", "LNG": "geo_tankers", "XLE": "geo_tankers", "TNK": "geo_tankers",
    "LMT": "geo_defense", "NOC": "geo_defense", "ITA": "geo_defense", "EUAD": "geo_defense",
    "GD": "geo_defense", "RTX": "geo_defense", "LHX": "geo_defense", "TDY": "geo_defense",
    "AVAV": "geo_drones", "KTOS": "geo_drones", "RCAT": "geo_drones", "ONDS": "geo_drones",
    "PWR": "geo_reconstruction", "ACM": "geo_reconstruction", "FLR": "geo_reconstruction",
    "URI": "geo_reconstruction", "J": "geo_reconstruction",
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
    "nuclear": "nuclear",
    "nuclear_fusion": "nuclear",
    "demographics": "demographics",
    "geopolitical": "geopolitical",
    "geo_defense": "defense",
    "geo_drones": "drones",
    "geo_tankers": "tankers",
    "geo_reconstruction": "reconstruction",
    "general_markets": "general_markets",
    "cashlike": "cashlike",
}


def _ticker_label(t: str, ticker_wave: dict[str, str] | None = None) -> str:
    """Two-line tick label: ticker on top, wave bucket (equities) or
    asset class (non-equities) on a tighter second line. <sup> shifts
    the second-line baseline UP (superscript), so the wave/asset text
    sits ~half-ex closer to the ticker than a plain second line would.

    ``ticker_wave`` is the effective ticker->wave map (curation_history
    overlaid on TICKER_WAVE); pass the live dashboard's merged map so new
    curator adds show their real wave. Falls back to the static
    TICKER_WAVE when not supplied (e.g. the curator-backtest dashboard)."""
    tw = ticker_wave if ticker_wave is not None else TICKER_WAVE
    cls = TICKER_ASSET_CLASS.get(t, "equity")
    if cls == "equity":
        wave = WAVE_DISPLAY_LABEL.get(tw.get(t, "general_markets"), "")
        return f"{t}<br><sup>{wave}</sup>"
    if cls == "equity ETF":
        wave = WAVE_DISPLAY_LABEL.get(tw.get(t, "general_markets"), "")
        return f"{t}<br><sup>{wave} ETF</sup>"
    return f"{t}<br><sup>{cls}</sup>"


def _effective_ticker_wave(
    history_path: str = "data/curation_history.csv",
) -> dict[str, str]:
    """Ticker -> wave bucket, read straight from the curator's own log so
    the live dashboard's wave charts never drift when a new ticker enters
    the watchlist. Each ticker is bucketed by the wave_bucket the curator
    assigned it on its most recent ``add`` row in curation_history.csv;
    the static TICKER_WAVE map is the fallback for tickers that predate
    curation (the starter watchlist and the always_include anchors).

    Returns ``{**TICKER_WAVE, **<curated overrides>}`` so curated tickers
    win and everything else keeps its static bucket."""
    overrides: dict[str, str] = {}
    p = Path(history_path)
    if p.exists():
        try:
            hist = pd.read_csv(p)
            # File is append-only chronological, so iterating in row order
            # lets the most recent add for each ticker overwrite earlier ones.
            for row in hist[hist["action"] == "add"].itertuples():
                wb = str(getattr(row, "wave_bucket", "") or "").strip()
                if wb in _VALID_WAVE_BUCKETS:
                    overrides[row.ticker] = wb
        except Exception:
            # A malformed log should never break the dashboard; fall back
            # to the static map for every ticker.
            overrides = {}
    return {**TICKER_WAVE, **overrides}






# Pages and the labels they expose in the cross-page nav strip. Keys are
# the bare filenames (no path) of the published GitHub Pages files.
_NAV_PAGES: list[tuple[str, str]] = [
    ("index.html", "live dashboard"),
    ("forward_test.html", "forward test"),          # out-of-sample curator replay on the live corpus
    ("backtest_gkg_3yr_kimi.html", "Curator DB"),   # default curator (kimi); Sonnet DBs retired from nav
    ("sweep_risk_aversion.html", "sweep: risk_aversion"),
    ("sweep_lookback.html", "sweep: lookback"),
    ("sweep_concentration_cap.html", "sweep: concentration_cap"),
    ("sweep_max_watchlist_size.html", "sweep: max_watchlist_size"),
]


def _nav(current: str) -> str:
    """Render the shared dashboard nav. scripts/dash_nav.py is the SINGLE source of truth for the nav
    (README + Backtest + Forwardtest groups), imported here so every dashboard -- the scripts/ builders
    and these portfolio dashboards -- renders the identical strip."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import dash_nav
    return dash_nav.render(current)


def _nav_strip(current: str, pages: "list[tuple[str, str]] | None" = None) -> str:
    """Legacy per-page nav (superseded by _nav / dash_nav). Kept only so any un-migrated caller still
    renders; the live dashboards all use _nav now.
    Return an HTML <nav> with links to published pages.
    The entry whose filename matches ``current`` is rendered as bold text
    instead of a link, so a reader can see which page they're on. The strip
    also carries a right-aligned "generated <local time>" stamp — the moment
    this page's HTML was produced — so a reader can tell fresh from stale
    content on GitHub Pages at a glance. ``pages`` overrides the default
    ``_NAV_PAGES`` list (used by the GKG dashboards, which link only to the
    README and their sibling DB while the new design has nothing else to show
    yet)."""
    parts = []
    for fname, label in (pages if pages is not None else _NAV_PAGES):
        if fname == current:
            parts.append(f"<strong>{label}</strong>")
        else:
            parts.append(f'<a href="{fname}">{label}</a>')
    # Local wall-clock time with tz abbreviation (e.g. "2026-07-02 14:40 EDT").
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return (
        '<nav style="font-size:14px;color:#555;margin:0 0 1em 0;'
        'padding-bottom:0.5em;border-bottom:1px solid #eee;display:flex;'
        'justify-content:space-between;flex-wrap:wrap;gap:0.5em;">'
        '<span>' + " · ".join(parts) + '</span>'
        f'<span style="color:#999;white-space:nowrap;">generated {generated}</span>'
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


def _thesis_buy_hold_curve(
    thesis_holdings: dict[str, float],
    start: pd.Timestamp,
    end: pd.Timestamp,
    anchor: float,
) -> "pd.Series | None":
    """Daily portfolio value of the thesis-baseline holdings held without
    rebalancing from ``start`` to ``end``, then uniformly rescaled so the
    first value equals ``anchor``. Used by the live dashboard's plot 1 to
    contrast the path actually taken (curator picks plus manual rebalances)
    against the counterfactual of just holding the initial
    /initialize-portfolio allocation."""
    tickers = [t for t, s in thesis_holdings.items() if s > 0]
    if not tickers:
        return None
    try:
        raw = yf.download(tickers, start=start, end=end + pd.Timedelta(days=1),
                          auto_adjust=True, progress=False, group_by="column")
    except Exception:  # noqa: BLE001 — yfinance is permissively wrapped elsewhere too.
        return None
    if raw.empty:
        return None
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
        else raw[["Close"]].rename(columns={"Close": tickers[0]})
    closes = closes.dropna(how="all").ffill().dropna(how="all")
    common = [t for t in tickers if t in closes.columns]
    if not common:
        return None
    shares = pd.Series({t: thesis_holdings[t] for t in common})
    values = closes[common].mul(shares, axis=1).sum(axis=1).dropna()
    if values.empty or float(values.iloc[0]) <= 0:
        return None
    return (values / float(values.iloc[0])) * anchor


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


# Shared filled-square marker style for rebalance points on both dashboards'
# plot 1. A solid orange fill with a thin white outline reads clearly as a
# filled square sitting on the (also orange) curve, not a hollow outline.
_REBALANCE_MARKER = {"symbol": "square", "size": 10, "color": "#ea580c",
                     "line": {"width": 1.5, "color": "white"}}


def _rebalance_popup(curation_json: Path) -> str:
    """Tooltip body for one rebalance marker: what the curator changed
    (adds/removes) and a one-sentence 'why', read from a single curation
    JSON. Returns '' when the file is missing.

    The 'why' is just the first complete sentence of rationale_overall so
    the tooltip stays short and never ends in a "…": the "Stand at <date>
    close." preamble is dropped, a min length keeps a short lead-in from
    ending the quote early, run-on sentences are capped at ~220 chars at a
    clause boundary, and any dangling open-paren is trimmed.
    """
    import textwrap, re
    if not curation_json.exists():
        return ""
    cj = json.loads(curation_json.read_text())
    parts: list[str] = []
    adds = [a.get("ticker", "") for a in (cj.get("adds") or [])]
    rems = [r.get("ticker", "") for r in (cj.get("removes") or [])]
    if adds:
        parts.append(f"<span style='color:#0a7a3a;'>add: {', '.join(adds)}</span>")
    if rems:
        parts.append(f"<span style='color:#b91c1c;'>remove: {', '.join(rems)}</span>")
    if not adds and not rems:
        parts.append("<i>no changes</i>")
    text = " ".join((cj.get("rationale_overall") or "").split())
    text = re.sub(r"^Stand at[^.]*\.\s*", "", text)
    if text:
        first = text
        for m in re.finditer(r"[.!?](?:\s|$)", text):
            if m.start() + 1 >= 60:
                first = text[: m.start() + 1]
                break
        if len(first) > 220:
            head = first[:220]
            cut = max(head.rfind(", "), head.rfind("; "), head.rfind(". "))
            if cut < 60:
                cut = head.rfind(" ")
            first = head[:cut]
            if first.count("(") > first.count(")"):
                first = first[: first.rfind("(")]
            first = first.rstrip(" ,;.") + "."
        wrapped = "<br>".join(textwrap.wrap(first, width=64))
        if wrapped:
            parts.append(f"<i>why:</i><br>{wrapped}")
    return "<br>".join(parts)


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

    # Effective ticker -> wave map: the curator's own curation_history.csv
    # overlaid on the static TICKER_WAVE. Every wave chart below uses this
    # instead of TICKER_WAVE directly, so a ticker the curator adds is
    # bucketed by the wave it was added under and never silently drifts
    # into general_markets when it's missing from the static map.
    ticker_wave = _effective_ticker_wave()

    # Composition text for the thesis buy-and-hold caption that appears
    # below plot 1 in live mode. Populated when the buy-and-hold trace
    # is successfully built; stays None when no thesis baseline exists or
    # the yfinance fetch fails. Read later in fig.add_annotation.
    _bh_alloc_html: "str | None" = None

    # Pre-compute the trade list before the row layout: when every proposed
    # trade falls below `min_trade_size_frac` (a fraction of portfolio value)
    # from investor_profile.md the whole trade-table chart is omitted and
    # subsequent charts shift up. Also up front so the table row can be sized
    # to fit every trade row (Plotly Tables truncate instead of scrolling when
    # their subplot domain is too small). Returns (ticker, action, shares, $,
    # cur, target) tuples plus running totals; empty list when nothing to trade.
    # The fraction is converted to a dollar floor below, once portfolio value
    # (trade_total_value) is known.
    _min_trade_frac = load_financial_model().get("min_trade_size_frac", 0.0) if is_live else 0.0
    try:
        _min_trade_frac = float(_min_trade_frac)
    except (TypeError, ValueError):
        _min_trade_frac = 0.0
    trade_rows: list[tuple] = []
    trade_total_buy = 0.0
    trade_total_sell = 0.0
    trade_total_value = 0.0
    if is_live and snap_path.exists() and rec_path.exists():
        try:
            _snaps_tt = pd.read_csv(snap_path, parse_dates=["date"])
            _recs_tt = pd.read_csv(rec_path, parse_dates=["date"])
            _snap_latest_tt = _snaps_tt[_snaps_tt["date"] == _snaps_tt["date"].max()].copy()
            _rec_latest_tt = _recs_tt[_recs_tt["date"] == _recs_tt["date"].max()].copy()
            trade_total_value = float(_snap_latest_tt["value"].sum())
            # Convert the fractional floor to a dollar floor now that we know the
            # portfolio value (e.g. frac 0.1 on a $95k book => a $9.5k min trade).
            _min_trade_usd = _min_trade_frac * trade_total_value
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
                    if abs(delta_dollars) < _min_trade_usd:
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

    # Row layout. Chart 5 (the trade table) is always present on the live
    # dashboard; rows below min_trade_size_frac * portfolio are filtered out above, and
    # when that leaves the table empty only the header renders.
    R_PORTFOLIO       = 1
    R_TURNOVER        = 2
    R_REC_WAVE        = 3
    R_LATEST_WEIGHTS  = 4
    R_TRADE_TABLE     = 5 if is_live else None
    R_ACTUAL_WEIGHTS  = 6 if is_live else None
    R_GAIN_INIT       = 7 if is_live else 5
    _after_gain       = 8 if is_live else 6
    R_ASSET_USD       = _after_gain
    R_WAVE_USD        = _after_gain + 1
    R_EXP_VS_REAL     = None
    n_rows            = R_WAVE_USD

    _chart5_anchor = "portfolio as initialized" if is_live else "backtest start"
    _chart5_tail = (
        "Bars sum to total realized portfolio gain since the thesis was set. Green = winners, red = losers."
        if is_live else
        "Bars sum to total realized portfolio gain over the backtest window. Green = winners, red = losers."
    )

    # Build the title list in row order, numbering as we go.
    titles_list: list[str] = []
    titles_list.append(
        f"{R_PORTFOLIO}. Portfolio value over time"
    )
    titles_list.append(
        f"{R_TURNOVER}. Recommended rebalance turnover"
    )
    titles_list.append(
        f"{R_REC_WAVE}. Recommended portfolio percentages"
    )
    titles_list.append(
        f"{R_LATEST_WEIGHTS}. Latest recommended portfolio %"
    )
    if R_TRADE_TABLE is not None:
        titles_list.append(
            f"{R_TRADE_TABLE}. Trades to move from actual to recommended"
            "<br><sub><i>Per-ticker buys and sells needed to rebalance from today's actual portfolio (chart 6 below) to the latest recommendation (chart 4 above).</i></sub>"
        )
    if R_ACTUAL_WEIGHTS is not None:
        titles_list.append(
            f"{R_ACTUAL_WEIGHTS}. Today's actual portfolio %"
            "<br><sub><i>Per-ticker share of total portfolio value from today's snapshot. Compare against chart 4 above to see how far the actual portfolio sits from the latest recommendation.</i></sub>"
        )
    titles_list.append(
        f"{R_GAIN_INIT}. Cumulative $ gain since {_chart5_anchor}"
    )
    titles_list.append(
        f"{R_ASSET_USD}. Actual portfolio $ by asset class over time"
    )
    titles_list.append(
        f"{R_WAVE_USD}. Actual portfolio $ by wave over time"
    )
    titles_all = tuple(titles_list)

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
        # domain isn't tall enough; conversely, oversizing the domain
        # leaves empty space below the table that pushes the next chart
        # away. Buffer = 50px covers the subplot title plus a small
        # bottom padding. Width units are 308px per chart row (see
        # fig.update_layout height below).
        _table_px = 32 + max(1, len(trade_rows)) * 34 + 50
        _row_h[R_TRADE_TABLE - 1] = max(0.5, _table_px / 308.0)
    fig = make_subplots(
        rows=n_rows, cols=1,
        subplot_titles=titles_all,
        vertical_spacing=0.030,
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
        # Live plot 1: blue for the actual portfolio, orange for the
        # thesis buy-and-hold counterfactual, dotted green for SPY.
        # Non-live (legacy backtest) keeps blue too.
        _port_color = "#3b82f6" if is_live else "#1f77b4"
        _port_width = 2.5 if is_live else 2
        fig.add_trace(
            go.Scatter(x=totals.index, y=totals.values, mode=_ts_mode,
                       name="Portfolio $", line={"width": _port_width, "color": _port_color},
                       legend="legend", legendrank=7),
            row=1, col=1,
        )
        # Mark each curator-driven review with a filled orange square on
        # the portfolio-value line. Dates come from one curation JSON per
        # /review-portfolio run in data/curator_runs/live/; hovering a
        # square shows that review's adds/removes and the curator's reason
        # (same popup as the curator-backtest dashboard). asof() places the
        # marker on the nearest snapshot at/before the review date.
        _live_runs = Path("data/curator_runs/live")
        if _live_runs.exists() and len(totals) > 0:
            _rb_x, _rb_y, _rb_text = [], [], []
            for _cj in sorted(_live_runs.glob("*-curation.json")):
                _d = _cj.name[:10]  # YYYY-MM-DD prefix
                _ts = pd.Timestamp(_d)
                if _ts < totals.index[0] or _ts > totals.index[-1]:
                    continue
                _val = totals.asof(_ts)
                if pd.isna(_val):
                    continue
                _rb_x.append(_ts)
                _rb_y.append(float(_val))
                _rb_text.append(_rebalance_popup(_cj))
            if _rb_x:
                fig.add_trace(
                    go.Scatter(x=_rb_x, y=_rb_y, mode="markers", name="Rebalance",
                               marker=_REBALANCE_MARKER,
                               hovertext=_rb_text,
                               hoverlabel={"align": "left", "bgcolor": "white",
                                           "bordercolor": "#7c2d12"},
                               hovertemplate="<b>Executed %{x|%Y-%m-%d}</b>"
                                             "<br>portfolio $%{y:,.0f}<br>%{hovertext}"
                                             "<extra></extra>",
                               legend="legend", legendrank=6),
                    row=1, col=1,
                )
        # Benchmark overlays normalized to the portfolio's starting value.
        # SPY-style benchmarks are rendered green and dotted so they're
        # visually distinct from the portfolio (blue on live, blue on
        # legacy backtest) and the buy-and-hold counterfactual (orange).
        if benchmarks and len(totals) > 1:
            benchmark_curves = _fetch_benchmark_curves(
                benchmarks, totals.index[0], totals.index[-1], float(totals.iloc[0]),
            )
            _bench_color = "#10b981" if is_live else "#66c266"
            _bench_dash = "dot" if is_live else "dash"
            for b, curve in benchmark_curves.items():
                fig.add_trace(
                    go.Scatter(x=curve.index, y=curve.values, mode=_ts_mode,
                               name=f"{b} (rescaled)",
                               line={"width": 1.5, "color": _bench_color, "dash": _bench_dash},
                               legend="legend", legendrank=4),
                    row=1, col=1,
                )
        # Constant-rate reference curves: dotted lines showing what the
        # thesis baseline portfolio would be worth at each profile-specified
        # growth rate (default 0.5% / 1% / 1.5% per week) from day zero. Live
        # dashboard only — anchored at the thesis-baseline date.
        if is_live and len(snaps) > 0:
            anchor_date = snaps["date"].min()
            anchor_value = float(totals.iloc[0])
            ref_dates = pd.to_datetime(totals.index)
            # Profile-driven rates (percent/week) -> fractions, slowest first.
            ref_rates = sorted(r / 100.0 for r in load_dashboard_guides())
            # Grey ramp light (slow) -> dark (fast); single rate -> mid grey.
            n_ref = len(ref_rates)
            ref_shades = {}
            for i, rate in enumerate(ref_rates):
                frac = i / (n_ref - 1) if n_ref > 1 else 0.5
                g = round(0xCC + (0x44 - 0xCC) * frac)
                ref_shades[rate] = f"#{g:02x}{g:02x}{g:02x}"
            for _ref_idx, (rate, color) in enumerate(ref_shades.items()):
                days = (ref_dates - anchor_date).days
                ref_vals = anchor_value * (1 + rate) ** (days / 7.0)
                fig.add_trace(
                    go.Scatter(x=ref_dates, y=ref_vals, mode="lines",
                               name=f"{rate * 100:g}%/wk",
                               line={"width": 1, "color": color, "dash": "dot"},
                               legend="legend", legendrank=n_ref - _ref_idx),
                    row=1, col=1,
                )
        # Thesis buy-and-hold counterfactual (live only): if the day-1
        # /initialize-portfolio allocation had been bought and held with
        # no further trading, what would it be worth on each subsequent
        # date? Scaled so its day-1 value equals the live snapshots'
        # day-1 total (which has been rescaled for capital events, so the
        # comparison is apples-to-apples in dollar terms). Last in the
        # add_trace order so it sits at the bottom of the legend column.
        if is_live and len(totals) > 1 and thesis_baseline_path \
                and Path(thesis_baseline_path).exists():
            try:
                _tb = json.loads(Path(thesis_baseline_path).read_text())
                _holdings = {t: float(v.get("shares", 0.0))
                             for t, v in _tb.get("holdings", {}).items()}
                _bh = _thesis_buy_hold_curve(
                    _holdings, totals.index[0], totals.index[-1], float(totals.iloc[0]),
                )
                if _bh is not None:
                    # Compose the thesis day-1 ticker breakdown as an
                    # HTML fragment for the annotation that goes below
                    # plot 1 (added after fig.update_layout below).
                    _alloc = _tb.get("allocations_usd", {}) or {}
                    _total_alloc = sum(v for v in _alloc.values() if v) or 1.0
                    _ranked = sorted(
                        ((t, v) for t, v in _alloc.items() if v and v > 0),
                        key=lambda kv: -kv[1],
                    )
                    _bh_alloc_html = ",  ".join(
                        f"{t} {v / _total_alloc * 100:.0f}%"
                        for t, v in _ranked
                    )
                    fig.add_trace(
                        go.Scatter(x=_bh.index, y=_bh.values, mode="lines",
                                   name="Buy-and-hold",
                                   line={"width": 1.8, "color": "#d97706"},
                                   legend="legend", legendrank=5),
                        row=1, col=1,
                    )
            except (OSError, ValueError):
                pass
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
            lambda t: ticker_wave.get(t, "general_markets")
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
        ticktext_3 = [_ticker_label(t, ticker_wave) for t in tickers_in_chart]
        # Group tickers by wave and emit one Bar trace per wave so the
        # legend matches chart 4 (wave colors and labels), not a
        # per-ticker spaghetti. Categorical x-axis order is set
        # explicitly so the bars still read in weight-descending order.
        latest_with_wave = latest_weights.copy()
        latest_with_wave["wave_bucket"] = latest_with_wave["ticker"].map(
            lambda t: ticker_wave.get(t, "general_markets")
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
                       name="concentration_cap",
                       legend="legend7",
                       hoverinfo="skip", showlegend=True),
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
    if R_TRADE_TABLE is not None:
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
                lambda t: ticker_wave.get(t, "general_markets")
            )
            _tickers_in_chart5 = _latest_now["ticker"].tolist()
            _ticktext_5 = [_ticker_label(t, ticker_wave) for t in _tickers_in_chart5]
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
        # snapshots.csv accumulates every ticker ever held, so over a long
        # window the bar-per-ticker x-axis crowds into unreadable labels.
        # Keep the informative ends — the biggest winners and biggest losers —
        # and collapse the near-zero middle into one labelled spacer bar. The
        # total-gain annotation below still sums ALL tickers, so the headline
        # is unaffected; only the display is trimmed.
        _TOP_N = 7
        _SPACER = "⋯"  # horizontal ellipsis; safe as a fake ticker key
        if len(gain_items) > 2 * _TOP_N + 2:
            _hidden = len(gain_items) - 2 * _TOP_N
            plot_items = gain_items[:_TOP_N] + [(_SPACER, 0.0)] + gain_items[-_TOP_N:]
        else:
            _hidden = 0
            plot_items = gain_items
        gain_tickers = [t for t, _ in plot_items]
        gain_values = [v for _, v in plot_items]
        ticktext_4 = [
            f"⋯<br><sup>{_hidden} more</sup>" if t == _SPACER
            else _ticker_label(t, ticker_wave)
            for t in gain_tickers
        ]
        # Color positive bars green, negative red so a glance reads winners
        # vs losers without consulting the y-axis number; the spacer bar is
        # transparent (zero height, just a visual break for the hidden middle).
        bar_colors = [
            "rgba(0,0,0,0)" if t == _SPACER
            else ("#2ca02c" if v >= 0 else "#d62728")
            for t, v in plot_items
        ]
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
        _total_init = sum(gain_by_ticker.values())
        _chart5_prefix = f"{R_GAIN_INIT}. Cumulative $ gain since {_chart5_anchor}"
        fig.layout.annotations[R_GAIN_INIT - 1].update(
            text=fig.layout.annotations[R_GAIN_INIT - 1].text.replace(
                _chart5_prefix,
                f"{_chart5_prefix} (total: ${_total_init:+,.0f})",
            )
        )

    # $ by asset class over time and $ by wave over time. Both
    # roll up the per-ticker per-day $ values from snapshots.csv. Each
    # ticker contributes to exactly one bucket in each chart, so the sum
    # of all lines in either chart equals the portfolio total.
    if snap_path.exists():
        snaps_full = pd.read_csv(snap_path, parse_dates=["date"])
        snaps_full["asset_bucket"] = snaps_full["ticker"].map(
            lambda t: ASSET_CLASS_BUCKET.get(TICKER_ASSET_CLASS.get(t, "equity"), "equities")
        )
        snaps_full["wave_bucket"] = snaps_full["ticker"].map(
            lambda t: ticker_wave.get(t, "general_markets")
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
        template="seaborn",
        height=308 * n_rows,
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
    )
    fig.update_yaxes(title_text="$", row=R_PORTFOLIO, col=1)
    fig.update_yaxes(title_text="portfolio %", row=R_REC_WAVE, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="portfolio %", row=R_LATEST_WEIGHTS, col=1, tickformat=".0%")
    if R_ACTUAL_WEIGHTS is not None:
        fig.update_yaxes(title_text="portfolio %", row=R_ACTUAL_WEIGHTS, col=1, tickformat=".0%")
    fig.update_yaxes(title_text="$ gain", row=R_GAIN_INIT, col=1, zeroline=True,
                     zerolinewidth=1, zerolinecolor="#888")
    fig.update_yaxes(title_text="$", row=R_ASSET_USD, col=1, tickformat="$,.0f")
    fig.update_yaxes(title_text="$", row=R_WAVE_USD, col=1, tickformat="$,.0f")
    fig.update_yaxes(title_text="turnover (%)", row=R_TURNOVER, col=1, rangemode="tozero")

    # Apply the padded snapshots-derived range to every time-series
    # subplot so data points don't sit flush against the axis edges
    # and all time-series charts share the same visual window. Charts 3
    # (latest weights) and 4 (gain bars) are bar charts with categorical
    # x-axes so the range setter is a no-op there.
    if xrange is not None:
        xrange_rows = [R_PORTFOLIO, R_TURNOVER, R_REC_WAVE,
                       R_ASSET_USD, R_WAVE_USD]
        for r in xrange_rows:
            fig.update_xaxes(range=list(xrange), row=r, col=1)

    # Thesis buy-and-hold caption: a small two-line text block placed
    # in the gap just below plot 1, listing the day-1 thesis composition
    # that the orange buy-and-hold curve is built from. Live mode only;
    # the trace name itself reads just "Buy-and-hold" so the legend
    # stays compact. The y position uses _row_top math for the default
    # plotly layout; the custom-layout block below reaches in and
    # repositions this annotation against the per-row pixel domains.
    _bh_anno_idx: "int | None" = None
    if is_live and _bh_alloc_html:
        # Single-line caption: bold header followed by the inline ticker
        # breakdown, nudged ~2ex below plot 1's bottom so it clears the
        # x-axis tick labels.
        fig.add_annotation(
            name="bh_composition",
            text=(f"<b>Buy-and-hold portfolio:</b>  "
                  f"<span style='color:#555;'>{_bh_alloc_html}</span>"),
            xref="paper", yref="paper",
            x=0.0, xanchor="left", xshift=48,  # ~6ex right of the y-axis
            y=_row_top(R_PORTFOLIO) - _row_h - 0.019, yanchor="top",
            showarrow=False,
            font=dict(size=12, color="#222"),
            align="left",
        )
        _bh_anno_idx = len(fig.layout.annotations) - 1

    # CUSTOM SUBPLOT LAYOUT: override plotly's uniform vertical_spacing
    # so specific gaps between subplots can be widened or narrowed
    # independently. Each row's domain is computed from absolute pixel
    # sizes (row_heights + per-gap deltas) and converted to figure-
    # fraction coords. Subplot title annotations are repositioned to
    # sit just above each row's new top edge.
    if is_live:
        EX_PX = 8
        ROW_PX = 308
        DEFAULT_GAP_PX = 92  # roughly matches plotly auto-spacing at vs ~0.030
        # Extra ex of space ABOVE each row (positive widens gap above).
        # Row 2 (turnover) gets +5ex so the buy-and-hold caption below
        # plot 1 has room without crowding the next subplot's title.
        gap_extras_ex: dict[int, int] = {2: 5, 5: 2, 6: -2, 8: 2, 9: 2, 11: 3}
        row_sizes_px = [
            _table_px if i == R_TRADE_TABLE else ROW_PX
            for i in range(1, n_rows + 1)
        ]
        gap_sizes_px = [
            DEFAULT_GAP_PX + gap_extras_ex.get(i + 1, 0) * EX_PX
            for i in range(1, n_rows)
        ]
        total_h_custom = sum(row_sizes_px) + sum(gap_sizes_px)
        # Walk top-down to compute fractional [y_low, y_high] per row.
        cur_top = 1.0
        new_domains: list[tuple[float, float]] = []
        for i in range(n_rows):
            bot = cur_top - row_sizes_px[i] / total_h_custom
            # Clamp to [0, 1] to avoid sub-epsilon negative values from
            # float roundoff on the bottom row.
            new_domains.append((max(0.0, min(1.0, bot)), max(0.0, min(1.0, cur_top))))
            cur_top = bot
            if i < n_rows - 1:
                cur_top -= gap_sizes_px[i] / total_h_custom
        # Apply. Note: Table rows have no yaxis — only xy chart rows do —
        # so the yaxis index advances independently of the row index,
        # skipping any Table rows.
        title_offset_frac = 14 / total_h_custom
        yaxis_counter = 0
        for i, (y_lo, y_hi) in enumerate(new_domains, start=1):
            if i == R_TRADE_TABLE:
                for trace in fig.data:
                    if isinstance(trace, go.Table):
                        trace.domain.y = (y_lo, y_hi)
                        break
            else:
                yaxis_counter += 1
                yaxis_key = f"yaxis{'' if yaxis_counter == 1 else yaxis_counter}"
                fig.layout[yaxis_key].domain = (y_lo, y_hi)
            if i - 1 < len(fig.layout.annotations):
                fig.layout.annotations[i - 1].update(y=y_hi + title_offset_frac)
        fig.update_layout(height=int(total_h_custom))
        # Reposition per-chart legends using the new y_high values so
        # each legend sits at the top of its own chart, not at the
        # plotly auto-layout position that no longer matches.
        _row_to_yhi = {i: new_domains[i - 1][1] for i in range(1, n_rows + 1)}
        legend_updates: dict[str, dict] = {}
        for legend_name, row_idx in [
            ("legend", R_PORTFOLIO),
            ("legend5", R_REC_WAVE),
            ("legend7", R_LATEST_WEIGHTS),
            ("legend2", R_ASSET_USD),
            ("legend3", R_WAVE_USD),
        ]:
            if row_idx in _row_to_yhi:
                legend_updates[legend_name] = dict(y=_row_to_yhi[row_idx])
        fig.update_layout(**legend_updates)
        # Reposition the buy-and-hold composition caption to sit at the
        # bottom of plot 1's pixel-sized domain (default-layout math
        # used at add_annotation time no longer matches the custom row
        # heights set above).
        if _bh_anno_idx is not None and _bh_anno_idx < len(fig.layout.annotations):
            # 40px ≈ 5ex of clearance below plot 1's tick labels.
            _bh_y = new_domains[R_PORTFOLIO - 1][0] - 40.0 / total_h_custom
            fig.layout.annotations[_bh_anno_idx].update(y=_bh_y)

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
                    f"thesis baseline date ({cutoff.date()}).<br>"
                    f"Each row is one <code>/review-portfolio</code> run that "
                    "produced at least one watchlist change.</p>"
                    "<table style='border-collapse:collapse;font-size:14px;'>"
                    "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
                    "<th style='padding:4px 12px;'>Date</th>"
                    "<th style='padding:4px 12px;'>Adds</th>"
                    "<th style='padding:4px 12px;'>Removes</th></tr></thead>"
                    f"<tbody>{''.join(tbl_rows)}</tbody></table>"
                )
        except Exception:
            pass  # silently skip the section if the file is malformed

    # Section: the curator's actual WebSearch queries at each review, newest
    # first, as collapsible <details> blocks (latest open) — the news-visibility
    # record. Reads every archived live run from data/curator_runs/live/; runs
    # that predate query-capture (no search_terms) are skipped. Native <details>
    # gives the click-to-expand behavior with no JavaScript.
    live_search = ""
    if is_live:
        live_dir = Path("data/curator_runs/live")
        by_date: dict[str, list[str]] = {}
        if live_dir.exists():
            for f in sorted(live_dir.glob("*.json")):
                try:
                    cj = json.loads(f.read_text())
                except Exception:  # noqa: BLE001 - skip malformed archive files
                    continue
                terms = [str(t) for t in (cj.get("search_terms") or []) if str(t).strip()]
                if not terms:
                    continue
                d = str(cj.get("as_of_date") or f.stem)
                # If a date has more than one archived file, keep the richest.
                if d not in by_date or len(terms) > len(by_date[d]):
                    by_date[d] = terms
        runs = sorted(by_date.items(), key=lambda kv: kv[0], reverse=True)
        if runs:
            blocks = []
            for i, (d, terms) in enumerate(runs):
                chips = "".join(
                    "<span style='display:inline-block;background:#f0f3f7;"
                    "border:1px solid #dde;border-radius:12px;padding:2px 10px;"
                    f"margin:3px 4px 3px 0;font-size:13px;'>{_html.escape(t)}</span>"
                    for t in terms
                )
                open_attr = " open" if i == 0 else ""
                blocks.append(
                    f"<details{open_attr} style='margin:6px 0;max-width:900px;'>"
                    "<summary style='cursor:pointer;font-size:14px;font-weight:600;"
                    f"padding:4px 0;'>{_html.escape(d)} &mdash; {len(terms)} queries"
                    "</summary>"
                    f"<div style='margin:6px 0 12px;'>{chips}</div></details>"
                )
            live_search = (
                "<h2 style='margin-top:2em;'>Curator search terms</h2>"
                "<p style='font-size:14px;color:#555;max-width:780px;'>"
                "Every news query the curator ran at each review, captured verbatim "
                "from the agent's actual WebSearch tool calls. Click a review to "
                "expand; the most recent is open by default. (Reviews before "
                "query-capture was added are omitted.)</p>"
                + "".join(blocks)
            )

    # Funded-ticker quick links: each real holding (shares > 0 in today's
    # snapshot) links to its live Google Finance chart opened at the 1-year
    # window. Google Finance's quote URL honors ?window=1Y but needs the
    # exchange suffix (e.g. RKLB:NASDAQ, ITA:BATS), so map yfinance's exchange
    # code to Google's suffix; unknown codes fall back to a "<TICKER> ticker"
    # Google search (whose card can't be forced off its 1D default).
    ticker_links = ""
    if is_live and snap_path.exists():
        try:
            _s_tl = pd.read_csv(snap_path, parse_dates=["date"])
            _sl_tl = _s_tl[_s_tl["date"] == _s_tl["date"].max()]
            _funded = sorted(_sl_tl[_sl_tl["shares"] > 0]["ticker"].unique())
            if _funded:
                _gx = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ",
                       "NAS": "NASDAQ", "NYQ": "NYSE", "PCX": "NYSEARCA",
                       "ASE": "NYSEAMERICAN", "BTS": "BATS"}
                import yfinance as _yf

                def _chart_url(t: str) -> str:
                    try:
                        suffix = _gx.get(_yf.Ticker(t).info.get("exchange"))
                    except Exception:  # noqa: BLE001 - network/info failure
                        suffix = None
                    if suffix:
                        return f"https://www.google.com/finance/quote/{t}:{suffix}?window=1Y"
                    return f"https://www.google.com/search?q={t}+ticker"

                _chips = "".join(
                    f"<a href='{_html.escape(_chart_url(t))}' "
                    "target='_blank' rel='noopener' "
                    "style='display:inline-block;background:#f0f7f0;border:1px solid #cde0cd;"
                    "border-radius:12px;padding:3px 12px;margin:3px 6px 3px 0;font-size:14px;"
                    "font-weight:600;color:#0a7a3a;text-decoration:none;'>"
                    f"{_html.escape(t)} &#8599;</a>"
                    for t in _funded
                )
                ticker_links = (
                    "<h2 style='margin-top:2em;'>Live ticker charts</h2>"
                    "<p style='font-size:14px;color:#555;max-width:780px;'>"
                    "Each funded holding links to its live Google Finance chart.</p>"
                    f"<div style='margin:6px 0 12px;'>{_chips}</div>"
                )
        except Exception:  # noqa: BLE001 - skip if snapshot is malformed
            pass

    # Parameter-settings table: the exact optimizer knobs /review-portfolio uses
    # with real money, read from investor_profile.md. Mirrors the backtest
    # dashboard's table so the two pages read side by side. Live path has no
    # backtest window / starter, so only the optimizer + curation knobs appear.
    _lfm = load_financial_model()
    _live_param_rows = [
        ("Risk aversion (λ)", f"{_lfm['risk_aversion']:g}", ""),
        ("Lookback (μ/Σ estimation)", f"{_lfm['lookback_period']}", ""),
        ("Concentration cap (max weight)", f"{_lfm['concentration_cap']:.0%}", ""),
        ("Min trade size", f"{_lfm['min_trade_size_frac']:.0%} of portfolio",
         "smallest proposed trade; smaller positions are filtered out"),
        ("Rebalance cadence", f"{_lfm['rebalance_period']}", ""),
        ("Max watchlist size", f"{_lfm['max_watchlist_size']}", ""),
        ("Always-include anchors", ", ".join(_lfm["always_include"]) or "—",
         "permanent optimizer anchors, outside max_watchlist_size"),
        ("Risk-free rate", f"{_lfm['risk_free_rate']:.0%}", ""),
    ]
    _live_param_tr = "".join(
        f"<tr><td style='padding:5px 14px 5px 0;color:#555;white-space:nowrap;'>{_html.escape(k)}</td>"
        f"<td style='padding:5px 14px 5px 0;font-weight:600;'>{_html.escape(str(v))}</td>"
        f"<td style='padding:5px 0;color:#b45309;font-size:13px;'>{_html.escape(note)}</td></tr>"
        for k, v, note in _live_param_rows
    )
    live_params_html = (
        "<h2 style='margin:1.4em 0 0.3em;'>Parameter settings</h2>"
        "<p style='color:#555;max-width:780px;margin:0 0 0.6em;'>The optimizer knobs "
        "behind the recommendations below, read from <code>investor_profile.md</code>, "
        "the config <code>/review-portfolio</code> uses with real money.</p>"
        "<table style='border-collapse:collapse;font-size:14px;margin-bottom:1.2em;'>"
        f"<tbody>{_live_param_tr}</tbody></table>"
    )

    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Live dashboard</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1280px;margin:0 auto;padding:1em 1.5em;color:#222;}'
        'th,td{border-bottom:1px solid #eee;}</style>'
        '</head><body>'
        + _nav("index.html")
        + live_params_html
        + chart_html
        + ticker_links
        + live_curation
        + live_search +
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
        # Seed/starter tickers carry no curator wave_bucket, so resolve their wave from the explicit
        # _STARTER_WAVE_DEFAULTS override, then the ticker->wave map, and only then fall back to
        # general_markets. Without the TICKER_WAVE step a seed AI name like AMZN reads grey (general_markets)
        # in the Gantt + gain plots and its wave never enters the legend.
        open_periods[t] = (run_start, _STARTER_WAVE_DEFAULTS.get(t) or TICKER_WAVE.get(t, "general_markets"))

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
    # defaults = the canonical CBT run; every caller passes these explicitly, so they are
    # a safety net rather than a config surface.
    backtest_dir: str = "data/curator_runs/proto-mws16/_backtest",
    runs_dir: str = "data/curator_runs/proto-mws16",
    out_path: str = "docs/backtest_gkg_3yr_kimi.html",
    benchmarks: list[str] | None = None,
    config_note: str | None = None,
    heading: str = "Curator Backtest",
    acronym: str = "CBT",
    show_max_articles: bool = True,
    handoff_date: str | None = None,
    compare_backtest_dir: str | None = None,
    actual_csv: str | None = None,
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

    ``actual_csv`` (forward paper portfolios only, e.g. FT): path to the REAL
    ``data/snapshots.csv``. When given, the real portfolio's value is drawn on
    chart 1 next to the paper replay, rescaled to meet it on their first common
    date so the two are compared on RETURNS, not on absolute dollars (the real
    book started at a different size). The gap between the lines is execution
    drift: what following the recommendations would have produced vs what the
    user's actual holdings did.
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
    rebalance_dates: list[str] = []
    _cadence = "quarterly"          # cadence label for the copy below; overridden from _starter.json
    runs_starter = Path(runs_dir) / "_starter.json"
    if runs_starter.exists():
        _starter = json.loads(runs_starter.read_text())
        starter_tickers = _starter.get("starter_watchlist", [])
        rebalance_dates = _starter.get("as_of_dates", [])
        _cadence = _starter.get("rebalance_period", _cadence)
    periods, _ = _build_ticker_periods(runs_dir, starter_tickers, end)
    periods = list(periods)
    # always_include anchors (SPY/AGG/IAU) are permanently in the optimizer universe but sit OUTSIDE the
    # curator's watchlist, so _build_ticker_periods misses them (except SPY, which is also a starter).
    # Add them for the full window so the Gantt shows they're always available (solid overlay if funded).
    _anchor_wb = {"SPY": "general_markets", "AGG": "cashlike", "IAU": "cashlike", "BIL": "cashlike"}
    _have_tk = {p[0] for p in periods}
    for _anc in load_financial_model().get("always_include", []):
        if _anc not in _have_tk:
            periods.append((_anc, start, end, _anchor_wb.get(_anc, "cashlike")))

    # Per-(ticker, date) wave-bucket resolver built from the curation periods above. Charts 3/4/5 use
    # this so dollars are attributed to the wave the CURATOR assigned at that time (matching the plot-2
    # Gantt), not a static ticker->wave map. A re-added ticker (e.g. GOOGL: AI, then quantum after its
    # 2024-12 re-add) therefore credits each wave for the window it was held under that thesis.
    _periods_by_tk: dict[str, list[tuple[pd.Timestamp, pd.Timestamp, str]]] = {}
    for _tk, _s, _e, _wb in periods:
        _periods_by_tk.setdefault(_tk, []).append((_s, _e, _wb))
    for _v in _periods_by_tk.values():
        _v.sort(key=lambda x: x[0])

    def _bucket_at(tk: str, dt: pd.Timestamp) -> str:
        """Wave bucket active for `tk` on `dt` (curator's per-period label); static-map fallback."""
        for _s, _e, _wb in _periods_by_tk.get(tk, ()):
            if _s <= dt <= _e:
                return _wb
        return TICKER_WAVE.get(tk, "general_markets")

    def _most_recent_bucket(tk: str) -> str:
        """Bucket of `tk`'s latest period — the single color for its per-ticker bar in chart 3."""
        ps = _periods_by_tk.get(tk)
        return max(ps, key=lambda x: x[0])[2] if ps else TICKER_WAVE.get(tk, "general_markets")

    # Realized return numbers for the headline summary.
    final = float(totals.iloc[-1])
    cur_return = (final / initial) - 1.0
    fix_initial = float(baselines["fixed_total"].dropna().iloc[0]) if "fixed_total" in baselines else initial
    fix_final = float(baselines["fixed_total"].dropna().iloc[-1]) if "fixed_total" in baselines else initial
    fix_return = (fix_final / fix_initial) - 1.0
    # Headline buy-and-hold: CBT = equal-weight starter held forever (eq_total, its established headline);
    # CBS = the naive AAPL/GOOGL/AMZN equal-weight comparator (naive_total), matching its plot-2 blue curve.
    _bh_hdl = "naive_total" if (acronym == "CBS" and "naive_total" in baselines.columns) else "eq_total"
    bnh_initial = float(baselines[_bh_hdl].dropna().iloc[0]) if _bh_hdl in baselines else initial
    bnh_final = float(baselines[_bh_hdl].dropna().iloc[-1]) if _bh_hdl in baselines else initial
    bnh_return = (bnh_final / bnh_initial) - 1.0

    fig = make_subplots(
        rows=5, cols=1, vertical_spacing=0.06,
        row_heights=[0.22, 0.24, 0.13, 0.12, 0.17],
        subplot_titles=(
            "1. Realized portfolio value: curator vs baselines vs benchmark",
            "2. Watchlist composition over time — translucent = watchlisted, solid = funded by optimizer",
            "3. Cumulative $ gain per holding",
            "4. Cumulative $ gain per wave bucket",
            "5. Actual portfolio $ by wave over time",
        ),
    )
    # Style plots 1-5's subplot titles to match the HTML <h2> of the sections below (bold, #111, left, larger).
    # At this point fig.layout.annotations holds exactly the 5 subplot titles (nothing else added yet).
    for _st in fig.layout.annotations:
        _st.update(font={"size": 18, "color": "#111"}, x=0.0, xanchor="left", text=f"<b>{_st.text}</b>")

    # Chart 1: equity-curve race.
    fig.add_trace(
        go.Scatter(x=totals.index, y=totals.values, name="Curator-driven",
                   mode="lines", line={"color": "#d97706", "width": 2.5}),
        row=1, col=1,
    )
    # The user's REAL portfolio, when asked for (see actual_csv in the docstring). Rescaled to the paper
    # curve at their first common date, so both lines start together and every later gap is execution
    # drift rather than a difference in account size.
    if actual_csv and Path(actual_csv).exists():
        _act = pd.read_csv(actual_csv, parse_dates=["date"])
        _act = _act.groupby("date")["total_value"].first().sort_index()
        _act = _act[(_act.index >= start) & (_act.index <= end)]
        _anchor = totals.asof(_act.index[0]) if len(_act) else float("nan")
        if len(_act) > 1 and pd.notna(_anchor) and float(_act.iloc[0]) > 0:
            _scale = float(_anchor) / float(_act.iloc[0])
            fig.add_trace(
                go.Scatter(x=_act.index, y=(_act * _scale).values, name="Actual (your holdings)",
                           mode="lines", line={"color": "#111", "width": 1.8, "dash": "dot"},
                           hovertemplate="%{x|%Y-%m-%d}<br>$%{y:,.0f} (rescaled)<extra></extra>"),
                row=1, col=1,
            )
    # Orange box on the curator curve at each quarterly rebalance (one per
    # curator call). y = curator portfolio value as of that date (asof picks
    # the nearest snapshot at/before the quarter-end). This single trace both
    # plots the markers and carries the legend swatch; placed immediately
    # after "Curator-driven" so it sits just below it in the legend.
    # Per-square hover popup (built by the shared _rebalance_popup helper):
    # what the curator changed (adds/removes) and a one-sentence "why" from
    # that quarter's curation JSON. asof() places each marker on the nearest
    # snapshot at/before the quarter-end.
    # Split rebalance markers: watchlist CHANGED (non-empty adds/removes) -> a bigger RED square that
    # stands out; a no-change rebalance -> the small orange square. So the eye lands on the dates that
    # actually rotated the watchlist.
    # Markers sit on the EXECUTION date, not the decision date, mirroring the replay's own rule: snap the
    # decision to the last session at/before it, then advance t_update_days sessions (the day-0 deployment
    # is capital setup, so it is not lagged). A weekend decision therefore lands on the following Monday,
    # which is where the trade actually happened. A decision whose execution has not happened yet (today's
    # curation, filling next session) draws no marker until it does.
    _t_upd = int(load_backtest_config().get("t_update_days", 1))
    _idx = totals.index
    _rebal_x, _rebal_y, _rebal_text = [], [], []     # no-change rebalances
    _chg_x, _chg_y, _chg_text = [], [], []           # watchlist changed
    for _i, _d in enumerate(rebalance_dates):
        _ts = pd.Timestamp(_d)
        _pos = int(_idx.searchsorted(_ts, side="right")) - 1     # last session at/before the decision
        _epos = 0 if _pos < 0 else _pos + (0 if _i == 0 else _t_upd)
        if _epos > len(_idx) - 1:
            continue                                             # decided, not yet executed
        _x, _val = _idx[_epos], float(totals.iloc[_epos])
        _cj_path = Path(runs_dir) / f"{_d}-curation.json"
        _changed = False
        try:
            _cj = json.loads(_cj_path.read_text())
            _changed = bool(_cj.get("adds") or _cj.get("removes"))
        except Exception:  # noqa: BLE001
            pass
        _popup = _rebalance_popup(_cj_path)
        if str(_x.date()) != str(_ts.date()):        # weekend/lagged fill: name the decision date too
            _popup = f"decided {_ts.date()}<br>{_popup}"
        if _changed:
            _chg_x.append(_x); _chg_y.append(_val); _chg_text.append(_popup)
        else:
            _rebal_x.append(_x); _rebal_y.append(_val); _rebal_text.append(_popup)
    fig.add_trace(
        go.Scatter(x=_rebal_x, y=_rebal_y, mode="markers", name="Rebalanced (no change)",
                   marker=_REBALANCE_MARKER,
                   hovertext=_rebal_text,
                   hoverlabel={"align": "left", "bgcolor": "white",
                               "bordercolor": "#7c2d12"},
                   hovertemplate="<b>Rebalanced %{x|%Y-%m-%d}</b>"
                                 "<br>portfolio $%{y:,.0f}<br>%{hovertext}"
                                 "<extra></extra>"),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(x=_chg_x, y=_chg_y, mode="markers", name="Watchlist changed",
                   marker={"symbol": "square", "size": 16, "color": "#dc2626",
                           "line": {"width": 1.5, "color": "white"}},
                   hovertext=_chg_text,
                   hoverlabel={"align": "left", "bgcolor": "white", "bordercolor": "#7f1d1d"},
                   hovertemplate="<b>Watchlist changed, executed %{x|%Y-%m-%d}</b>"
                                 "<br>portfolio $%{y:,.0f}<br>%{hovertext}"
                                 "<extra></extra>"),
        row=1, col=1,
    )
    # Blue benchmark curve. CBS = the naive AAPL/GOOGL/AMZN equal-weight buy-and-hold (naive_total) -- a
    # "what a typical investor might just hold" comparator. CBT keeps its equal-weight starter buy-and-hold
    # headline (eq_total).
    _bh_col = "naive_total" if (acronym == "CBS" and "naive_total" in baselines.columns) else "eq_total"
    _bh_name = "AAPL/GOOGL/AMZN equal-weight (buy-and-hold)" if _bh_col == "naive_total" else "Buy-and-hold"
    if _bh_col in baselines.columns and baselines[_bh_col].notna().any():
        _bh = baselines.dropna(subset=[_bh_col])
        fig.add_trace(
            go.Scatter(x=_bh["date"], y=_bh[_bh_col],
                       name=_bh_name,
                       mode="lines", line={"color": "#3b82f6", "width": 1.8}),
            row=1, col=1,
        )
    for b, curve in bench_curves.items():
        fig.add_trace(
            go.Scatter(x=curve.index, y=curve.values, name=f"{b} benchmark",
                       mode="lines", line={"color": "#10b981", "width": 1.5, "dash": "dot"}),
            row=1, col=1,
        )
    if acronym == "CBS":
        # CBS spans a narrow range ($50-58K), so a linear axis reads cleanly; CBT's $50K->$millions needs log.
        fig.update_yaxes(title_text="portfolio value ($)", row=1, col=1,
                         tickprefix="$", separatethousands=True)
    else:
        fig.update_yaxes(
            title_text="portfolio value ($)", row=1, col=1,
            type="log",
            tickvals=[10000, 30000, 100000, 300000, 1000000],
            ticktext=["$10K", "$30K", "$100K", "$300K", "$1M"],
        )
    # Align plot 2's (equity curve, row 1) x-axis with the [start, end] range used by the other time-axis
    # subplots (row 2 Gantt, row 5 wave-area) so dates — and the handoff line — line up vertically down the stack.
    fig.update_xaxes(range=[start, end], row=1, col=1)
    # A handoff marker only means something when there IS backtest news to the left of it. FT's handoff
    # equals its start date (forward-only), so the line and the 'backtest <- | -> forward news' label
    # would sit on the y-axis describing a side of the chart that does not exist: suppress it there.
    _show_handoff = bool(handoff_date) and pd.Timestamp(handoff_date) > start
    if _show_handoff:   # CBS: mark the backtest -> forward news handoff on the equity curve. add_vline's own
        # annotation breaks on a datetime axis (it averages Timestamps), so draw the line + label separately.
        fig.add_vline(x=pd.Timestamp(handoff_date), line={"dash": "dot", "color": "#888", "width": 1.5},
                      row=1, col=1)
        fig.add_annotation(x=pd.Timestamp(handoff_date), y=1.0, yref="y domain", yanchor="bottom",
                           xanchor="center", xshift=9, text="backtest ← | → forward news", showarrow=False,
                           font={"size": 11, "color": "#666"}, row=1, col=1)
        # Plots 3 (Gantt) and 6 (wave-area) get the same marker, but drawn AFTER their traces exist (below,
        # once every subplot axis is established) — add_vline here, before those rows are populated, would
        # silently fall back onto row 1's x-axis.

    # Chart 2: watchlist Gantt. One row per ticker, color = wave_bucket.
    # Sort tickers so the first-added is at the top, latest at the bottom.
    seen: list[str] = []
    for tk, _s, _e, _wb in periods:
        if tk not in seen: seen.append(tk)
    seen.reverse()  # so top of chart is first-added
    # always_include anchors (SPY/AGG/IAU) sink to the BOTTOM rows (highest y-index on the reversed axis),
    # so the curator's wave picks read at the top and the permanent safe-havens sit beneath them.
    _anchors = set(load_financial_model().get("always_include", []))
    seen = [t for t in seen if t not in _anchors] + [t for t in seen if t in _anchors]
    y_index = {tk: i for i, tk in enumerate(seen)}

    # FUNDED intervals per ticker: snapshot dates where the optimizer actually gave it weight
    # (value > 0). Grouped into contiguous runs so we can draw a solid inner bar over the translucent
    # watchlist span — the optimizer often funds only 1-2 of the (up to 5) watchlisted names.
    _all_dates = sorted(snaps["date"].unique())
    _didx = {d: i for i, d in enumerate(_all_dates)}
    _funded: dict[str, list[tuple]] = {}
    for _tk, _grp in snaps[snaps["value"] > 0].groupby("ticker"):
        _ix = sorted({_didx[d] for d in _grp["date"]})
        _runs: list[list[int]] = []
        for _i in _ix:
            if _runs and _i == _runs[-1][1] + 1:
                _runs[-1][1] = _i
            else:
                _runs.append([_i, _i])
        _funded[_tk] = [(_all_dates[a], _all_dates[b]) for a, b in _runs]

    # Each ticker's Gantt bar uses its wave's base color exactly so the bar hue matches the legend
    # swatch one-for-one. Watchlist membership = translucent width-14 bar; funded = solid width-7 inner.
    legend_seen: set[str] = set()
    for tk, p_start, p_end, wb in periods:
        color = WAVE_COLORS.get(wb, "#888888")
        show_legend = wb not in legend_seen
        legend_seen.add(wb)
        fig.add_trace(
            go.Scatter(
                x=[p_start, p_end], y=[y_index[tk], y_index[tk]], mode="lines",
                line={"color": color, "width": 14}, opacity=0.28,
                name=wb, legendgroup=wb, showlegend=show_legend, legend="legend5",
                hovertemplate=f"<b>{tk}</b> · watchlisted<br>{wb}<br>%{{x|%Y-%m-%d}}<extra></extra>",
            ),
            row=2, col=1,
        )
        for _fs, _fe in _funded.get(tk, []):
            _s0, _e0 = max(_fs, p_start), min(_fe, p_end)
            if _s0 <= _e0:
                fig.add_trace(
                    go.Scatter(
                        x=[_s0, _e0], y=[y_index[tk], y_index[tk]], mode="lines",
                        line={"color": color, "width": 7}, opacity=1.0,
                        legendgroup=wb, showlegend=False, legend="legend5",
                        hovertemplate=f"<b>{tk}</b> · FUNDED<br>{wb}<br>%{{x|%Y-%m-%d}}<extra></extra>",
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
    # by gain descending, each bar colored by its wave bucket (matching
    # charts 2/4/5) so a reader can see which waves drove the P&L.
    snaps_sorted = snaps.sort_values(["ticker", "date"])
    _gain_by_ticker: dict[str, float] = {}
    _gain_by_wave: dict[str, float] = {}          # chart 4: time-aware, built alongside per-ticker gains
    for _tk, _sub in snaps_sorted.groupby("ticker"):
        _sub = _sub.sort_values("date").reset_index(drop=True)
        _pc = _sub["price"].diff()
        _ps = _sub["shares"].shift(1)
        _daily = (_ps * _pc).fillna(0.0)
        _gain_by_ticker[_tk] = float(_daily.sum())
        # Chart 4 attribution: split this ticker's daily P&L across the wave bucket(s) it was held
        # under over time (curator's per-period label). Cash/bond/metal/crypto asset classes collapse
        # to "cashlike" regardless of wave, matching chart 5's display_bucket split.
        _ac = ASSET_CLASS_BUCKET.get(TICKER_ASSET_CLASS.get(_tk, "equity"), "equities")
        if _ac in ("bonds", "cash", "precious metals", "crypto"):
            _gain_by_wave["cashlike"] = _gain_by_wave.get("cashlike", 0.0) + float(_daily.sum())
        else:
            for _dt, _g in zip(_sub["date"], _daily):
                _wb = _bucket_at(_tk, _dt)
                _gain_by_wave[_wb] = _gain_by_wave.get(_wb, 0.0) + float(_g)
    _gain_items = sorted(_gain_by_ticker.items(), key=lambda kv: kv[1], reverse=True)
    _gain_tickers = [t for t, _ in _gain_items]
    _gain_values = [v for _, v in _gain_items]
    # Color each ticker bar by its MOST-RECENT curator bucket (so GOOGL reads quantum, its latest thesis).
    _bar_colors = [WAVE_COLORS.get(_most_recent_bucket(t), "#888888")
                   for t in _gain_tickers]
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
        tickangle=-90,
        row=3, col=1,
    )
    fig.update_yaxes(title_text="$ gain", tickformat="$,.0f",
                     zeroline=True, zerolinewidth=1, zerolinecolor="#888",
                     row=3, col=1)

    # Chart 4: cumulative $ gain per wave bucket. `_gain_by_wave` was accumulated in the per-ticker loop
    # above with TIME-AWARE attribution (each day's P&L credited to the wave the curator held it under),
    # so a re-bucketed ticker splits across waves. Cash/bonds/metals/crypto collapse to "cashlike" so the
    # bars line up with chart 5's bands. Sorted by gain descending.
    _wave_items = sorted(_gain_by_wave.items(), key=lambda kv: kv[1], reverse=True)
    _wave_keys = [w for w, _ in _wave_items]
    _wave_vals = [v for _, v in _wave_items]
    fig.add_trace(
        go.Bar(x=_wave_keys, y=_wave_vals,
               marker_color=[WAVE_COLORS.get(w, "#888888") for w in _wave_keys],
               name="$ gain by wave", showlegend=False,
               hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>"),
        row=4, col=1,
    )
    fig.update_xaxes(
        tickmode="array",
        tickvals=_wave_keys,
        ticktext=[WAVE_DISPLAY_LABEL.get(w, w) for w in _wave_keys],
        tickangle=-30,
        row=4, col=1,
    )
    fig.update_yaxes(title_text="$ gain", tickformat="$,.0f",
                     zeroline=True, zerolinewidth=1, zerolinecolor="#888",
                     row=4, col=1)

    # Chart 5: actual portfolio $ by wave over time. Stacked area on
    # linear y-axis: top edge = total portfolio value; each band's
    # thickness = that wave bucket's $ contribution.
    snaps_full = snaps.copy()
    snaps_full["asset_bucket"] = snaps_full["ticker"].map(
        lambda t: ASSET_CLASS_BUCKET.get(TICKER_ASSET_CLASS.get(t, "equity"), "equities")
    )
    # time-aware wave attribution (curator's per-period label at each date), matching charts 2 and 4
    snaps_full["wave_bucket"] = [
        _bucket_at(t, d) for t, d in zip(snaps_full["ticker"], snaps_full["date"])
    ]
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
    fig.update_yaxes(title_text="$", tickformat="$,.0f", row=5, col=1)
    fig.update_xaxes(range=[start, end], row=5, col=1)

    if _show_handoff:   # plots 3 (Gantt row 2) and 6 (wave-area row 5): draw the handoff marker now that every
        # subplot axis exists, so each shape resolves to its own x-axis (x2, x5) instead of falling back to x.
        for _r in (2, 5):
            fig.add_vline(x=pd.Timestamp(handoff_date), line={"dash": "dot", "color": "#888", "width": 1.5},
                          row=_r, col=1)

    fig.update_layout(
        template="seaborn",
        height=3700, margin={"t": 112 if config_note else 90, "b": 60, "l": 80, "r": 30},
        title={
            # Only the parameter note sits above plot 1 (curator model + optimizer/curator config).
            # Rendered only when the caller passes config_note, so a dashboard never mislabels the run
            # that made it. The old return-summary stats line was removed — those numbers live in the
            # plot 1 curves and the curation log below.
            "text": (
                f"<span style='font-size:13px;color:#111;font-weight:600;'>{_html.escape(config_note)}</span>"
                if config_note else ""
            ),
            "x": 0.5, "xanchor": "center",
        },
        # Per-row legends, each pinned to its chart's vertical position
        # in paper coords (1.0 = top, 0.0 = bottom).
        legend=dict(
            title_text="Portfolio value",
            xref="paper", x=1.02, yref="paper", y=0.98, yanchor="top",
        ),
        # Per-row legends. y values are overridden by the per-gap
        # spacing block below so the placeholders here just need to
        # be valid paper coords.
        legend5=dict(
            title_text="Wave bucket",
            xref="paper", x=1.02,
            yref="paper", y=0.0, yanchor="middle",
        ),
        legend4=dict(
            title_text="Wave bucket",
            xref="paper", x=1.02,
            yref="paper", y=0.0, yanchor="top",
        ),
    )

    # --- Per-gap vertical spacing override (5-row layout) ---
    # Plotly's make_subplots only supports a single uniform
    # vertical_spacing. Each chart-to-chart gap is shrunk to 50% of
    # plotly's default (111 px) except gap(4,5) which is shrunk only
    # 33% (149 px). Override each yaxis's domain and size the figure
    # in absolute pixels so individual subplot sizes are preserved
    # across edits.
    ROW_PX = [393, 443, 246, 246, 320]        # charts 1..5
    GAP_PX = [111, 111, 111, 149]              # 4 gaps; gap(4,5) wider
    _new_fig_h = sum(ROW_PX) + sum(GAP_PX)
    _tops, _bots = [], []
    _y = 1.0
    for _i, _h_px in enumerate(ROW_PX):
        _tops.append(_y)
        _y -= _h_px / _new_fig_h
        _bots.append(_y)
        if _i < len(GAP_PX):
            _y -= GAP_PX[_i] / _new_fig_h
    # Apply yaxis domains.
    for _i in range(5):
        _key = "yaxis" if _i == 0 else f"yaxis{_i + 1}"
        fig.layout[_key].domain = (max(0.0, _bots[_i]), min(1.0, _tops[_i]))
    # Reposition subplot-title annotations (~14px above each row top).
    _title_offset = 14 / _new_fig_h
    for _i in range(min(5, len(fig.layout.annotations))):
        fig.layout.annotations[_i].update(y=_tops[_i] + _title_offset)
    # Reposition per-row legends to the new geometry.
    # Rows: 0 = equity curve, 1 = Gantt, 2 = $ gain/holding,
    # 3 = $ gain/wave, 4 = wave area.
    fig.update_layout(
        height=_new_fig_h,
        legend5=dict(y=(_tops[1] + _bots[1]) / 2, yanchor="middle"),
        legend4=dict(y=_tops[4], yanchor="top"),
    )

    # Curation event log table at the bottom.
    log_html = ""
    if summary_path.exists():
        log = json.loads(summary_path.read_text())
        # Retries per rebalance: the reject-and-retry rounds the harness fired, read from each date's
        # curation JSON (_retries field; absent -> 0 for pre-retry runs). Loaded once, looked up by date.
        _retries_by_date: dict[str, int] = {}
        for _cf in Path(runs_dir).glob("2*-curation.json"):
            try:
                _cj = json.loads(_cf.read_text())
                _retries_by_date[str(_cj.get("as_of_date", _cf.name[:10]))] = int(_cj.get("_retries", 0))
            except Exception:  # noqa: BLE001
                pass
        rows = []
        _n_active = 0
        for ev in log:
            _adds, _removes, _rej = ev.get("adds") or [], ev.get("removes") or [], ev.get("rejections", 0)
            if not _adds and not _removes and not _rej:
                continue                                  # only show rebalances that actually did something
            _n_active += 1
            d = ev.get("date", "")
            adds = ", ".join(_adds) or "—"
            removes = ", ".join(_removes) or "—"
            # Rejections: show each dropped ticker + whether it was an add or a remove, with the
            # validator's reason as a hover tooltip. Falls back to the bare count for older summaries
            # that predate the rejections_detail field.
            _rej_detail = ev.get("rejections_detail") or []
            if _rej_detail:
                rej_cell = "<br>".join(
                    f"<span title='{_html.escape(str(r.get('reason', '')))}'>"
                    f"{_html.escape(str(r.get('ticker', '?')))} "
                    f"({_html.escape(str(r.get('action', '?')))})</span>"
                    for r in _rej_detail
                )
            else:
                rej_cell = str(_rej) if _rej else "—"
            _nret = _retries_by_date.get(str(d), 0)
            rows.append(
                f"<tr><td>{_html.escape(d)}</td>"
                f"<td style='color:#0a7a3a;'>{_html.escape(adds)}</td>"
                f"<td style='color:#b91c1c;'>{_html.escape(removes)}</td>"
                f"<td style='color:#9a6a00;'>{rej_cell}</td>"
                f"<td>{_nret or '—'}</td></tr>"
            )
        log_html = (
            "<h2 style='margin-top:2em;'>6. Curation log</h2>"
            f"<p style='color:#555;'>The {_n_active} of {len(log)} {_html.escape(_cadence)} curator calls "
            "that made a change (no-change rebalances are hidden). The <em>Rejections</em> column lists each "
            "add/remove the validator dropped as invalid, as <code>TICKER (action)</code> — hover for the "
            "reason (see "
            "<a href='https://github.com/joehahn/portfolio-wave-rider/blob/main/REFERENCE.md#cli-reference'>"
            "REFERENCE.md</a>). <em>Retries</em> = reject-and-retry rounds fired that week (the validator "
            "fed a rejection reason back and the curator re-proposed); blank = none.</p>"
            "<table style='border-collapse:collapse;width:100%;font-size:14px;'>"
            "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
            "<th style='padding:6px;'>Date</th>"
            "<th style='padding:6px;'>Adds</th>"
            "<th style='padding:6px;'>Removes</th>"
            "<th style='padding:6px;'>Rejections</th>"
            "<th style='padding:6px;'>Retries</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    # Search-terms history: the wave beats the curator worked at each rebalance,
    # as collapsible blocks in chronological order. Mirrors the live dashboard's
    # panel; data comes from each <date>-curation.json's search_terms (the wave
    # keywords behind that rebalance's news pool, preserved from the agent output).
    search_html = ""
    _st_blocks = []
    _runs = Path(runs_dir)
    if _runs.exists():
        for f in sorted(_runs.glob("*-curation.json")):
            try:
                cj = json.loads(f.read_text())
            except Exception:  # noqa: BLE001 - skip malformed files
                continue
            d = str(cj.get("as_of_date") or f.stem.replace("-curation", ""))
            adds = cj.get("adds") or []
            removes = cj.get("removes") or []
            overall = str(cj.get("rationale_overall") or "").strip()
            dec = []
            if overall:
                dec.append(f"<p style='margin:6px 0;color:#333;'><b>Rationale:</b> {_html.escape(overall)}</p>")
            for x in adds:
                ev = "".join(
                    f"<li><a href='{_html.escape(str(e.get('url', '')))}' target='_blank' rel='noopener'>"
                    f"{_html.escape(str(e.get('source', '') or e.get('url', '')))}</a>"
                    f"{(' &mdash; ' + _html.escape(str(e.get('summary', '')))) if e.get('summary') else ''}"
                    f"{(' (' + _html.escape(str(e.get('date', ''))) + ')') if e.get('date') else ''}</li>"
                    for e in (x.get("news_evidence") or []) if e.get("url") or e.get("source")
                )
                dec.append(
                    f"<div style='margin:5px 0;'><span style='color:#0a7a3a;font-weight:600;'>+ "
                    f"{_html.escape(str(x.get('ticker', '')))}</span> {_html.escape(str(x.get('rationale', '')))}"
                    + (f"<ul style='margin:3px 0 6px 1.2em;font-size:13px;color:#555;'>{ev}</ul>" if ev else "")
                    + "</div>"
                )
            for x in removes:
                dec.append(
                    f"<div style='margin:5px 0;'><span style='color:#b91c1c;font-weight:600;'>&minus; "
                    f"{_html.escape(str(x.get('ticker', '')))}</span> {_html.escape(str(x.get('rationale', '')))}</div>"
                )
            terms = [str(t) for t in (cj.get("search_terms") or []) if str(t).strip()]
            if not dec and not terms:
                continue
            chips = "".join(
                "<span style='display:inline-block;background:#f0f3f7;"
                "border:1px solid #dde;border-radius:12px;padding:2px 10px;"
                f"margin:3px 4px 3px 0;font-size:12px;'>{_html.escape(t)}</span>"
                for t in terms
            )
            _tag = (f"+{len(adds)}/&minus;{len(removes)}" if (adds or removes) else "no changes")
            _st_blocks.append(
                "<details style='margin:6px 0;max-width:900px;'>"
                "<summary style='cursor:pointer;font-size:14px;font-weight:600;"
                f"padding:4px 0;'>{_html.escape(d)} &mdash; {_tag} &middot; {len(terms)} queries</summary>"
                f"<div style='margin:6px 0 12px;'>{''.join(dec)}"
                "<div style='margin-top:8px;font-size:12px;color:#888;'>search terms:</div>"
                f"{chips}</div></details>"
            )
    if _st_blocks:
        # The whole section collapses under a clickable triangle (summary styled like the h2 headings); the
        # per-rebalance rows inside are their own nested <details>.
        search_html = (
            "<details id='curator-decisions' style='margin-top:2em;'>"
            "<summary style='cursor:pointer;font-size:1.5em;font-weight:bold;color:#111;'>"
            "16. Curator decisions &amp; search terms</summary>"
            f"<p style='color:#555;max-width:780px;'>One row per {_html.escape(_cadence)} rebalance. "
            "Click to expand the curator's overall rationale, each add/remove with its reason, the cited "
            "<code>news_evidence</code> links, and the wave keywords behind that rebalance's pool "
            "(GDELT&nbsp;GKG + Wayback, date-clean).</p>"
            + "".join(_st_blocks)
            + "</details>"
        )

    # Parameter-settings table, rendered just above chart 1 so a reader can
    # see exactly which optimizer / backtest knobs produced the curves below.
    # Values are the *effective* ones used by the replay: a backtest-only
    # override from investor_profile.md's `backtest` section wins if set,
    # otherwise the live financial_model / top-level value is used (this
    # mirrors the precedence in src/cli.py). By default no overrides are set,
    # so the backtest uses the same config /review-portfolio runs live; an
    # override (used to test a candidate config) is flagged so the reader can
    # see the published curve no longer matches the live config.
    _fm = load_financial_model()
    _bc = load_backtest_config()
    def _eff(override, live):
        return (override, True) if override is not None else (live, False)
    _ra, _ra_ov = _eff(_bc.get("risk_aversion"), _fm["risk_aversion"])
    _lb, _lb_ov = _eff(_bc.get("lookback_years"),
                       float(str(_fm["lookback_period"]).rstrip("y")))
    _cap, _cap_ov = _eff(_bc.get("concentration_cap"), _fm["concentration_cap"])
    _n_reb = sum(1 for _d in rebalance_dates
                 if start <= pd.Timestamp(_d) <= end)
    _param_rows = [
        ("Backtest window", f"{start.date()} → {end.date()}", ""),
        ("Rebalance cadence", f"{_cadence} ({_n_reb} curator calls)", ""),
        ("Starter watchlist", ", ".join(starter_tickers) or "—", ""),
        ("Initial capital", f"${initial:,.0f}", ""),
        ("Risk aversion (λ)", f"{_ra:g}",
         f"backtest-only override — live uses {_fm['risk_aversion']:g}" if _ra_ov else ""),
        ("optimizer_lookback_days", f"{round(_lb * 365)}",
         f"backtest-only override — live uses {_fm['optimizer_lookback_days']}" if _lb_ov else "μ/Σ estimation window"),
        ("Concentration cap (max weight)", f"{_cap:.0%}",
         f"backtest-only override — live uses {_fm['concentration_cap']:.0%}" if _cap_ov else ""),
        ("Min trade size", f"{_fm['min_trade_size_frac']:.0%} of portfolio",
         "smallest proposed trade; smaller positions are filtered out"),
        ("Max watchlist size", f"{_fm['max_watchlist_size']}", ""),
        ("news_lookback_days", f"{int(_fm['news_lookback_days'])}", ""),
        ("Curator model (LLM)", _bc.get("curator_model") or "—", ""),
        # max_articles is a backtest-retrieval cap; omitted for the bootstrap (CBS), whose pools vary in size.
        *([("Max articles / pool", f"{int(_bc['max_articles'])}", "")] if show_max_articles else []),
        ("Always-include anchors", ", ".join(_fm["always_include"]) or "—",
         "permanent optimizer anchors, outside max_watchlist_size"),
        ("Risk-free rate", f"{_fm['risk_free_rate']:.0%}", ""),
        ("Execution lag", f"{_bc['t_update_days']} trading day(s) after each rebalance signal", ""),
    ]
    _param_tr = "".join(
        f"<tr><td style='padding:5px 14px 5px 0;color:#555;white-space:nowrap;'>{_html.escape(k)}</td>"
        f"<td style='padding:5px 0;font-weight:600;'>{_html.escape(str(v))}</td></tr>"
        for k, v, _ in _param_rows
    )
    params_html = (
        "<h2 style='margin:1.4em 0 0.3em;'>Parameter settings</h2>"
        "<p style='color:#555;max-width:780px;margin:0 0 0.6em;'>The exact "
        "optimizer and backtest knobs behind the charts below, read from "
        "<code>investor_profile.md</code>. This is the same config "
        "<code>/review-portfolio</code> uses with real money.</p>"
        "<table style='border-collapse:collapse;font-size:14px;margin-bottom:1.2em;'>"
        f"<tbody>{_param_tr}</tbody></table>"
    )

    # Forward-test section (reporting only): in-sample vs out-of-sample split,
    # read from forward_split.json if curator_backtest wrote one. Empty when no
    # split date is configured, so the published dashboard is unchanged by default.
    forward_html = ""
    _fs_path = bd / "forward_split.json"
    if _fs_path.exists():
        try:
            _fs = json.loads(_fs_path.read_text())
        except Exception:  # noqa: BLE001
            _fs = None
        if _fs and _fs.get("in_sample"):
            _sd = _html.escape(str(_fs.get("split_date", "")))
            _fi, _fo = _fs["in_sample"], _fs["out_sample"]
            def _pf(x):  # fraction -> %
                return f"{x * 100:+.2f}%" if x is not None else "n/a"
            def _ppf(x):  # already in pp
                return f"{x:+.2f}pp" if x is not None else "n/a"
            _rows = (
                f"<tr><td style='padding:4px 14px 4px 0;'>In-sample (&le; {_sd})</td>"
                f"<td style='padding:4px 14px;'>{_fi['n_rebalances']}</td>"
                f"<td style='padding:4px 14px;'>{_pf(_fi['curator'])}</td>"
                f"<td style='padding:4px 14px;'>{_pf(_fi['buy_and_hold'])}</td>"
                f"<td style='padding:4px 0;'>{_ppf(_fi['lift_pp'])}</td></tr>"
                f"<tr><td style='padding:4px 14px 4px 0;'>Out-of-sample (&gt; {_sd})</td>"
                f"<td style='padding:4px 14px;'>{_fo['n_rebalances']}</td>"
                f"<td style='padding:4px 14px;'>{_pf(_fo['curator'])}</td>"
                f"<td style='padding:4px 14px;'>{_pf(_fo['buy_and_hold'])}</td>"
                f"<td style='padding:4px 0;'>{_ppf(_fo['lift_pp'])}</td></tr>"
            )
            _pending = ("" if _fs.get("populated") else
                        "<p style='color:#b45309;font-size:13px;max-width:780px;'>"
                        "No out-of-sample rebalances yet — extend the backtest "
                        "window's <code>end_date</code> past the split date to "
                        "populate the out-of-sample row.</p>")
            forward_html = (
                "<h2 style='margin:1.4em 0 0.3em;'>Forward test (overfitting check)</h2>"
                "<p style='color:#555;max-width:780px;margin:0 0 0.6em;'>In-sample "
                "rebalances fall on/before the split, where the curator LLM's "
                "training may already know the outcomes; out-of-sample rebalances "
                "fall strictly after it, genuinely unknowable when decided. A "
                "curator lift that holds out-of-sample is evidence of real signal; "
                "one that collapses toward buy-and-hold is evidence the in-sample "
                "result was hindsight.</p>"
                "<table style='border-collapse:collapse;font-size:14px;margin-bottom:0.4em;'>"
                "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
                "<th style='padding:5px 14px 5px 0;'>Segment</th>"
                "<th style='padding:5px 14px;'>Rebalances</th>"
                "<th style='padding:5px 14px;'>Curator</th>"
                "<th style='padding:5px 14px;'>Buy-and-hold</th>"
                "<th style='padding:5px 0;'>Lift</th></tr></thead>"
                f"<tbody>{_rows}</tbody></table>{_pending}"
            )

    # ---- summary metric cards (rendered just above Parameter settings) ----
    import math as _math
    _days = max((end - start).days, 1)
    _ann = (final / initial) ** (365.25 / _days) - 1.0 if initial > 0 and final > 0 else 0.0
    _maxdd = float((totals / totals.cummax() - 1.0).min())
    _calmar = _ann / abs(_maxdd) if _maxdd < 0 else float("nan")
    _ir = _tstat = _alpha = float("nan")
    _spy_curve = bench_curves.get("SPY")
    if _spy_curve is not None and len(totals) > 3:
        _c = totals.pct_change().dropna()
        _s = _spy_curve.reindex(totals.index).ffill().pct_change().reindex(_c.index)
        _act = (_c - _s).dropna()
        if len(_act) > 2 and _act.std() > 0:
            _ppy = len(_act) / (_days / 365.25)
            _ir = _act.mean() / _act.std() * _math.sqrt(_ppy)
            _tstat = _act.mean() / _act.std() * _math.sqrt(len(_act))
        _alpha = _ann - ((_spy_curve.iloc[-1] / _spy_curve.iloc[0]) ** (365.25 / _days) - 1.0)
    # LLM cost from the run's _log token usage (SDK-harness runs only; sonnet-5 $2/$10 per M tok)
    _tin = _tout = 0
    _model = ""
    _logdir = Path(runs_dir) / "_log"
    for _lf in (sorted(_logdir.glob("*-curator.json")) if _logdir.exists() else []):
        try:
            _lg = json.loads(_lf.read_text())
            _u = _lg.get("usage", {})
            _tin += _u.get("in", 0)
            _tout += _u.get("out", 0)
            _model = _lg.get("model", _model)
        except Exception:  # noqa: BLE001
            pass
    _cost = (_tin * 2 + _tout * 10) / 1e6 if (_tin or _tout) else None
    _n_add = _n_rem = 0
    if summary_path.exists():
        for _ev in json.loads(summary_path.read_text()):
            _n_add += len(_ev.get("adds") or [])
            _n_rem += len(_ev.get("removes") or [])
    _last_snap = snaps[snaps["date"] == snaps["date"].max()]
    _final_pos = sorted(_last_snap[_last_snap["value"] > 0]["ticker"].tolist())

    def _card(v, label):
        return (f'<div style="display:inline-block;min-width:118px;margin:0 1.4em 0.8em 0">'
                f'<b style="font-size:1.45em;color:#0b7285">{v}</b><br>'
                f'<span style="font-size:.78em;color:#555">{_html.escape(label)}</span></div>')
    _nn = lambda x: x == x  # noqa: E731 (not-NaN)
    cards_html = (
        '<h2 style="margin:1.4em 0 0.3em;">Summary</h2>'
        '<div style="margin:0.4em 0 0.6em">'
        + (_card(handoff_date, "backtest → WebSearch news handoff") if _show_handoff else "")
        + _card(f"{cur_return * 100:+.0f}%", "total return")
        + _card(f"{_ann * 100:+.0f}%", "annualized")
        + _card(f"{_maxdd * 100:.0f}%", "max drawdown")
        + _card(f"{_calmar:.2f}" if _nn(_calmar) else "n/a", "Calmar (ann / |DD|)")
        + _card(f"{_ir:+.2f}" if _nn(_ir) else "n/a", "Info Ratio vs SPY")
        + _card(f"{_tstat:+.1f}" if _nn(_tstat) else "n/a", "IR t-stat")
        + _card(f"{_alpha * 100:+.0f}%" if _nn(_alpha) else "n/a", "ann. alpha vs SPY")
        + _card(f"{_n_add} / {_n_rem}", "adds / removes")
        + _card(str(len(_final_pos)), "final positions")
        + _card(f"${_cost:,.2f}" if _cost is not None else "n/a",
                f"LLM cost ({_tin // 1000}K+{_tout // 1000}K tok)" if _cost is not None else "LLM cost")
        + '</div>'
    )

    # ---- Bootstrap-vs-backtest KPI table: the SAME canonical config scored two ways over the OVERLAPPING
    # window -- this run's bootstrap-news path (CBS) vs the reference backtest-news path (CBT). Only built when
    # the caller passes a comparison backtest dir (CBS mode); it doubles as the backtest-vs-forward overfit read.
    _cmp_html = ""
    if compare_backtest_dir and (Path(compare_backtest_dir) / "snapshots.csv").exists():
        _cbt_tot = (pd.read_csv(Path(compare_backtest_dir) / "snapshots.csv", parse_dates=["date"])
                    .groupby("date")["total_value"].first().sort_index())
        _lo, _hi = max(start, _cbt_tot.index[0]), min(end, _cbt_tot.index[-1])
        if _lo < _hi:
            _ov_spy = _fetch_benchmark_curves(["SPY"], _lo, _hi, 1.0).get("SPY")

            def _kpi_set(_t):   # KPIs for a total-value series restricted to the overlap [_lo, _hi]
                _t = _t[(_t.index >= _lo) & (_t.index <= _hi)].dropna()
                if len(_t) < 3:
                    return None
                _i0, _f0 = float(_t.iloc[0]), float(_t.iloc[-1])
                _d = max((_t.index[-1] - _t.index[0]).days, 1)
                _a = (_f0 / _i0) ** (365.25 / _d) - 1.0 if _i0 > 0 and _f0 > 0 else float("nan")
                _md = float((_t / _t.cummax() - 1.0).min())
                _dv = _t.diff().dropna()                      # Gain-to-Pain = net gain / sum of down-moves
                _pain = float(-_dv[_dv < 0].sum())
                _ir = _ts = _al = float("nan")
                if _ov_spy is not None:
                    _sp = _ov_spy.reindex(_t.index).ffill()
                    _c = _t.pct_change().dropna()
                    _ac = (_c - _sp.pct_change().reindex(_c.index)).dropna()
                    if len(_ac) > 2 and _ac.std() > 0:
                        _ir = _ac.mean() / _ac.std() * _math.sqrt(len(_ac) / (_d / 365.25))
                        _ts = _ac.mean() / _ac.std() * _math.sqrt(len(_ac))
                    if len(_sp) > 1 and float(_sp.iloc[0]) > 0:
                        _al = _a - ((float(_sp.iloc[-1]) / float(_sp.iloc[0])) ** (365.25 / _d) - 1.0)
                return {"total": _f0 / _i0 - 1.0, "ann": _a, "mdd": _md,
                        "calmar": _a / abs(_md) if _md < 0 else float("nan"),
                        "gpr": (_f0 - _i0) / _pain if _pain > 0 else float("nan"),
                        "ir": _ir, "tstat": _ts, "alpha": _al}

            _kb, _kc = _kpi_set(totals), _kpi_set(_cbt_tot)   # bootstrap-news path, backtest-news path
            if _kb and _kc:
                _pf = lambda x: f"{x * 100:+.0f}%" if x == x else "n/a"     # noqa: E731
                _nf = lambda x, d=2: f"{x:.{d}f}" if x == x else "n/a"      # noqa: E731
                _METR = [("Total return", "total", _pf), ("Annualized (CAGR)", "ann", _pf),
                         ("Max drawdown", "mdd", _pf), ("Calmar (ann / |DD|)", "calmar", _nf),
                         ("Gain-to-Pain", "gpr", _nf), ("Info Ratio vs SPY", "ir", _nf),
                         ("IR t-stat", "tstat", lambda x: _nf(x, 1)), ("Ann. alpha vs SPY", "alpha", _pf)]
                _trs = "".join(
                    f'<tr><td style="padding:3px 14px 3px 0">{_html.escape(_lab)}</td>'
                    f'<td style="padding:3px 22px 3px 0;text-align:right">{_fmt(_kb[_k])}</td>'
                    f'<td style="padding:3px 0;text-align:right">{_fmt(_kc[_k])}</td></tr>'
                    for _lab, _k, _fmt in _METR)
                _cmp_html = (
                    '<h2 style="margin:1.4em 0 0.3em;">Bootstrap vs backtest KPIs (same config)</h2>'
                    f'<p style="color:#555;max-width:840px;margin:0 0 .5em;">The <b>same canonical config</b> '
                    f'scored over the <b>overlapping</b> window ({_lo.date()} to {_hi.date()}): the '
                    '<b>bootstrap (CBS)</b> column is this run&#39;s live/forward-style news path, the '
                    '<b>backtest (CBT)</b> column is the GKG+Wayback backtest-news path. A wide gap flags '
                    'path-sensitivity / overfit. <b>Caveat:</b> the window is short and the CBS is still '
                    'seed-dominated (one curator add so far), so every figure is noisy and mostly reflects the '
                    'inherited seed, not the curator&#39;s forward skill; it firms up as forward quarters accrue.</p>'
                    '<table style="border-collapse:collapse;font-size:14px;">'
                    '<tr><th style="text-align:left;padding:3px 14px 3px 0">Metric</th>'
                    '<th style="text-align:right;padding:3px 22px 3px 0">Bootstrap (CBS)</th>'
                    '<th style="text-align:right;padding:3px 0">Backtest (CBT)</th></tr>'
                    f'{_trs}</table>')

    # Make the plots 1-5 subplot titles match the h2 section headers below (plots 6+): bold, left-justified,
    # instead of Plotly's default unbold-centered. The subplot titles are the annotations whose text starts
    # with a digit ("1." .. "5."); left-anchor them at the plot's left edge and bold via <b>.
    for _ann in fig.layout.annotations:
        if _ann.text and _ann.text[0].isdigit() and "<b>" not in _ann.text:
            _ann.update(x=0.0, xanchor="left", align="left",
                        text=f"<b>{_ann.text}</b>", font={"size": 14, "color": "#111"})

    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    # ================= extra figures (separate from the main grid) =================
    _EXTRA_CFG = {"displayModeBar": False}

    def _to_html(f):
        return f.to_html(full_html=False, include_plotlyjs=False, config=_EXTRA_CFG)

    # (a) Allocation over time: stacked-area of per-ticker weight %. The always_include safe-haven
    # anchors (SPY/AGG/IAU) are COLLAPSED into one band so the wave-pick vs defensive split is legible;
    # cash (unallocated) fills to 100%.
    _piv = snaps.pivot_table(index="date", columns="ticker", values="value", aggfunc="first").fillna(0.0)
    _tot = _piv.sum(axis=1).replace(0, float("nan"))
    _w = _piv.div(_tot, axis=0).fillna(0.0) * 100.0
    _cash = (100.0 - _w.sum(axis=1)).clip(lower=0)
    _anchors = [a for a in (load_financial_model().get("always_include") or []) if a in _w.columns]
    _picks = [c for c in _w.columns if c not in _anchors]
    _order = _w[_picks].sum().sort_values(ascending=False).index.tolist() if _picks else []
    _af = go.Figure()
    for _t in _order:
        _af.add_trace(go.Scatter(x=_w.index, y=_w[_t], name=str(_t), mode="lines",
                                 stackgroup="a", line={"width": 0.4}))
    if _anchors:
        _anchor_w = _w[_anchors].sum(axis=1)
        if float(_anchor_w.max()) > 0.1:
            _af.add_trace(go.Scatter(x=_w.index, y=_anchor_w, mode="lines", stackgroup="a",
                                     name="safe-haven (" + ".".join(_anchors) + ")",
                                     line={"width": 0.4, "color": "#9aa5b1"}))
    if float(_cash.max()) > 0.1:
        _af.add_trace(go.Scatter(x=_cash.index, y=_cash, name="cash", mode="lines",
                                 stackgroup="a", line={"width": 0.4, "color": "#ced4da"}))
    _af.update_layout(template="seaborn", height=400, margin={"t": 20, "l": 80, "r": 30},  # l=80 matches the
                      yaxis={"title": "% of portfolio", "range": [0, 100]},                # main fig so plot 7's
                      xaxis={"range": [start, end]})                                       # x-axis aligns with plot 6
    if _show_handoff:   # plot 7: mark the backtest -> forward news handoff (same dotted line as plots 2/3/6)
        _af.add_vline(x=pd.Timestamp(handoff_date), line={"dash": "dot", "color": "#888", "width": 1.5})

    # (b) Gains vs news source / vs keyword: each add's forward price return (add -> next remove / end),
    # bucketed by its news_evidence source and by the wave keyword that surfaced the evidence article.
    _price = snaps.pivot_table(index="date", columns="ticker", values="price", aggfunc="first").sort_index()
    _events = []
    for _f in sorted(Path(runs_dir).glob("*-curation.json")):
        try:
            _cj = json.loads(_f.read_text())
        except Exception:  # noqa: BLE001
            continue
        _dt = str(_cj.get("as_of_date") or _f.stem.replace("-curation", ""))
        for _x in _cj.get("adds", []):
            _events.append((_dt, _x.get("ticker"), "add", _x.get("news_evidence", [])))
        for _x in _cj.get("removes", []):
            _events.append((_dt, _x.get("ticker"), "remove", None))
    _events.sort(key=lambda e: e[0])

    def _fwd_ret(tkr, d0):
        if tkr not in _price.columns:
            return None
        d1 = next((_d for (_d, _t, _ac, _) in _events if _t == tkr and _ac == "remove" and _d > d0), None)
        p = _price[tkr].dropna()
        p0 = p[p.index >= pd.Timestamp(d0)]
        p1 = p[p.index <= (pd.Timestamp(d1) if d1 else end)]
        if p0.empty or p1.empty:
            return None
        return float(p1.iloc[-1] / p0.iloc[0] - 1.0)

    _kwmap = {}
    try:
        _cfg = json.loads((Path(__file__).resolve().parent.parent / "retrieval_config.json").read_text())
        for _wv, _ks in _cfg.get("wave_keywords", {}).items():
            for _k in _ks:
                _kwmap[_k.lower()] = _wv
    except Exception:  # noqa: BLE001
        pass
    from collections import defaultdict as _ddict
    # Bucket sources by the DOMAIN of each evidence `url` (authoritative + matches news_sources.md),
    # NOT the free-text `source` the curator typed (which is inconsistent: "The Verge" vs "theverge.com").
    def _url_domain(u):
        m = re.search(r"https?://([^/]+)", u or "")
        return re.sub(r":\d+$", "", re.sub(r"^www\.", "", m.group(1).lower())) if m else ""  # strip www + :port
    # Author lookup: _authors.json maps evidence URL -> byline (";"-separated co-authors), captured by the
    # post-run author refetch. Key it scheme/www-insensitively so https evidence URLs match http cache keys.
    def _url_key(u):
        return re.sub(r"^https?://(www\.)?", "", (u or "").lower()).rstrip("/")
    _author_by_key = {}
    try:
        for _k, _v in json.loads((Path(runs_dir) / "_authors.json").read_text()).items():
            if _v:
                _author_by_key[_url_key(_k)] = _v
    except Exception:  # noqa: BLE001
        pass
    # Supplement with bylines harvested directly from the run's in-dir pool files (the authoritative source;
    # covers runs whose _authors.json was never written). Any explicit _authors.json entry takes precedence.
    try:
        for _pf in sorted(Path(runs_dir).glob("*-pool.json")):
            for _a in json.loads(_pf.read_text()).get("articles", []):
                _u, _au = _a.get("url"), (_a.get("author") or "").strip()
                if _u and _au:
                    _author_by_key.setdefault(_url_key(_u), _au)
    except Exception:  # noqa: BLE001
        pass
    try:                                                  # gkg_pool.is_source_name for byline filtering
        import sys as _sys
        _sp = str(Path(__file__).resolve().parent.parent / "scripts")
        if _sp not in _sys.path:
            _sys.path.insert(0, _sp)
        import gkg_pool as _gp
    except Exception:  # noqa: BLE001
        _gp = None
    # lede_source (wayback / live / none) per evidence URL, read from the pool files — lets us compare the
    # forward gain of adds cited from clean-Wayback ledes vs the look-ahead-biased live-fallback ledes.
    _ledesrc_by_key = {}
    for _pf in Path(runs_dir).glob("*-pool.json"):
        try:
            _pj = json.loads(_pf.read_text())                       # {date}-pool.json is a dict {..., articles:[]}
            for _pa in (_pj.get("articles", []) if isinstance(_pj, dict) else _pj):
                if isinstance(_pa, dict) and _pa.get("url"):
                    _ledesrc_by_key.setdefault(_url_key(_pa["url"]), _pa.get("lede_source", "none"))
        except Exception:  # noqa: BLE001
            pass
    _src_ret, _kw_ret, _author_ret, _ledesrc_ret = _ddict(list), _ddict(list), _ddict(list), _ddict(list)
    for (_d, _t, _ac, _ev) in _events:
        if _ac != "add":
            continue
        # Attribute each add its ticker's realized optimizer $ P&L (same basis as charts 4/5), NOT the raw
        # add->end price return. The raw return ignores the optimizer's weighting + timing, so a pick the
        # optimizer MADE money on (e.g. held through the rise, trimmed the pullback) could read as a "loss"
        # -- contradicting charts 4/5 and the equity curve. $ P&L keeps every gain/loss plot on one basis.
        _r = _gain_by_ticker.get(_t)
        if _r is None:
            continue
        _srcs, _auths = set(), set()
        for _e in (_ev or []):
            _dom = _url_domain(_e.get("url", "")) or re.sub(r"[^a-z0-9]", "", (_e.get("source") or "").lower())
            if _dom:
                _srcs.add(_dom)
            _ls = _ledesrc_by_key.get(_url_key(_e.get("url", "")), "none")   # per evidence-article lede source
            _ledesrc_ret[_ls].append(_r)
            _byline = _author_by_key.get(_url_key(_e.get("url", "")))
            for _one in re.split(r"\s*;\s*", _byline or ""):
                _o = _one.strip()
                if _o and not (_gp and _gp.is_source_name(_o, _srcs)):   # drop source/wire/brand names
                    _auths.add(_o)
            _low = f"{_e.get('summary', '')} {_e.get('url', '')}".lower()
            for _k in _kwmap:
                if _k in _low:
                    _kw_ret[_k].append(_r)
        for _s in (_srcs or {"(no source)"}):
            _src_ret[_s].append(_r)
        for _a1 in (_auths or {"(no author)"}):   # byline-less adds bucket into "(no author)" (co-authors deduped per add)
            _author_ret[_a1].append(_r)

    # Seed (CBT inheritance) bucket: the day-0 seeded tickers (initial_weights, CBS-only) are NOT curator adds,
    # so their P&L -- often the bulk of a seeded CBS's losses (the inherited LHX-heavy portfolio) -- is otherwise
    # invisible in these add-attribution plots. Add it as one explicit "seed (CBT inheritance)" bar (total
    # seed-only P&L) so plots 8/10/11/13 reflect the real strategy P&L, not just the curator's own adds.
    _seed_tk = set()
    try:
        _sj = json.loads((Path(runs_dir) / "_starter.json").read_text())
        _seed_tk = {str(t).upper() for t in (_sj.get("initial_weights") or {})}
    except Exception:  # noqa: BLE001
        pass
    _added_tk = {e[1] for e in _events if e[2] == "add"}
    _seed_only = [t for t in _seed_tk if t not in _added_tk]
    if _seed_only:
        _seed_pnl = sum(_gain_by_ticker.get(t, 0.0) for t in _seed_only)
        for _dct in (_src_ret, _author_ret, _kw_ret, _ledesrc_ret):
            _dct["seed (CBT inheritance)"].append(_seed_pnl)
    # Inherited-watchlist bucket: tickers the optimizer FUNDED that this run neither added nor seeded --
    # they arrived on the starter watchlist from the seeding CBT run. Without this bucket their P&L (often
    # the bulk of a seeded run's gains) is invisible in every attribution plot, so the charts can read as
    # all-losses while the equity curve is up. Anchors are excluded: they come from the profile, not curation.
    _anchor_tk = {str(t).upper() for t in (load_financial_model().get("always_include") or [])}
    _inherited = [t for t, _v in _gain_by_ticker.items()
                  if t not in _added_tk and t not in _seed_tk and t not in _anchor_tk and abs(_v) > 1e-9]
    if _inherited:
        _inh_pnl = sum(_gain_by_ticker.get(t, 0.0) for t in _inherited)
        for _dct in (_src_ret, _author_ret, _kw_ret, _ledesrc_ret):
            _dct["inherited (CBT watchlist)"].append(_inh_pnl)

    # universe padding: show configured items that produced ZERO with a 0 bar (like the retriever DB) —
    # recognized desks (news_sources.md) for the source plots, all wave keywords for the keyword plot.
    _recognized = set()
    try:
        import sys as _sys
        _sp = str(Path(__file__).resolve().parent.parent / "scripts")
        if _sp not in _sys.path:
            _sys.path.insert(0, _sp)
        import gkg_pool as _gp
        _recognized = set(_gp.PREFERRED_DOMAINS) | set(_gp.MAJOR_DOMAINS)
    except Exception:  # noqa: BLE001
        pass

    def _zeros_details(names, noun):
        if not names:
            return ""
        return (f"<details style='margin:2px 0 1em;'><summary style='cursor:pointer;color:#868e96;"
                f"font-size:13px;'>+ {len(names)} {noun} with zero adds (configured but never cited)</summary>"
                f"<div style='color:#868e96;font-size:12px;margin:6px 0 0;max-width:900px;'>"
                f"{_html.escape(', '.join(sorted(names)))}</div></details>")

    def _attr_html(dct, xlab, labels=None, universe=None, noun="sources"):
        keys = set(dct) | set(universe or ())
        rows = sorted(((k, (sum(dct[k]) / len(dct[k]) if dct.get(k) else 0.0), len(dct.get(k, ()))) for k in keys),
                      key=lambda r: r[1])
        _lab = lambda k: (labels or {}).get(k, k)  # noqa: E731
        nonzero = [r for r in rows if r[2] > 0]           # cited: shown as bars
        zeros = [_lab(r[0]) for r in rows if r[2] == 0]   # never cited: collapsed
        if not nonzero and not zeros:
            return ""
        out = ""
        if nonzero:
            # LOG x-axis: bars can be negative (a losing pick), and log can't show <=0, so plot |value| on
            # a log axis with the sign carried by color (green +, red -), and hover the true signed value.
            # A zero-return row sinks to a small floor so log() stays defined. Mirrors the gains plots.
            _mags = [abs(r[1]) for r in nonzero]
            _floor = (min([m for m in _mags if m > 0] or [0.01])) * 0.5
            f = go.Figure(go.Bar(x=[m if m > 0 else _floor for m in _mags],
                                 y=[f"{_lab(r[0])} (n={r[2]})" for r in nonzero], orientation="h",
                                 marker_color=["#2b8a3e" if r[1] >= 0 else "#c92a2a" for r in nonzero],
                                 customdata=[r[1] for r in nonzero],
                                 hovertemplate="%{y}: $%{customdata:,.0f} signed<extra></extra>"))
            f.update_layout(template="seaborn", height=max(200, 24 * len(nonzero) + 110),
                            margin={"t": 20, "l": 230, "r": 30},
                            xaxis={"title": f"|{xlab}| (log; green = +, red = &minus;)", "type": "log"})
            out += _to_html(f)
        return out + _zeros_details(zeros, noun)

    _gain_src = _attr_html(_src_ret, "mean $ P&L per add", universe=_recognized, noun="sources")
    _gain_kw = _attr_html(_kw_ret, "mean $ P&L per add", universe=set(_kwmap), noun="keywords")
    _gain_author = _attr_html(_author_ret, "mean $ P&L per add", noun="authors")
    # Number of adds per author: raw n behind plot 13 (co-authors each counted; byline-less adds under "(no author)").
    _npa = ""
    _naa = sorted(((a, len(v)) for a, v in _author_ret.items() if v), key=lambda r: r[1])
    if _naa:
        _fna = go.Figure(go.Bar(x=[r[1] for r in _naa], y=[r[0] for r in _naa], orientation="h",
                                marker_color="#1f77b4"))
        _fna.update_layout(template="seaborn", height=max(200, 24 * len(_naa) + 110),
                           margin={"t": 20, "l": 230, "r": 30},
                           xaxis={"title": "number of adds crediting this author"})
        _npa = _to_html(_fna)

    # Gain PER ARTICLE vs source: normalize each source's TOTAL add-gain by how many articles it put into
    # the pools (its pool footprint) -> signal density. Pool counts come from the run's *-pool.json (GKG).
    _pool_src = {}   # source domain -> set of unique urls
    for _pf in sorted(Path(runs_dir).glob("2*-pool.json")):
        try:
            _pj = json.loads(_pf.read_text())
        except Exception:  # noqa: BLE001
            continue
        for _a in _pj.get("articles", []):
            _sd = re.sub(r"^www\.", "", str(_a.get("source", "")).lower())
            if _sd and _a.get("url"):
                _pool_src.setdefault(_sd, set()).add(_a["url"])
    # (b1) number of adds per source (how many adds cited each domain); zero-add sources collapsed.
    _nps = ""
    _nall = sorted(((s, len(_src_ret.get(s, ()))) for s in (set(_src_ret) | _recognized) if s != "(no source)"),
                   key=lambda r: r[1])
    _nrows = [r for r in _nall if r[1] > 0]
    _nzero = [r[0] for r in _nall if r[1] == 0]
    if _nrows or _nzero:
        if _nrows:
            _fn = go.Figure(go.Bar(x=[r[1] for r in _nrows], y=[r[0] for r in _nrows], orientation="h",
                                   marker_color="#1f77b4"))
            _fn.update_layout(template="seaborn", height=max(200, 24 * len(_nrows) + 110),
                              margin={"t": 20, "l": 230, "r": 30},
                              xaxis={"title": "number of adds citing this source"})
            _nps = _to_html(_fn)
        _nps += _zeros_details(_nzero, "sources")

    # (b2) gain PER ARTICLE: |value| on a LOG axis (sign shown by color, not position), so the wide
    # spread of densities is readable; biggest losers sink to the bottom (ascending signed sort).
    # The two synthetic buckets (seed / inherited) have no pool-article footprint, so a per-article rate is
    # undefined for them; plot their TOTAL P&L instead and label it, rather than dropping them and leaving
    # a chart of nothing but the curator's adds (which can be all-red while the portfolio is up).
    _SYNTH_BUCKETS = ("seed (CBT inheritance)", "inherited (CBT watchlist)")
    _gpa = {}
    for _s2, _rets2 in _src_ret.items():
        _n2 = len(_pool_src.get(_s2, ()))
        if _n2:
            _gpa[_s2] = sum(_rets2) / _n2
        elif _s2 in _SYNTH_BUCKETS:
            _gpa[_s2] = sum(_rets2)
    _gain_per_art = ""
    if _gpa:
        _rws = sorted(((k, v, len(_pool_src.get(k, ()))) for k, v in _gpa.items()), key=lambda r: r[1])
        _mags = [abs(r[1]) for r in _rws]
        _floor = (min([m for m in _mags if m > 0] or [0.01])) * 0.5   # keep log(0) safe
        _f = go.Figure(go.Bar(x=[m if m > 0 else _floor for m in _mags],
                              y=[f"{r[0]} ({r[2]} art.)" if r[2] else f"{r[0]} (total P&L)"
                                 for r in _rws], orientation="h",
                              marker_color=["#2b8a3e" if r[1] >= 0 else "#c92a2a" for r in _rws],
                              customdata=[r[1] for r in _rws],
                              hovertemplate="%{y}: $%{customdata:,.0f} per article signed<extra></extra>"))
        _f.update_layout(template="seaborn", height=max(240, 26 * len(_rws) + 120),
                         margin={"t": 20, "l": 230, "r": 30},
                         xaxis={"title": "|$ P&L per pool article| (log; green = +, red = &minus;)", "type": "log"})
        _gain_per_art = _to_html(_f)

    # 10. Gain PER ARTICLE by LEDE SOURCE (same metric as plot 9): total forward gain of adds cited from
    # each lede type / that type's pool-article footprint. Clean Wayback vs look-ahead-biased live-fallback.
    from collections import Counter as _Ctr
    _pool_ls = _Ctr(_ledesrc_by_key.values())
    _ls_lab = {"wayback": "clean Wayback lede", "live": "live-fallback lede", "none": "title only"}
    _lsr = sorted(((_ls_lab.get(_k, _k),
                    (sum(_v) / _pool_ls[_k]) if _pool_ls.get(_k) else sum(_v),
                    len(_v), _pool_ls.get(_k, 0))
                   for _k, _v in _ledesrc_ret.items()
                   if _pool_ls.get(_k) or _k in _SYNTH_BUCKETS), key=lambda r: r[1])
    _gain_ledesrc = ""
    if _lsr:
        # Same presentation as plots 9 and 11: |value| on a LOG axis so a wide spread stays readable, with
        # the sign carried by colour, not direction -- so a loss bar extends right like every other bar.
        _lmags = [abs(r[1]) for r in _lsr]
        _lfloor = (min([m for m in _lmags if m > 0] or [0.01])) * 0.5      # keep log(0) safe
        _lsf = go.Figure(go.Bar(
            x=[m if m > 0 else _lfloor for m in _lmags],
            y=[f"{r[0]} ({r[3]} art.)" if r[3] else f"{r[0]} (total P&L)" for r in _lsr],
            orientation="h", marker_color=["#2b8a3e" if r[1] >= 0 else "#c92a2a" for r in _lsr],
            customdata=[[r[1], r[2]] for r in _lsr],
            hovertemplate="%{y}: $%{customdata[0]:,.0f} signed (n=%{customdata[1]} adds)<extra></extra>"))
        _lsf.update_layout(template="seaborn", height=240, margin={"t": 20, "l": 210, "r": 40},
                           xaxis={"title": "|$ P&L per article| (log; green = +, red = &minus;)",
                                  "type": "log"})
        _gain_ledesrc = _to_html(_lsf)
    _ledesrc_note = ('<p style="color:#555;max-width:820px;margin:0 0 .4em;">Plot&nbsp;9&#39;s <b>$ P&amp;L per '
                     'article</b> (total $ P&amp;L / pool-article footprint), but bucketed by the LEDE SOURCE '
                     'of the cited evidence: clean, look-ahead-safe <b>Wayback</b> ledes vs the look-ahead-biased '
                     '<b>live-fallback</b> ledes (fetched from today&#39;s page). If live noticeably beats Wayback, '
                     'the bias is flattering the picks &mdash; a caution on fuller-mode backtest returns.</p>')

    _attr_note = ('<p style="color:#555;max-width:820px;margin:0 0 .4em;">Each add\'s realized <b>$ P&amp;L</b> '
                  '(the optimizer-weighted dollar gain/loss on that ticker &mdash; same basis as charts 4/5 and '
                  'the equity curve, NOT the raw add&rarr;end price return), bucketed by the '
                  '<code>news_evidence</code> source (by URL domain) and by the wave keyword that surfaced the '
                  'cited article. Answers which desks / search terms produced profitable picks. n = number of adds.</p>')
    _gpa_note = ('<p style="color:#555;max-width:820px;margin:0 0 .4em;">Plot&nbsp;8\'s total $ P&amp;L per source '
                 'divided by how many articles that source contributed to the pools (its footprint, shown as '
                 '<code>N&nbsp;art.</code>). Signal <b>density</b>: high = a source that produced gains from '
                 'few articles; near-zero/negative with many articles = low-signal, a block-list candidate. '
                 'Two bars are not per-article rates: <b>seed (CBT inheritance)</b> (the day-0 weights this run '
                 'started from) and <b>inherited (CBT watchlist)</b> (tickers the optimizer funded that this run '
                 'never added, arriving on the starter watchlist). Neither has a pool-article footprint, so they '
                 'show TOTAL $ P&amp;L and are labelled as such &mdash; without them the chart would show only the '
                 'curator&#39;s own adds, which can read as all-losses while the portfolio is up.</p>')
    _author_note = ('<p style="color:#555;max-width:820px;margin:0 0 .4em;">Plot&nbsp;8\'s realized <b>$ P&amp;L</b> '
                    'per add, but bucketed by the article <b>author</b> (byline extracted from each cited evidence URL) '
                    'instead of the source domain. Which reporters surfaced winning picks. n = number of adds; a '
                    'co-authored article credits each byline. Adds whose evidence URL has no captured byline are omitted.</p>')

    extra_html = (
        '<h2 style="margin:1.6em 0 0.2em;">7. Allocation over time</h2>' + _to_html(_af)
        + (('<h2 style="margin:1.6em 0 0.2em;">8. Gains vs news source</h2>' + _attr_note + _gain_src) if _gain_src else '')
        + (('<h2 style="margin:1.6em 0 0.2em;">9. P&amp;L per article vs news source</h2>' + _gpa_note + _gain_per_art) if _gain_per_art else '')
        + (('<h2 style="margin:1.6em 0 0.2em;">10. P&amp;L per article vs lede source</h2>' + _ledesrc_note + _gain_ledesrc) if _gain_ledesrc else '')
        + (('<h2 style="margin:1.6em 0 0.2em;">11. Gains vs search keyword</h2>' + _attr_note + _gain_kw) if _gain_kw else '')
    )
    # 12. Number of adds per source — the raw n behind the source-gain plots.
    _nps_html = (('<h2 style="margin:1.6em 0 0.2em;">12. Number of adds per source</h2>'
                  '<p style="color:#555;max-width:820px;margin:0 0 .4em;">How many adds cited each source '
                  '(by URL domain) as evidence &mdash; the raw <code>n</code> behind plots 8 and 9.</p>'
                  + _nps) if _nps else '')
    # 13. Gains vs author — moved here, just below "12. Number of adds per source".
    _author_html = (('<h2 style="margin:1.6em 0 0.2em;">13. Gains vs author</h2>' + _author_note + _gain_author)
                    if _gain_author else '')
    # 14. Number of adds per author — raw n behind plot 13 (sits just below it).
    _npa_html = (('<h2 style="margin:1.6em 0 0.2em;">14. Number of adds per author</h2>'
                  '<p style="color:#555;max-width:820px;margin:0 0 .4em;">How many adds each byline is credited '
                  'on (co-authors each counted; adds whose evidence URL carries no captured byline bucket into '
                  '<code>(no author)</code>) &mdash; the raw <code>n</code> behind plot 13.</p>'
                  + _npa) if _npa else '')
    # 15. Latest recommended portfolio % — the optimizer's target weights at the final rebalance (mirrors the
    # live dashboard's plot 4), each bar colored by its wave. Read from the run's recommendations.csv.
    _latest_rec_html = ""
    try:
        _rec = pd.read_csv(Path(backtest_dir) / "recommendations.csv")
        _lr = _rec[_rec["date"] == _rec["date"].max()]
        _lr = _lr[_lr["weight"] > 0.0005].sort_values("weight", ascending=False)
        if not _lr.empty:
            # Every ticker the curator has on the watchlist at this rebalance, funded or not. The unfunded
            # ones draw as zero-height bars, so the chart shows the whole eligible set and makes plain how
            # much of it the optimizer chose to skip. `_have_tk` is the watchlist set (anchors excluded).
            _last_dt = pd.Timestamp(str(_lr["date"].iloc[0])[:10])
            _funded = list(_lr["ticker"].astype(str))
            _unfunded = sorted({tk for tk, _s, _e, _wb in periods
                                if tk in _have_tk and _s <= _last_dt <= _e and tk not in set(_funded)})
            _tickers = _funded + _unfunded
            _weights = list(_lr["weight"]) + [0.0] * len(_unfunded)
            _rw = [_most_recent_bucket(str(t)) for t in _tickers]
            _rf = go.Figure(go.Bar(
                x=_tickers, y=_weights,
                marker_color=[WAVE_COLORS.get(w, "#888888") for w in _rw], customdata=_rw,
                text=[f"{w * 100:.0f}%" if w > 0.0005 else "unfunded" for w in _weights],
                textposition="outside", textfont={"size": 11},
                hovertemplate="%{x} (%{customdata})<br>%{y:.1%}<extra></extra>"))
            _rcap = float(_fm.get("concentration_cap", 0.25))
            _rf.add_hline(y=_rcap, line={"color": "#d62728", "width": 1.5, "dash": "dot"})
            _rf.update_layout(height=352, margin={"l": 60, "r": 20, "t": 10, "b": 96}, plot_bgcolor="white",
                              font={"size": 13}, showlegend=False, yaxis_title="portfolio %",
                              xaxis_title="Watchlist")
            _rf.update_yaxes(tickformat=".0%", gridcolor="#eee")
            # Two-line x labels: ticker on top, its wave bucket below (fed the most-recent-bucket map so the
            # sub-label matches each bar's wave color), same format as plot 4. Hover/click still key on the
            # plain ticker (the x value), so the 1Y/3Y popup is unaffected.
            _rwmap = dict(zip(_tickers, _rw))
            _rf.update_xaxes(tickmode="array", tickvals=_tickers,
                             ticktext=[_ticker_label(str(t), _rwmap) for t in _tickers])
            # Click-to-inspect: fetch each recommended ticker's 3-year daily closes (independently, so a
            # short-history IPO doesn't truncate the others) and embed them, so clicking a bar opens a modal
            # with that ticker's price line and a 1Y/3Y toggle (1Y is sliced client-side from the 3Y series).
            # Fetched at render; a yfinance failure just omits the popup.
            _tkhist: dict[str, dict] = {}
            for _tk in _tickers:
                try:
                    _hp = fetch_prices([_tk], period="3y")
                    _col = (_hp[_tk] if _tk in _hp.columns else _hp.iloc[:, 0]).dropna()
                    if len(_col) > 5:
                        _tkhist[_tk] = {"x": [d.strftime("%Y-%m-%d") for d in _col.index],
                                        "y": [round(float(v), 2) for v in _col.values]}
                except Exception:  # noqa: BLE001
                    pass
            _barid = "rec15chart"
            _bar_html = _rf.to_html(full_html=False, include_plotlyjs=False,
                                    config={"displayModeBar": False}, div_id=_barid)
            _modal = ""
            if _tkhist:
                _modal = (
                    '<div id="tkmodal" style="display:none;position:fixed;inset:0;'
                    'background:rgba(0,0,0,.45);z-index:9999;" '
                    "onclick=\"if(event.target===this)this.style.display='none'\">"
                    '<div style="background:#fff;max-width:780px;margin:6vh auto;padding:12px 16px 6px;'
                    'border-radius:8px;position:relative;">'
                    "<button onclick=\"document.getElementById('tkmodal').style.display='none'\" "
                    'style="position:absolute;top:6px;right:12px;border:none;background:none;'
                    'font-size:24px;cursor:pointer;color:#999;">&times;</button>'
                    '<div style="margin:2px 0 8px;"><button id="tkb1" onclick="_setTkYrs(1)">1Y</button>'
                    '<button id="tkb3" onclick="_setTkYrs(3)">3Y</button></div>'
                    '<div id="tkmodal_chart" style="width:100%;height:380px;"></div></div></div>'
                    '<script>var _TKHIST=' + json.dumps(_tkhist) + ';var _curTk=null,_curYrs=1;'
                    # slice the embedded 3Y series to the last `y` years so the y-axis auto-scales to the view
                    'function _tkslice(h,y){if(y>=3)return h;var L=new Date(h.x[h.x.length-1]);'
                    'var c=new Date(L);c.setFullYear(c.getFullYear()-y);var X=[],Y=[];'
                    'for(var i=0;i<h.x.length;i++){if(new Date(h.x[i])>=c){X.push(h.x[i]);Y.push(h.y[i]);}}'
                    'return{x:X,y:Y};}'
                    'function _tkdraw(){var h=_TKHIST[_curTk];if(!h)return;var d=_tkslice(h,_curYrs);'
                    'Plotly.newPlot("tkmodal_chart",[{x:d.x,y:d.y,type:"scatter",mode:"lines",'
                    'line:{color:"#1f77b4",width:1.6},'
                    'hovertemplate:"%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>"}],'
                    '{margin:{l:58,r:20,t:42,b:40},template:"seaborn",'
                    'title:{text:_curTk+": "+_curYrs+"-year price history",x:0.02,font:{size:16}},'
                    'yaxis:{title:"$",tickprefix:"$"}},{displayModeBar:false,responsive:true});'
                    'var on="background:#1f77b4;color:#fff;",off="background:#eee;color:#333;",'
                    'b="border:none;border-radius:4px;padding:3px 12px;margin-right:6px;cursor:pointer;font-size:13px;";'
                    'document.getElementById("tkb1").style.cssText=b+(_curYrs==1?on:off);'
                    'document.getElementById("tkb3").style.cssText=b+(_curYrs==3?on:off);}'
                    'window._showTkHist=function(tk){if(!_TKHIST[tk])return;_curTk=tk;_curYrs=1;'
                    'document.getElementById("tkmodal").style.display="block";_tkdraw();};'
                    'window._setTkYrs=function(y){_curYrs=y;_tkdraw();};'
                    '(function a(){var g=document.getElementById("' + _barid + '");'
                    'if(g&&g.on){g.on("plotly_click",function(ev){window._showTkHist(ev.points[0].x);});}'
                    'else setTimeout(a,150);})();</script>')
            _hint = (" Click any bar to open that ticker&#39;s price history (1Y / 3Y toggle)."
                     if _tkhist else "")
            # 15b. Position-size calculator: enter a portfolio $ and get the per-ticker $ and share counts
            # implied by the recommended weights. Pure client-side arithmetic on values embedded at render
            # (weight from the optimizer, last close from the same 3Y history the click-popup uses), so the
            # page stays a single static file with no backend.
            _calc = ""
            _crows = [(str(t), float(w), float(_tkhist[str(t)]["y"][-1]))
                      for t, w in zip(_lr["ticker"], _lr["weight"]) if str(t) in _tkhist]
            if _crows:
                _cdefault = int(round(float(totals.iloc[-1]) / 1000.0) * 1000) or 50000
                _asof_px = str(_tkhist[_crows[0][0]]["x"][-1])
                _calc = (
                    '<div style="margin:.9em 0 0;padding:12px 14px;border:1px solid #e5e7eb;border-radius:8px;'
                    'background:#fafafa;max-width:820px;">'
                    '<label style="font-size:14px;color:#333;">Portfolio to invest: $'
                    '<input id="pfcalc" type="number" min="0" step="1000" value="' + str(_cdefault) + '" '
                    'style="width:130px;padding:3px 6px;margin-left:4px;font-size:14px;"></label>'
                    '<span style="color:#777;font-size:13px;margin-left:10px;">shares at the '
                    + _asof_px + ' close; fractional shares assumed</span>'
                    '<table style="border-collapse:collapse;width:100%;font-size:14px;margin-top:8px;">'
                    '<thead><tr style="border-bottom:2px solid #ccc;text-align:left;">'
                    '<th style="padding:5px;">Ticker</th><th style="padding:5px;">Weight</th>'
                    '<th style="padding:5px;">Price</th><th style="padding:5px;">Invest $</th>'
                    '<th style="padding:5px;">Shares</th></tr></thead>'
                    '<tbody id="pfcalcbody"></tbody></table></div>'
                    '<script>var _PFROWS=' + json.dumps(_crows) + ';'
                    'function _pfcalc(){var v=parseFloat(document.getElementById("pfcalc").value)||0;'
                    'var h="",tot=0;for(var i=0;i<_PFROWS.length;i++){var r=_PFROWS[i],d=v*r[1];tot+=d;'
                    'h+="<tr style=\\"border-bottom:1px solid #eee;\\"><td style=\\"padding:5px;\\"><b>"+r[0]'
                    '+"</b></td><td style=\\"padding:5px;\\">"+(r[1]*100).toFixed(1)+"%</td>'
                    '<td style=\\"padding:5px;\\">$"+r[2].toFixed(2)+"</td>'
                    '<td style=\\"padding:5px;\\">$"+d.toLocaleString(undefined,{maximumFractionDigits:0})'
                    '+"</td><td style=\\"padding:5px;\\">"+(d/r[2]).toFixed(4)+"</td></tr>";}'
                    'h+="<tr style=\\"border-top:2px solid #ccc;\\"><td style=\\"padding:5px;\\"><b>total</b>'
                    '</td><td style=\\"padding:5px;\\">"+((tot/(v||1))*100).toFixed(1)+"%</td>'
                    '<td style=\\"padding:5px;\\"></td><td style=\\"padding:5px;\\"><b>$"'
                    '+tot.toLocaleString(undefined,{maximumFractionDigits:0})+"</b></td>'
                    '<td style=\\"padding:5px;\\"></td></tr>";'
                    'document.getElementById("pfcalcbody").innerHTML=h;}'
                    'document.getElementById("pfcalc").addEventListener("input",_pfcalc);_pfcalc();</script>')
            # Curation clock, live paper portfolios only (handoff_date is set for CBS/FT, not for the
            # finished CBT backtest, where a "next curation" would be meaningless).
            _clock = ""
            if handoff_date and rebalance_dates:
                _cad_days = {"weekly": 7, "biweekly": 14, "monthly": 30,
                             "quarterly": 91}.get(str(_cadence), 14)
                _last_cur = pd.Timestamp(str(rebalance_dates[-1])[:10])
                _next_cur = _last_cur + pd.Timedelta(days=_cad_days)
                _dn = (_next_cur - pd.Timestamp.today().normalize()).days
                _when = (f"{_dn} day{'s' if _dn != 1 else ''} from this refresh" if _dn > 0
                         else ("today" if _dn == 0 else f"overdue by {-_dn} day{'s' if _dn != -1 else ''}"))
                _clock = (f' <b>Most recent curation {_last_cur.date()}; the next is due '
                          f'{_next_cur.date()} ({_when})</b>, on the profile&#39;s {_cadence} cadence. '
                          f'Between curations the watchlist is fixed and only the weights move.')
            _latest_rec_html = (
                '<h2 style="margin:1.6em 0 0.2em;">15. Latest recommended portfolio %</h2>'
                f'<p style="color:#555;max-width:820px;margin:0 0 .4em;">The optimizer&#39;s target weights at the '
                f'final rebalance ({str(_lr["date"].iloc[0])[:10]}) &mdash; the allocation the curator strategy '
                f'would hold now. Bars at zero are the {len(_unfunded)} watchlisted ticker(s) the optimizer '
                f'left unfunded: the curator judged them worth watching, the math did not fund them this '
                f'rebalance. Each ticker&#39;s wave is labelled beneath it and sets the bar colour. '
                f'Red dotted line = the concentration_cap ({_rcap:.0%}).{_clock}{_hint}</p>'
                + _bar_html + _modal
                + ('<p style="color:#555;max-width:820px;margin:1em 0 .2em;"><b>Position sizes.</b> Enter what '
                   'you have to invest and the table gives the dollars and share count each funded ticker '
                   'implies at these weights. Recommendation only &mdash; nothing here places a trade.</p>'
                   if _calc else '') + _calc)
    except Exception:  # noqa: BLE001
        pass
    # Intro news description: CBS (handoff_date set) is a backtest-news -> live-news SPLICE; CBT is plain
    # backtest news. The old intro hardcoded the GKG+Wayback (backtest-only) description for both, which
    # misdescribed the CBS's whole reason to exist.
    if _show_handoff:
        _pool_line = (
            f'Before the {handoff_date} handoff (dotted line in chart&nbsp;2) each pool is the backtest&#39;s '
            f'GDELT&nbsp;GKG + Wayback pool (look-ahead-reduced); after it, the live WebSearch forward corpus, '
            f'the same news path <code>/review-portfolio</code> consumes with real money. This backtest-news '
            f'to live-news splice is the point of the CBS; see the <a href="retrieval_bootstrap.html">RBS</a> '
            f'for the news-side view of the same transition. ')
    elif handoff_date:      # forward-only run (FT): every pool is live WebSearch, no backtest side
        _pool_line = ('Each pool is the live WebSearch forward corpus for the preceding window &mdash; no '
                      'backtest news is used; see the <a href="retrieval_bootstrap.html">RBS</a> for the '
                      'news-side view. ')
    else:
        _pool_line = ('Each pool is built upstream from GDELT&nbsp;GKG + Wayback (look-ahead-reduced); browse '
                      'them in the <a href="pool_browser.html">pool browser</a>. ')
    # Forward-transition status (CBS only): how many rebalances postdate the handoff. 0 => the post-handoff
    # curve is price-extension of the frozen watchlist, NOT WebSearch-driven curation -- state that so the
    # segment past the dotted line isn't misread as "the forward regime". The "next due" date uses the median
    # rebalance gap so no cadence->days constant is hardcoded.
    _forward_note = ""
    if _show_handoff:
        _reb = (sorted(pd.Timestamp(x) for x in rebalance_dates) if rebalance_dates
                else sorted(pd.Timestamp(p.stem.replace("-curation", ""))
                            for p in Path(runs_dir).glob("*-curation.json")))
        _n_fwd = sum(1 for d in _reb if d > pd.Timestamp(handoff_date))
        if _reb and _n_fwd == 0:
            _gaps = pd.Series(_reb).diff().dropna()
            _gap = _gaps.median() if len(_gaps) else pd.Timedelta(days=14)
            _forward_note = (
                '<p style="color:#8a5a00;background:#fff8e6;border-left:3px solid #f0b429;'
                'padding:.5em .7em;max-width:780px;margin:.2em 0 .7em;font-size:14px;">'
                f'<b>Transition status:</b> no forward-news (WebSearch) rebalance has fired yet. The last '
                f'curation was {_reb[-1].date()} on backtest-tail news, so the curve to the right of the dotted '
                f'handoff line is price-extension of that frozen watchlist, not WebSearch-driven curation. The '
                f'first forward rebalance is due ~{(_reb[-1] + _gap).date()}.</p>')
        elif _reb and _n_fwd > 0:
            _forward_note = (
                '<p style="color:#555;max-width:780px;margin:.2em 0 .7em;font-size:14px;">'
                f'<b>Transition status:</b> {_n_fwd} forward-news (WebSearch) rebalance'
                f'{"s have" if _n_fwd != 1 else " has"} fired since the {handoff_date} handoff.</p>')

    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>{heading} ({acronym})</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5;}'
        'h1,h2{color:#111;}'
        'table{margin-top:0.5em;}'
        'th,td{border-bottom:1px solid #eee;}'
        '</style></head><body>'
        + _nav(Path(out_path).name) +
        f'<h1>{heading} ({acronym})</h1>'
        f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{start.date()} to {end.date()}</p>'
        f'<p style="color:#555;max-width:780px;">The watchlist-curator agent was called '
        f'{_html.escape(_cadence)} over the {start.date()} to {end.date()} window. '
        f'At each rebalance it read a date-clean news pool for the preceding '
        f'{_html.escape(_cadence)} window. ' + _pool_line +
        'The curator proposed adds and removes against the active watchlist; the optimizer then ran '
        'mean-variance on the revised watchlist. Each rebalance is marked by an '
        'orange square on the curator curve in chart 2; hover over one to see '
        'that rebalance\'s adds, removes, and the curator\'s rationale. '
        'The buy-and-hold curve below is '
        'the value of the initial portfolio (which never gets rebalanced or '
        'optimized) over time. The buy-and-hold portfolio has equal amounts of '
        f'<code>[{_html.escape(", ".join(starter_tickers))}]</code> and is held without any '
        'rebalancing across the window. '
        'Throughout the charts below: <code>general_markets</code> = defensive '
        'equity ETFs (broad-market / dividend / utilities / staples); '
        '<code>cashlike</code> = bonds + cash-equivalents + precious metals '
        '(e.g., AGG, BIL, IAU).</p>'
        + _forward_note
        + cards_html
        + _cmp_html
        + params_html
        + forward_html
        + chart_html          # plots 1-5 (equity curve first)
        + log_html            # 6. Curation log -- sits AFTER the charts it explains

        + extra_html
        + _nps_html
        + _author_html
        + _npa_html
        + _latest_rec_html
        + search_html
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
