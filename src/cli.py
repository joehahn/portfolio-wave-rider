"""Single CLI for every portfolio operation.

Nine subcommands. Each calls one function in ``src/portfolio.py`` and
prints the result as JSON to stdout. The /review-portfolio skill
invokes ``init-holdings`` (first-run branch only), ``wave-history``
(after each news pass), and ``analyze``; the cron jobs invoke
``snapshot``, ``news-feed``, ``recommend``, and ``dashboard``.
``backtest`` is a one-off spot-check tool, not part of any cron flow.
``seed-wave-history`` is a one-time backfill for chart 4 trajectories.

Usage:
    python -m src.cli init-holdings      --allocations '{"AAPL": 5000, ...}' --out holdings.csv
    python -m src.cli wave-history       [--news data/news_latest.json] [--force]
    python -m src.cli seed-wave-history  [--force]
    python -m src.cli news-feed          [--holdings holdings.csv] [--per-ticker-limit 5]
    python -m src.cli analyze            --tickers AAPL MSFT NVDA --period 3y --max-weight 0.25
    python -m src.cli snapshot           [--date YYYY-MM-DD] [--force]
    python -m src.cli recommend          [--max-weight 0.25] [--force]
    python -m src.cli backtest           [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--initial-usd 50000]
    python -m src.cli dashboard
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import portfolio


def _load_wave_views(arg: str) -> dict[str, str]:
    """Accept either a JSON literal or a path to a JSON file mapping ticker -> stage."""
    raw = json.loads(arg) if arg.startswith("{") else json.loads(Path(arg).read_text())
    return {str(k).upper(): str(v) for k, v in raw.items()}


def _load_allocations(arg: str) -> dict[str, float]:
    """Accept either a JSON literal or a path to a JSON file mapping ticker -> dollars."""
    raw = json.loads(arg) if arg.startswith("{") else json.loads(Path(arg).read_text())
    return {str(k).upper(): float(v) for k, v in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.cli", description="Portfolio CLI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-holdings",
                             help="convert a thesis-driven dollar allocation into shares; overwrite holdings.csv")
    p_init.add_argument("--allocations", required=True,
                        help="JSON literal or path mapping ticker -> dollars")
    p_init.add_argument("--out", default="holdings.csv")

    p_wh = sub.add_parser("wave-history",
                          help="append wave-stage classifications from news_latest.json to data/wave_history.csv")
    p_wh.add_argument("--news", default="data/news_latest.json",
                      help="path to news_latest.json (must contain top-level `date` and `wave_stages`)")
    p_wh.add_argument("--out", default="data/wave_history.csv")
    p_wh.add_argument("--force", action="store_true",
                      help="overwrite any existing rows for the news file's date")

    p_seed = sub.add_parser("seed-wave-history",
                            help="backfill wave_history.csv with 12 months of post-hoc monthly classifications (seeded=True)")
    p_seed.add_argument("--out", default="data/wave_history.csv")
    p_seed.add_argument("--force", action="store_true",
                        help="overwrite any existing rows for the seeded dates")

    p_nf = sub.add_parser("news-feed",
                          help="pull recent Yahoo Finance headlines per holdings ticker into data/news_feed.json")
    p_nf.add_argument("--holdings", default="holdings.csv")
    p_nf.add_argument("--out", default="data/news_feed.json")
    p_nf.add_argument("--per-ticker-limit", type=int, default=5,
                      help="max headlines per ticker (default 5; yfinance typically returns ~10)")
    p_nf.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")

    p_an = sub.add_parser("analyze", help="fetch + optimize + risk in one call")
    p_an.add_argument("--tickers", nargs="+", required=True)
    p_an.add_argument("--period", default="3y")
    p_an.add_argument("--objective", default="max_sharpe",
                      choices=["max_sharpe", "min_variance", "mean_variance"])
    p_an.add_argument("--max-weight", type=float, default=0.25)
    p_an.add_argument("--risk-free-rate", type=float, default=0.04)
    p_an.add_argument("--risk-aversion", type=float, default=1.0,
                      help="lambda in mean_variance objective (μᵀw - λ·wᵀΣw); "
                           "small λ favors return, large λ favors variance reduction")
    p_an.add_argument("--wave-views", default=None,
                      help="JSON literal or path mapping ticker -> wave stage "
                           "(buildup|surge|peak|digestion|neutral). Tilts expected returns.")

    p_snap = sub.add_parser("snapshot", help="append today's $ values to data/snapshots.csv")
    p_snap.add_argument("--holdings", default="holdings.csv")
    p_snap.add_argument("--out", default="data/snapshots.csv")
    p_snap.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")
    p_snap.add_argument("--force", action="store_true",
                        help="overwrite an existing row for this date")

    p_rec = sub.add_parser("recommend", help="optimize and append weights to data/recommendations.csv")
    p_rec.add_argument("--holdings", default="holdings.csv")
    p_rec.add_argument("--out", default="data/recommendations.csv")
    p_rec.add_argument("--period", default="3y")
    p_rec.add_argument("--max-weight", type=float, default=0.25)
    p_rec.add_argument("--risk-free-rate", type=float, default=0.04)
    p_rec.add_argument("--objective", default="max_sharpe",
                       choices=["max_sharpe", "min_variance"])
    p_rec.add_argument("--date", default=None)
    p_rec.add_argument("--force", action="store_true")

    p_bt = sub.add_parser("backtest",
                           help="walk-forward weekly-rebalance backtest of the cron 'recommend' path; outputs to data/backtest/")
    p_bt.add_argument("--holdings", default="holdings.csv",
                      help="watchlist source; only the ticker column is used")
    p_bt.add_argument("--start-date", default=None,
                      help="YYYY-MM-DD; defaults to 12 months before --end-date")
    p_bt.add_argument("--end-date", default=None,
                      help="YYYY-MM-DD; defaults to yesterday")
    p_bt.add_argument("--initial-usd", type=float, default=50000.0,
                      help="starting portfolio value in dollars")
    p_bt.add_argument("--out-dir", default="data/backtest/")
    p_bt.add_argument("--lookback-years", type=int, default=3,
                      help="optimizer lookback window in years; default 3 matches the live system")
    p_bt.add_argument("--max-weight", type=float, default=0.25)
    p_bt.add_argument("--objective", default="max_sharpe",
                      choices=["max_sharpe", "min_variance"])
    p_bt.add_argument("--risk-free-rate", type=float, default=0.04)
    p_bt.add_argument("--benchmarks", nargs="*", default=["SPY"],
                      help="benchmark tickers compared against the backtest's realized return "
                           "(default: SPY). Pass an empty list to skip the benchmark section.")

    p_dash = sub.add_parser("dashboard", help="generate data/dashboard.html from snapshots + recommendations + news + wave history")
    p_dash.add_argument("--snapshots", default="data/snapshots.csv")
    p_dash.add_argument("--recommendations", default="data/recommendations.csv")
    p_dash.add_argument("--news", default="data/news_latest.json")
    p_dash.add_argument("--news-feed", default="data/news_feed.json")
    p_dash.add_argument("--wave-history", default="data/wave_history.csv")
    p_dash.add_argument("--benchmarks", nargs="*", default=["SPY"],
                        help="benchmark tickers to overlay on the portfolio-value chart "
                             "(default: SPY). Pass an empty list to suppress overlays.")
    p_dash.add_argument("--out", default="data/dashboard.html")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "init-holdings":
            allocations = _load_allocations(args.allocations)
            prices_df = portfolio.fetch_prices(list(allocations.keys()), period="7d")
            last_prices = {t: float(prices_df[t].iloc[-1]) for t in prices_df.columns}
            result = portfolio.initialize_holdings(allocations, last_prices, holdings_path=args.out)
        elif args.cmd == "wave-history":
            news_path = Path(args.news)
            if not news_path.exists():
                raise FileNotFoundError(f"news file not found: {news_path}")
            news = json.loads(news_path.read_text())
            wave_stages = news.get("wave_stages") or {}
            news_date = news.get("date") or ""
            if not news_date:
                raise ValueError(f"{news_path} has no top-level `date` field")
            result = portfolio.append_wave_history(
                wave_stages, date=news_date, out_path=args.out, force=args.force,
            )
        elif args.cmd == "seed-wave-history":
            result = portfolio.seed_wave_history(out_path=args.out, force=args.force)
        elif args.cmd == "news-feed":
            result = portfolio.fetch_news_feed(
                holdings_path=args.holdings, out_path=args.out,
                per_ticker_limit=args.per_ticker_limit, date=args.date,
            )
        elif args.cmd == "backtest":
            result = portfolio.backtest(
                holdings_path=args.holdings,
                start_date=args.start_date, end_date=args.end_date,
                initial_usd=args.initial_usd, out_dir=args.out_dir,
                lookback_years=args.lookback_years,
                max_weight=args.max_weight, objective=args.objective,
                risk_free_rate=args.risk_free_rate,
                benchmarks=args.benchmarks,
            )
        elif args.cmd == "analyze":
            result = portfolio.analyze(
                args.tickers, period=args.period, objective=args.objective,
                max_weight=args.max_weight, risk_free_rate=args.risk_free_rate,
                risk_aversion=args.risk_aversion,
                wave_views=_load_wave_views(args.wave_views) if args.wave_views else None,
            )
        elif args.cmd == "snapshot":
            result = portfolio.snapshot_holdings(
                holdings_path=args.holdings, out_path=args.out,
                date=args.date, force=args.force,
            )
        elif args.cmd == "recommend":
            result = portfolio.recommend_portfolio(
                holdings_path=args.holdings, out_path=args.out,
                period=args.period, max_weight=args.max_weight,
                risk_free_rate=args.risk_free_rate, objective=args.objective,
                date=args.date, force=args.force,
            )
        else:  # dashboard
            result = portfolio.build_dashboard(
                snapshots_path=args.snapshots,
                recommendations_path=args.recommendations,
                out_path=args.out,
                news_path=args.news,
                news_feed_path=args.news_feed,
                wave_history_path=args.wave_history,
                benchmarks=args.benchmarks,
            )
    except Exception as e:  # noqa: BLE001 — surface any failure as a JSON error line
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1

    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
