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


def test_initialize_holdings(tmp_path) -> None:
    allocations = {"AAA": 5000.0, "BBB": 3000.0, "CCC": 2000.0}
    prices = {"AAA": 100.0, "BBB": 50.0, "CCC": 20.0}
    out_path = tmp_path / "holdings.csv"
    result = portfolio.initialize_holdings(allocations, prices, str(out_path))

    # Resulting CSV has the expected schema and shares.
    df = pd.read_csv(out_path)
    assert list(df.columns) == ["ticker", "shares"]
    assert df.set_index("ticker")["shares"].to_dict() == {
        "AAA": 50.0, "BBB": 60.0, "CCC": 100.0,
    }
    # Total invested matches the sum of allocations within rounding.
    assert result["total_invested"] == pytest.approx(10000.0, abs=0.01)
    assert result["total_requested"] == pytest.approx(10000.0, abs=0.01)


def test_initialize_holdings_zero_allocation_keeps_zero_shares(tmp_path) -> None:
    allocations = {"AAA": 1000.0, "BBB": 0.0}
    prices = {"AAA": 100.0, "BBB": 50.0}
    out_path = tmp_path / "holdings.csv"
    portfolio.initialize_holdings(allocations, prices, str(out_path))
    df = pd.read_csv(out_path).set_index("ticker")
    assert df.loc["AAA", "shares"] == 10.0
    assert df.loc["BBB", "shares"] == 0.0


def test_initialize_holdings_rejects_missing_prices() -> None:
    with pytest.raises(ValueError, match="prices missing"):
        portfolio.initialize_holdings({"AAA": 100.0}, {"BBB": 50.0})


def test_initialize_holdings_rejects_negative_allocation() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        portfolio.initialize_holdings({"AAA": -100.0}, {"AAA": 50.0})


def test_render_news_html_returns_empty_when_file_missing(tmp_path) -> None:
    assert portfolio._render_news_html(tmp_path / "missing.json") == ""


def test_render_news_html_emits_expandable_headlines(tmp_path) -> None:
    import json
    news = {
        "date": "2026-05-02",
        "per_ticker": {
            "NVDA": {
                "wave_bucket": "AI",
                "bullets": [
                    {"headline": "NVDA Q1 revenue +69% YoY",
                     "summary": "Revenue $44.1B, Data Center $39.1B...",
                     "source": "NVIDIA",
                     "url": "https://example.com/nvda", "date": "2025-05-28"},
                ],
            },
            "AGG": {
                "wave_bucket": "general_markets",
                "bullets": [
                    {"headline": "Fed holds rates at 3.50-3.75%",
                     "summary": "Powell's final FOMC; sticky inflation...",
                     "source": "CNBC",
                     "url": "https://example.com/agg", "date": "2026-04-29"},
                ],
            },
        },
    }
    p = tmp_path / "news.json"
    p.write_text(json.dumps(news))
    out = portfolio._render_news_html(p)
    # Date appears in header.
    assert "2026-05-02" in out
    # Both tickers and their wave buckets appear as headers.
    assert "NVDA" in out and "AI" in out
    assert "AGG" in out and "general_markets" in out
    # Each bullet is wrapped in <details> so headlines are click-to-expand.
    assert out.count("<details") == 2
    assert out.count("<summary") == 2
    # Headlines are the click target (visible in the summary tag).
    assert "NVDA Q1 revenue +69% YoY" in out
    assert "Fed holds rates at 3.50-3.75%" in out
    # The expanded body contains the longer summary text and a "Read full article" link.
    assert "Revenue $44.1B, Data Center $39.1B" in out
    assert "Read full article" in out
    assert 'href="https://example.com/nvda"' in out
    # AI bucket is rendered before general_markets per the wave display order.
    assert out.index("NVDA") < out.index("AGG")


def test_render_news_html_falls_back_when_headline_missing(tmp_path) -> None:
    """Older news_latest.json without a headline field still renders cleanly."""
    import json
    news = {
        "date": "2026-05-02",
        "per_ticker": {
            "NVDA": {
                "wave_bucket": "AI",
                "bullets": [
                    {"summary": "Revenue jumped 69 percent year over year. Other context follows.",
                     "source": "NVIDIA",
                     "url": "https://example.com/nvda", "date": "2025-05-28"},
                ],
            },
        },
    }
    p = tmp_path / "news.json"
    p.write_text(json.dumps(news))
    out = portfolio._render_news_html(p)
    # Click target (the <summary>) falls back to the first sentence of the body.
    assert "Revenue jumped 69 percent year over year" in out
    # Full body still appears in the expanded section.
    assert "Other context follows" in out
    # Click-to-expand wrapper is still present.
    assert "<details" in out
