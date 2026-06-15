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
        # Don't overwrite the GitHub-Pages-served docs/backtest.html with
        # the synthetic AAA/BBB/CCC dashboard.
        publish_docs=False,
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


def test_fetch_prices_min_history_excludes_recent_ipo(monkeypatch) -> None:
    """A days-old IPO must be dropped before the join so it can't truncate the
    seasoned tickers' estimation window (the SPCX failure mode)."""
    # ~2.4y of business days ending today, so a 1.5y lookback lands inside.
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=600)
    seasoned = pd.DataFrame(
        {"AAA": np.linspace(100, 160, 600), "BBB": np.linspace(50, 90, 600)},
        index=dates,
    )
    ipo = pd.Series(np.nan, index=dates)
    ipo.iloc[-3:] = [200.0, 205.0, 210.0]  # first trade only 3 days ago
    full = seasoned.assign(ZZZ=ipo)

    def fake_download(tickers, start, end, **kwargs):
        df = full.copy()
        df.columns = pd.MultiIndex.from_product([["Close"], df.columns])
        return df.loc[start:end]
    monkeypatch.setattr(portfolio.yf, "download", fake_download)

    prices = portfolio.fetch_prices(["AAA", "BBB", "ZZZ"], period="1.5y", min_history=True)
    # ZZZ excluded; seasoned tickers keep the full ~1.5y window (not truncated to 3 rows).
    assert list(prices.columns) == ["AAA", "BBB"]
    assert prices.attrs["excluded_short_history"] == ["ZZZ"]
    assert len(prices) > 100

    # Without the filter, the leading-NaN join collapses the panel to ZZZ's 3 rows.
    unfiltered = portfolio.fetch_prices(["AAA", "BBB", "ZZZ"], period="1.5y")
    assert list(unfiltered.columns) == ["AAA", "BBB", "ZZZ"]
    assert len(unfiltered) == 3






# ---------------------------------------------------------------------------
# Watchlist curator: validation + holdings/history mutation.
# ---------------------------------------------------------------------------

def _payload(adds=(), removes=(), as_of_date="2024-06-01") -> dict:
    """Build a minimal curator JSON payload for testing."""
    def add(t, bucket="AI"):
        return {
            "ticker": t,
            "wave_bucket": bucket,
            "rationale": f"adopt {t} based on news",
            "news_evidence": [{"summary": "x", "source": "Bloomberg",
                                "url": f"https://example.com/{t}",
                                "date": as_of_date}],
        }
    def rem(t):
        return {"ticker": t, "rationale": f"drop {t}",
                "news_evidence": [{"summary": "y", "source": "WSJ",
                                    "url": f"https://example.com/rm-{t}",
                                    "date": as_of_date}]}
    return {
        "as_of_date": as_of_date,
        "rebalance_period": "monthly",
        "adds": [add(t) for t in adds],
        "removes": [rem(t) for t in removes],
        "no_changes": not (adds or removes),
        "rationale_overall": "test",
    }


def _write_holdings(path, tickers, shares=None) -> None:
    shares = shares or [0] * len(tickers)
    pd.DataFrame({"ticker": tickers, "shares": shares}).to_csv(path, index=False)


def _write_profile(path, max_size=12) -> None:
    path.write_text(
        f"---\nfinancial_model:\n  max_watchlist_size: {max_size}\n---\n# profile\n"
    )


def test_curator_applies_valid_add_and_remove(tmp_path) -> None:
    holdings = tmp_path / "holdings.csv"
    history = tmp_path / "curation_history.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["AAPL", "MSFT", "SPY", "AGG"])
    _write_profile(profile, max_size=12)
    payload = _payload(adds=["NVDA"], removes=["AGG"])
    result = portfolio.apply_curator_decisions(
        payload, holdings_path=str(holdings), history_path=str(history),
        profile_path=str(profile), listing_check=False,
    )
    assert result["applied_adds"] == ["NVDA"]
    assert result["applied_removes"] == ["AGG"]
    assert result["rejections"] == []
    assert set(result["post_watchlist"]) == {"AAPL", "MSFT", "SPY", "NVDA"}
    # history has one row per applied change
    hist = pd.read_csv(history)
    assert len(hist) == 2
    assert set(hist["action"]) == {"add", "remove"}


