"""Tests for the portfolio math functions and the CLI.

Runs offline — tests that need a prices series seed the disk-backed
state directly instead of calling yfinance. Each test gets its own
state directory via the PORTFOLIO_STATE_DIR env var.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import portfolio


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a fresh state dir so handles don't leak across tests."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PORTFOLIO_STATE_DIR", str(d))
    return d


@pytest.fixture
def returns_handle() -> str:
    """Seed a fake prices series and return a usable returns_handle."""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2023-01-03", periods=500, freq="B")
    daily = rng.normal(loc=[0.0005, 0.0003, 0.0007], scale=[0.012, 0.009, 0.018], size=(500, 3))
    prices = pd.DataFrame(
        100 * np.exp(daily.cumsum(axis=0)),
        index=dates,
        columns=["AAA", "BBB", "CCC"],
    )
    prices_handle = portfolio.put("prices", prices)
    return portfolio.compute_returns(prices_handle)["returns_handle"]


# ---- pure-Python function tests -------------------------------------------

def test_compute_returns_shapes(returns_handle: str) -> None:
    bundle = portfolio.get(returns_handle)
    assert set(bundle["mean"].index) == {"AAA", "BBB", "CCC"}
    assert bundle["log_returns"].shape[0] == 499


def test_optimizer_is_long_only_and_normalized(returns_handle: str) -> None:
    out = portfolio.optimize_portfolio(returns_handle, objective="max_sharpe")
    assert out["success"]
    assert all(w >= -1e-6 for w in out["weights"].values())
    assert abs(sum(out["weights"].values()) - 1.0) < 1e-6


def test_optimizer_respects_max_weight(returns_handle: str) -> None:
    out = portfolio.optimize_portfolio(returns_handle, objective="max_sharpe", max_weight=0.5)
    assert out["success"]
    assert max(out["weights"].values()) <= 0.5 + 1e-4


def test_min_variance_beats_equal_weight(returns_handle: str) -> None:
    opt = portfolio.optimize_portfolio(returns_handle, objective="min_variance")
    assert opt["success"]
    equal = {t: 1 / 3 for t in ["AAA", "BBB", "CCC"]}
    eq_metrics = portfolio.risk_metrics(returns_handle, equal)
    assert opt["annual_volatility"] <= eq_metrics["annual_volatility"] + 1e-4


def test_backtest_reports_both_windows(returns_handle: str) -> None:
    weights = {"AAA": 1 / 3, "BBB": 1 / 3, "CCC": 1 / 3}
    bt = portfolio.backtest(returns_handle, weights, train_fraction=0.7)
    assert bt["in_sample"]["n_observations"] > 0
    assert bt["out_of_sample"]["n_observations"] > 0


def test_apply_wave_tilt_math() -> None:
    mu = pd.Series({"AAA": 0.10, "BBB": 0.08, "CCC": 0.12})
    tilted = portfolio.apply_wave_tilt(mu, {"AAA": "buildup", "BBB": "peak", "CCC": "neutral"})
    assert tilted["AAA"] == pytest.approx(0.10 * 1.20)
    assert tilted["BBB"] == pytest.approx(0.08 * 0.80)
    assert tilted["CCC"] == pytest.approx(0.12)
    # Original is not mutated.
    assert mu["AAA"] == pytest.approx(0.10)


def test_wave_tilt_propagates_through_optimizer(returns_handle: str) -> None:
    # Tag every ticker with a different stage — weights and expected return
    # must change vs the untilted base run, and the views must be echoed.
    bundle = portfolio.get(returns_handle)
    tickers = list(bundle["mean"].index)
    views = {tickers[0]: "buildup", tickers[1]: "peak", tickers[2]: "digestion"}
    base = portfolio.optimize_portfolio(returns_handle, objective="max_sharpe", max_weight=0.5)
    tilted = portfolio.optimize_portfolio(
        returns_handle, objective="max_sharpe", max_weight=0.5, wave_views=views,
    )
    assert base["success"] and tilted["success"]
    assert tilted["applied_wave_views"] == views
    # At least one weight must differ materially from the base solution.
    diffs = [abs(tilted["weights"][t] - base["weights"][t]) for t in tickers]
    assert max(diffs) > 1e-3


def test_unknown_handle_raises() -> None:
    with pytest.raises(KeyError):
        portfolio.get("prices_does_not_exist")


# ---- CLI subcommand tests -------------------------------------------------

def _run_cli(*args: str, state_dir: Path) -> dict:
    """Invoke `python -m src.cli <args>` and return the parsed JSON."""
    env = {**os.environ, "PORTFOLIO_STATE_DIR": str(state_dir)}
    proc = subprocess.run(
        [sys.executable, "-m", "src.cli", *args],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


def test_cli_optimize(returns_handle: str, isolated_state: Path) -> None:
    out = _run_cli(
        "optimize", "--returns-handle", returns_handle,
        "--objective", "max_sharpe", "--max-weight", "0.5",
        state_dir=isolated_state,
    )
    assert out["success"]
    assert max(out["weights"].values()) <= 0.5 + 1e-4


def test_cli_risk(returns_handle: str, isolated_state: Path) -> None:
    weights = json.dumps({"AAA": 1 / 3, "BBB": 1 / 3, "CCC": 1 / 3})
    out = _run_cli(
        "risk", "--returns-handle", returns_handle, "--weights", weights,
        state_dir=isolated_state,
    )
    assert "sharpe_ratio" in out
    assert out["max_drawdown"] <= 0


def test_cli_backtest(returns_handle: str, isolated_state: Path) -> None:
    weights = json.dumps({"AAA": 1 / 3, "BBB": 1 / 3, "CCC": 1 / 3})
    out = _run_cli(
        "backtest", "--returns-handle", returns_handle, "--weights", weights,
        "--train-fraction", "0.7",
        state_dir=isolated_state,
    )
    assert out["in_sample"]["n_observations"] > 0
    assert out["out_of_sample"]["n_observations"] > 0
