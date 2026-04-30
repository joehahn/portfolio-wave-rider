"""Tests for the portfolio math functions.

Runs offline — tests that need a returns bundle build one in-process from
synthetic prices instead of calling yfinance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import portfolio


@pytest.fixture
def returns() -> dict:
    """Build a returns bundle from a synthetic 3-ticker price series."""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2023-01-03", periods=500, freq="B")
    daily = rng.normal(loc=[0.0005, 0.0003, 0.0007], scale=[0.012, 0.009, 0.018], size=(500, 3))
    prices = pd.DataFrame(
        100 * np.exp(daily.cumsum(axis=0)),
        index=dates,
        columns=["AAA", "BBB", "CCC"],
    )
    return portfolio.compute_returns(prices)


def test_compute_returns_shapes(returns: dict) -> None:
    assert set(returns["mean"].index) == {"AAA", "BBB", "CCC"}
    assert returns["log_returns"].shape[0] == 499


def test_optimizer_is_long_only_and_normalized(returns: dict) -> None:
    out = portfolio.optimize_portfolio(returns, objective="max_sharpe")
    assert out["success"]
    assert all(w >= -1e-6 for w in out["weights"].values())
    assert abs(sum(out["weights"].values()) - 1.0) < 1e-6


def test_optimizer_respects_max_weight(returns: dict) -> None:
    out = portfolio.optimize_portfolio(returns, objective="max_sharpe", max_weight=0.5)
    assert out["success"]
    assert max(out["weights"].values()) <= 0.5 + 1e-4


def test_min_variance_beats_equal_weight(returns: dict) -> None:
    opt = portfolio.optimize_portfolio(returns, objective="min_variance")
    assert opt["success"]
    equal = {t: 1 / 3 for t in ["AAA", "BBB", "CCC"]}
    eq_metrics = portfolio.risk_metrics(returns, equal)
    assert opt["annual_volatility"] <= eq_metrics["annual_volatility"] + 1e-4


def test_apply_wave_tilt_math() -> None:
    mu = pd.Series({"AAA": 0.10, "BBB": 0.08, "CCC": 0.12})
    tilted = portfolio.apply_wave_tilt(mu, {"AAA": "buildup", "BBB": "peak", "CCC": "neutral"})
    assert tilted["AAA"] == pytest.approx(0.10 * 1.20)
    assert tilted["BBB"] == pytest.approx(0.08 * 0.80)
    assert tilted["CCC"] == pytest.approx(0.12)
    # Original is not mutated.
    assert mu["AAA"] == pytest.approx(0.10)


def test_wave_tilt_propagates_through_optimizer(returns: dict) -> None:
    tickers = list(returns["mean"].index)
    views = {tickers[0]: "buildup", tickers[1]: "peak", tickers[2]: "digestion"}
    base = portfolio.optimize_portfolio(returns, objective="max_sharpe", max_weight=0.5)
    tilted = portfolio.optimize_portfolio(
        returns, objective="max_sharpe", max_weight=0.5, wave_views=views,
    )
    assert base["success"] and tilted["success"]
    assert tilted["applied_wave_views"] == views
    diffs = [abs(tilted["weights"][t] - base["weights"][t]) for t in tickers]
    assert max(diffs) > 1e-3


def test_risk_metrics_basic_shape(returns: dict) -> None:
    weights = {"AAA": 1 / 3, "BBB": 1 / 3, "CCC": 1 / 3}
    out = portfolio.risk_metrics(returns, weights)
    assert "sharpe_ratio" in out
    assert out["max_drawdown"] <= 0
    assert out["n_observations"] > 0