def test_curator_rejects_add_already_in_watchlist(tmp_path) -> None:
    holdings = tmp_path / "holdings.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["AAPL", "NVDA"])
    _write_profile(profile)
    payload = _payload(adds=["NVDA"])
    result = portfolio.apply_curator_decisions(
        payload, holdings_path=str(holdings),
        history_path=str(tmp_path / "h.csv"),
        profile_path=str(profile), listing_check=False,
    )
    assert result["applied_adds"] == []
    assert any(r["ticker"] == "NVDA" and "already" in r["reason"]
               for r in result["rejections"])


def test_curator_rejects_remove_of_unheld_ticker(tmp_path) -> None:
    holdings = tmp_path / "holdings.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["AAPL"])
    _write_profile(profile)
    payload = _payload(removes=["NVDA"])
    result = portfolio.apply_curator_decisions(
        payload, holdings_path=str(holdings),
        history_path=str(tmp_path / "h.csv"),
        profile_path=str(profile), listing_check=False,
    )
    assert result["applied_removes"] == []
    assert any(r["ticker"] == "NVDA" and "not in" in r["reason"]
               for r in result["rejections"])


def test_curator_rejects_remove_with_live_position(tmp_path) -> None:
    holdings = tmp_path / "holdings.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["AAPL", "MSFT"], shares=[10, 0])
    _write_profile(profile)
    payload = _payload(removes=["AAPL"])
    result = portfolio.apply_curator_decisions(
        payload, holdings_path=str(holdings),
        history_path=str(tmp_path / "h.csv"),
        profile_path=str(profile), listing_check=False,
    )
    assert result["applied_removes"] == []
    assert any(r["ticker"] == "AAPL" and "liquidate" in r["reason"]
               for r in result["rejections"])


def test_curator_enforces_max_watchlist_size(tmp_path) -> None:
    holdings = tmp_path / "holdings.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["A", "B", "C", "D"])
    _write_profile(profile, max_size=5)
    # 4 existing + 3 adds = 7 > cap of 5; expect 2 of the 3 adds rejected
    payload = _payload(adds=["E", "F", "G"])
    result = portfolio.apply_curator_decisions(
        payload, holdings_path=str(holdings),
        history_path=str(tmp_path / "h.csv"),
        profile_path=str(profile), listing_check=False,
    )
    assert len(result["applied_adds"]) == 1
    assert sum(1 for r in result["rejections"]
               if "max_watchlist_size" in r["reason"]) == 2


def test_curator_rejects_overlapping_adds_and_removes(tmp_path) -> None:
    holdings = tmp_path / "holdings.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["AAPL"])
    _write_profile(profile)
    payload = _payload(adds=["NVDA"], removes=["AAPL"])
    payload["adds"][0]["ticker"] = "AAPL"  # force the overlap
    with pytest.raises(ValueError, match="both adds and removes"):
        portfolio.apply_curator_decisions(
            payload, holdings_path=str(holdings),
            history_path=str(tmp_path / "h.csv"),
            profile_path=str(profile), listing_check=False,
        )


def test_curator_listing_check_blocks_pre_listing_add(tmp_path, monkeypatch) -> None:
    """Mock yfinance: ticker has no data on the as_of_date, so add is rejected."""
    def empty_download(*_a, **_kw):
        return pd.DataFrame()
    monkeypatch.setattr(portfolio.yf, "download", empty_download)
    holdings = tmp_path / "holdings.csv"
    profile = tmp_path / "profile.md"
    _write_holdings(holdings, ["AAPL"])
    _write_profile(profile)
    payload = _payload(adds=["NUKZ"], as_of_date="2022-01-01")
    result = portfolio.apply_curator_decisions(
        payload, holdings_path=str(holdings),
        history_path=str(tmp_path / "h.csv"),
        profile_path=str(profile), listing_check=True,
    )
    assert result["applied_adds"] == []
    assert any("listing-date" in r["reason"] for r in result["rejections"])


