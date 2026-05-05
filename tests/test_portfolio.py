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


def test_append_wave_history_writes_one_row_per_wave(tmp_path) -> None:
    out = tmp_path / "wave_history.csv"
    wave_stages = {
        "AI": {"stage": "surge", "rationale": "Real revenue compounding.",
               "evidence_tickers": ["NVDA", "MSFT", "GOOGL"]},
        "rockets_spacecraft": {"stage": "buildup", "rationale": "RKLB pre-Neutron.",
                                "evidence_tickers": ["RKLB"]},
        "general_markets": {"stage": "neutral", "rationale": "Macro instruments.",
                            "evidence_tickers": ["AGG", "BIL", "IAU", "IBIT"]},
    }
    result = portfolio.append_wave_history(wave_stages, date="2026-05-04",
                                            out_path=str(out))
    assert result["n_rows_appended"] == 3
    df = pd.read_csv(out)
    assert list(df.columns) == ["date", "wave", "stage", "evidence_tickers", "rationale", "seeded"]
    ai_row = df[df["wave"] == "AI"].iloc[0]
    assert ai_row["stage"] == "surge"
    assert ai_row["evidence_tickers"] == "NVDA;MSFT;GOOGL"


def test_append_wave_history_idempotent_on_date(tmp_path) -> None:
    out = tmp_path / "wave_history.csv"
    wave_stages = {"AI": {"stage": "surge", "rationale": "Same.", "evidence_tickers": ["NVDA"]}}
    portfolio.append_wave_history(wave_stages, date="2026-05-04", out_path=str(out))
    second = portfolio.append_wave_history(wave_stages, date="2026-05-04", out_path=str(out))
    assert second.get("skipped") is True
    # Force overwrite produces same row count, not duplicated.
    portfolio.append_wave_history(wave_stages, date="2026-05-04",
                                   out_path=str(out), force=True)
    df = pd.read_csv(out)
    assert len(df) == 1


def test_append_wave_history_appends_across_dates(tmp_path) -> None:
    out = tmp_path / "wave_history.csv"
    portfolio.append_wave_history(
        {"AI": {"stage": "surge", "rationale": "...", "evidence_tickers": ["NVDA"]}},
        date="2026-04-15", out_path=str(out),
    )
    portfolio.append_wave_history(
        {"AI": {"stage": "peak", "rationale": "...", "evidence_tickers": ["NVDA"]}},
        date="2026-05-15", out_path=str(out),
    )
    df = pd.read_csv(out).sort_values("date").reset_index(drop=True)
    assert len(df) == 2
    assert df.loc[0, "stage"] == "surge"
    assert df.loc[1, "stage"] == "peak"


def test_backtest_runs_against_seeded_prices(tmp_path, monkeypatch) -> None:
    """End-to-end backtest test against synthetic prices via a yf.download monkeypatch."""
    import json
    rng = np.random.default_rng(0)
    # Synthetic 4-year daily price series for 3 tickers (long enough for a 3y lookback).
    dates = pd.date_range("2022-05-01", periods=1040, freq="B")
    daily = rng.normal(loc=[0.0004, 0.0002, 0.0006], scale=[0.011, 0.008, 0.018], size=(1040, 3))
    prices = pd.DataFrame(
        100 * np.exp(daily.cumsum(axis=0)),
        index=dates,
        columns=["AAA", "BBB", "CCC"],
    )

    # Mock yf.download to return our synthetic prices in the shape the backtest expects.
    def fake_download(tickers, start, end, **kwargs):
        cols = pd.MultiIndex.from_product([["Close"], tickers])
        df = pd.DataFrame(prices.values, index=prices.index,
                          columns=pd.MultiIndex.from_product([["Close"], list(prices.columns)]))
        return df.loc[start:end]
    monkeypatch.setattr(portfolio.yf, "download", fake_download)

    holdings_path = tmp_path / "holdings.csv"
    holdings_path.write_text("ticker,shares\nAAA,0\nBBB,0\nCCC,0\n")

    out_dir = tmp_path / "backtest"
    result = portfolio.backtest(
        holdings_path=str(holdings_path),
        start_date="2025-11-03", end_date="2026-04-20",
        initial_usd=10000.0, out_dir=str(out_dir),
        # max_weight >= 1/n_tickers required for feasibility; with 3 tickers, 0.25 would be infeasible.
        max_weight=0.5,
    )

    # Output files exist.
    assert (out_dir / "snapshots.csv").exists()
    assert (out_dir / "recommendations.csv").exists()
    assert (out_dir / "report.md").exists()

    # Schemas match the live snapshots/recommendations files so the dashboard CLI
    # can render them with --snapshots / --recommendations overrides.
    snaps = pd.read_csv(out_dir / "snapshots.csv")
    assert list(snaps.columns) == ["date", "ticker", "shares", "price", "value", "total_value"]
    recs = pd.read_csv(out_dir / "recommendations.csv")
    assert list(recs.columns) == [
        "date", "ticker", "weight", "expected_return", "annual_volatility",
        "sharpe_ratio", "objective",
    ]

    # Sanity: starting value matches initial_usd; weights sum to ~1.0 each rebalance.
    assert result["initial_value"] == pytest.approx(10000.0, abs=0.01)
    for d, sub in recs.groupby("date"):
        assert abs(sub["weight"].sum() - 1.0) < 1e-4

    # Weight stability is finite and within [0, 2] (the L1 distance bounds).
    assert 0 <= result["weight_stability_l1"] <= 2


