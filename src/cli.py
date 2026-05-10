"""Single CLI for every portfolio operation.

Eight subcommands. Each calls one function in ``src/portfolio.py`` and
prints the result as JSON to stdout. The /review-portfolio skill
invokes ``init-holdings`` (first-run branch only), ``wave-history``
(after each news pass), and ``analyze``; the cron jobs invoke
``snapshot``, ``recommend``, and ``dashboard``. ``backtest`` is a
one-off spot-check tool, not part of any cron flow. ``seed-wave-history``
is a one-time backfill for chart 4 trajectories.

Usage:
    python -m src.cli init-holdings      --allocations '{"AAPL": 5000, ...}' --out holdings.csv
    python -m src.cli wave-history       [--news data/news_latest.json] [--force]
    python -m src.cli seed-wave-history  [--force]
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
    # Load profile-driven defaults for the optimizer-related flags. Missing
    # profile / missing financial_model section -> hard-coded defaults so
    # nothing breaks. CLI flags still override the profile values explicitly.
    fm = portfolio.load_financial_model()

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

    p_an = sub.add_parser("analyze", help="fetch + optimize + risk in one call")
    p_an.add_argument("--tickers", nargs="+", required=True)
    p_an.add_argument("--period", default=fm["lookback_period"])
    p_an.add_argument("--objective", default=fm["objective"],
                      choices=["max_sharpe", "min_variance", "mean_variance"])
    p_an.add_argument("--max-weight", type=float, default=0.25)
    p_an.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_an.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
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
    p_rec.add_argument("--wave-history", default="data/wave_history.csv",
                       help="apply tilts from this file's most recent row at/before today; pass '' to skip")
    p_rec.add_argument("--period", default=fm["lookback_period"])
    p_rec.add_argument("--max-weight", type=float, default=0.25)
    p_rec.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_rec.add_argument("--objective", default=fm["objective"],
                       choices=["max_sharpe", "min_variance", "mean_variance"])
    p_rec.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                       help="lambda in mean_variance objective; see analyze --risk-aversion")
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
    # Parse "3y" -> 3 from the profile's lookback_period.
    import re as _re
    _m = _re.match(r"(\d+)", str(fm["lookback_period"]))
    _default_lookback_years = int(_m.group(1)) if _m else 3
    p_bt.add_argument("--lookback-years", type=int, default=_default_lookback_years,
                      help="optimizer lookback window in years; default 3 matches the live system")
    p_bt.add_argument("--max-weight", type=float, default=0.25)
    p_bt.add_argument("--objective", default=fm["objective"],
                      choices=["max_sharpe", "min_variance", "mean_variance"])
    p_bt.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                      help="lambda in mean_variance objective; see analyze --risk-aversion")
    p_bt.add_argument("--wave-history", default=None,
                      help="path to wave_history.csv; if given, the optimizer applies "
                           "time-varying wave-stage tilts at each rebalance based on the "
                           "most recent classification at or before that date")
    p_bt.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_bt.add_argument("--benchmarks", nargs="*", default=["SPY"],
                      help="benchmark tickers compared against the backtest's realized return "
                           "(default: SPY). Pass an empty list to skip the benchmark section.")

    p_dash = sub.add_parser("dashboard", help="generate docs/index.html from snapshots + recommendations + news + wave history")
    p_dash.add_argument("--snapshots", default="data/snapshots.csv")
    p_dash.add_argument("--recommendations", default="data/recommendations.csv")
    p_dash.add_argument("--news", default="data/news_latest.json")
    p_dash.add_argument("--wave-history", default="data/wave_history.csv")
    p_dash.add_argument("--benchmarks", nargs="*", default=["SPY"],
                        help="benchmark tickers to overlay on the portfolio-value chart "
                             "(default: SPY). Pass an empty list to suppress overlays.")
    p_dash.add_argument("--out", default="docs/index.html")
    p_dash.add_argument("--nav-current", default=None,
                        choices=["live", "backtest", "lambda", "max_weight"],
                        help="if set, prepend a cross-page nav strip to the rendered HTML "
                             "with the named page highlighted as current (used in docs/)")
    p_dash.add_argument("--thesis-baseline", default="data/thesis_baseline.json",
                        help="if the file exists, time-series charts are scoped to dates "
                             ">= the thesis date. Pass an empty string to disable (the "
                             "backtest dashboard does this since its data predates any thesis).")

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
        elif args.cmd == "backtest":
            result = portfolio.backtest(
                holdings_path=args.holdings,
                start_date=args.start_date, end_date=args.end_date,
                initial_usd=args.initial_usd, out_dir=args.out_dir,
                lookback_years=args.lookback_years,
                max_weight=args.max_weight, objective=args.objective,
                risk_aversion=args.risk_aversion,
                tilt_schedule=fm["wave_stage_tilts"],
                risk_free_rate=args.risk_free_rate,
                benchmarks=args.benchmarks,
                wave_history_path=args.wave_history,
            )
        elif args.cmd == "analyze":
            result = portfolio.analyze(
                args.tickers, period=args.period, objective=args.objective,
                max_weight=args.max_weight, risk_free_rate=args.risk_free_rate,
                risk_aversion=args.risk_aversion,
                tilt_schedule=fm["wave_stage_tilts"],
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
                wave_history_path=args.wave_history or "",
                period=args.period, max_weight=args.max_weight,
                risk_free_rate=args.risk_free_rate, objective=args.objective,
                risk_aversion=args.risk_aversion,
                tilt_schedule=fm["wave_stage_tilts"],
                date=args.date, force=args.force,
            )
        else:  # dashboard
            result = portfolio.build_dashboard(
                snapshots_path=args.snapshots,
                recommendations_path=args.recommendations,
                out_path=args.out,
                wave_history_path=args.wave_history,
                benchmarks=args.benchmarks,
                nav_current=args.nav_current,
                thesis_baseline_path=args.thesis_baseline or None,
            )
            # Side-effect: refresh docs/news.html from the latest
            # /review-portfolio news payload so cron and the slash command
            # keep the news page in sync with the dashboard. Path is
            # derived from --out so e.g. data/dashboard.html keeps its
            # news next to it at data/news.html.
            news_out = str(Path(args.out).with_name("news.html"))
            news_result = portfolio.render_news_page(
                news_path=args.news, out_path=news_out,
            )
            result["news_page"] = news_result
    except Exception as e:  # noqa: BLE001 — surface any failure as a JSON error line
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1

    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