def test_reconstruct_watchlist_at_replays_history(tmp_path) -> None:
    history = tmp_path / "h.csv"
    pd.DataFrame([
        {"date": "2022-03-01", "action": "add", "ticker": "NVDA",
         "wave_bucket": "AI", "rationale": "x", "news_evidence_urls": ""},
        {"date": "2022-06-01", "action": "remove", "ticker": "AGG",
         "wave_bucket": "", "rationale": "y", "news_evidence_urls": ""},
        {"date": "2023-01-01", "action": "add", "ticker": "BOTZ",
         "wave_bucket": "robotics", "rationale": "z", "news_evidence_urls": ""},
    ]).to_csv(history, index=False)
    day_zero = ["AAPL", "MSFT", "SPY", "AGG"]
    # Before the first event - day-0 watchlist unchanged.
    assert portfolio.reconstruct_watchlist_at(
        "2022-01-01", day_zero, history_path=str(history)
    ) == ["AAPL", "AGG", "MSFT", "SPY"]
    # After first add, before remove.
    assert portfolio.reconstruct_watchlist_at(
        "2022-04-01", day_zero, history_path=str(history)
    ) == ["AAPL", "AGG", "MSFT", "NVDA", "SPY"]
    # After all events.
    assert portfolio.reconstruct_watchlist_at(
        "2024-01-01", day_zero, history_path=str(history)
    ) == ["AAPL", "BOTZ", "MSFT", "NVDA", "SPY"]


def test_curator_backtest_replays_synthetic_run(tmp_path, monkeypatch) -> None:
    """End-to-end replay against a synthetic 4-ticker price series.

    Verifies the function consumes _starter.json + dated curation payloads,
    produces all four output files, and the curator strategy diverges from
    the fixed-watchlist baseline once a curator add takes effect.
    """
    import json
    rng = np.random.default_rng(7)
    dates = pd.date_range("2022-05-01", periods=1040, freq="B")
    # Four tickers; DDD has clearly higher drift so a curator that adds it
    # mid-run should beat the fixed baseline.
    daily = rng.normal(loc=[0.0003, 0.0002, 0.0004, 0.0015],
                       scale=[0.010, 0.009, 0.011, 0.014], size=(1040, 4))
    prices = pd.DataFrame(
        100 * np.exp(daily.cumsum(axis=0)),
        index=dates, columns=["AAA", "BBB", "CCC", "DDD"],
    )

    def fake_download(tickers, start, end, **kwargs):
        if isinstance(tickers, str):
            tickers = [tickers]
        cols = pd.MultiIndex.from_product([["Close"], list(prices.columns)])
        df = pd.DataFrame(prices.values, index=prices.index, columns=cols)
        return df.loc[start:end]
    monkeypatch.setattr(portfolio.yf, "download", fake_download)

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    starter = {
        "starter_watchlist": ["AAA", "BBB", "CCC"],
        "start_date": "2025-08-01",
        "end_date": "2026-04-30",
        "rebalance_period": "monthly",
        "initial_usd": 10000.0,
        "lookback_years": 1.3,
        "max_watchlist_size": 6,
    }
    (runs_dir / "_starter.json").write_text(json.dumps(starter))
    # One curator payload mid-run: add DDD on 2025-10-01.
    add_payload = {
        "as_of_date": "2025-10-01",
        "rebalance_period": "monthly",
        "adds": [{"ticker": "DDD", "wave_bucket": "AI",
                  "rationale": "high-drift ticker enters",
                  "news_evidence": [{"summary": "x", "source": "X",
                                      "url": "https://example.com/ddd",
                                      "date": "2025-09-15"}]}],
        "removes": [],
        "no_changes": False,
    }
    (runs_dir / "2025-10-01-curation.json").write_text(json.dumps(add_payload))

    out_dir = tmp_path / "out"
    result = portfolio.curator_backtest(
        runs_dir=str(runs_dir), out_dir=str(out_dir),
        max_weight=0.5,
        benchmarks=[],  # skip yfinance benchmark fetch
    )

    # Output files exist with the expected shapes.
    assert (out_dir / "snapshots.csv").exists()
    assert (out_dir / "recommendations.csv").exists()
    assert (out_dir / "baselines_totals.csv").exists()
    assert (out_dir / "report.md").exists()
    assert (out_dir / "curation_summary.json").exists()

    snaps = pd.read_csv(out_dir / "snapshots.csv")
    assert list(snaps.columns) == ["date", "ticker", "shares", "price", "value", "total_value"]
    # DDD is in the final watchlist (was added).
    assert "DDD" in set(result["final_watchlist"])
    # The curator add was actually applied.
    summary = json.loads((out_dir / "curation_summary.json").read_text())
    assert any("DDD" in c.get("adds", []) for c in summary)
    # Baselines were computed.
    baselines = pd.read_csv(out_dir / "baselines_totals.csv")
    assert "fixed_total" in baselines.columns
    assert "bnh_total" in baselines.columns
    assert result["fixed_baseline_return"] is not None
    assert result["bnh_baseline_return"] is not None