def test_fetch_benchmark_curves_normalizes_to_starting_value(monkeypatch) -> None:
    """The benchmark curve should start at exactly `starting_value`."""
    dates = pd.date_range("2025-11-04", periods=10, freq="B")
    spy_close = pd.Series([400.0, 402.0, 404.0, 406.0, 408.0,
                           410.0, 412.0, 414.0, 416.0, 418.0], index=dates)

    def fake_download(tickers, start, end, **kwargs):
        cols = pd.MultiIndex.from_product([["Close"], tickers])
        df = pd.DataFrame({("Close", "SPY"): spy_close})
        return df.loc[start:end]

    monkeypatch.setattr(portfolio.yf, "download", fake_download)
    curves = portfolio._fetch_benchmark_curves(
        ["SPY"], dates[0], dates[-1], starting_value=50000.0,
    )
    assert "SPY" in curves
    # First value rebased to starting_value; final value scales proportionally.
    assert curves["SPY"].iloc[0] == pytest.approx(50000.0)
    # SPY went 400 -> 418 (+4.5%); curve should reflect the same percentage.
    assert curves["SPY"].iloc[-1] == pytest.approx(50000.0 * (418.0 / 400.0))


def test_fetch_benchmark_curves_returns_empty_on_yfinance_failure(monkeypatch) -> None:
    """A yfinance error must not break the dashboard - skip the benchmark instead."""
    def boom(*args, **kwargs):
        raise RuntimeError("yfinance unavailable")
    monkeypatch.setattr(portfolio.yf, "download", boom)
    curves = portfolio._fetch_benchmark_curves(
        ["SPY"], pd.Timestamp("2025-11-04"), pd.Timestamp("2025-11-15"), 50000.0,
    )
    assert curves == {}


def test_render_news_html_renders_both_sections_when_both_paths_provided(tmp_path) -> None:
    """The two news files render as two sections in the same HTML block."""
    import json
    feed = {
        "date": "2026-05-04",
        "per_ticker": {
            "NVDA": {"bullets": [
                {"headline": "Yahoo headline for NVDA", "summary": "Yahoo lead.",
                 "source": "Yahoo Finance", "url": "https://example.com/yf-nvda",
                 "date": "2026-05-04"},
            ]},
        },
    }
    latest = {
        "date": "2026-05-02",
        "per_ticker": {
            "NVDA": {"wave_bucket": "AI", "bullets": [
                {"headline": "LLM headline for NVDA", "summary": "Portfolio-relevance summary.",
                 "source": "SemiAnalysis", "url": "https://example.com/llm-nvda",
                 "date": "2026-04-23"},
            ]},
        },
    }
    feed_path = tmp_path / "news_feed.json"
    latest_path = tmp_path / "news_latest.json"
    feed_path.write_text(json.dumps(feed))
    latest_path.write_text(json.dumps(latest))

    out = portfolio._render_news_html(latest_path, news_feed_path=feed_path)

    # Both section titles appear, daily-feed first.
    assert "Today's headlines" in out
    assert "In-depth news from last /review-portfolio" in out
    assert out.index("Today's headlines") < out.index("In-depth news from last /review-portfolio")

    # Both source URLs render.
    assert 'href="https://example.com/yf-nvda"' in out
    assert 'href="https://example.com/llm-nvda"' in out

    # Daily section omits wave_bucket label since yfinance doesn't classify;
    # in-depth section keeps the (AI) label from wave_bucket.
    feed_section_end = out.index("In-depth news from last /review-portfolio")
    feed_section = out[:feed_section_end]
    indepth_section = out[feed_section_end:]
    assert "(AI)" not in feed_section
    assert "(AI)" in indepth_section


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
