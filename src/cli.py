"""Single CLI for every portfolio operation.

Seven subcommands. Each calls one function in ``src/portfolio.py`` and
prints the result as JSON to stdout. The cron job invokes ``snapshot``
and ``dashboard``. ``curate`` applies a watchlist-curator JSON payload
to holdings.csv and appends to data/curation_history.csv. ``backtest``
is a math-only spot-check tool with no LLM in the loop; a curator-driven
walk-forward variant lands in stage C2.

Usage:
    python -m src.cli init-holdings      --allocations '{"AAPL": 5000, ...}' --out holdings.csv
    python -m src.cli analyze            --tickers AAPL MSFT NVDA --period 3y --max-weight 0.25
    python -m src.cli curate             --input curator_payload.json [--as-of-date YYYY-MM-DD]
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

    p_snap = sub.add_parser("snapshot", help="append today's $ values to data/snapshots.csv")
    p_snap.add_argument("--holdings", default="holdings.csv")
    p_snap.add_argument("--out", default="data/snapshots.csv")
    p_snap.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")
    p_snap.add_argument("--force", action="store_true",
                        help="overwrite an existing row for this date")

    p_rec = sub.add_parser("recommend", help="optimize and append weights to data/recommendations.csv")
    p_rec.add_argument("--holdings", default="holdings.csv")
    p_rec.add_argument("--out", default="data/recommendations.csv")
    p_rec.add_argument("--period", default=fm["lookback_period"])
    p_rec.add_argument("--max-weight", type=float, default=0.25)
    p_rec.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_rec.add_argument("--objective", default=fm["objective"],
                       choices=["max_sharpe", "min_variance", "mean_variance"])
    p_rec.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                       help="lambda in mean_variance objective; see analyze --risk-aversion")
    p_rec.add_argument("--date", default=None)
    p_rec.add_argument("--force", action="store_true")

    p_cur = sub.add_parser("curate",
                            help="apply a watchlist-curator JSON payload to holdings.csv + curation_history.csv")
    p_cur.add_argument("--input", required=True,
                       help="path to the curator agent's JSON output")
    p_cur.add_argument("--holdings", default="holdings.csv")
    p_cur.add_argument("--history", default="data/curation_history.csv")
    p_cur.add_argument("--profile", default="investor_profile.md")
    p_cur.add_argument("--as-of-date", default=None,
                       help="override the payload's as_of_date (used in backtest replays)")
    p_cur.add_argument("--no-listing-check", action="store_true",
                       help="skip the yfinance listing-date check on adds (offline tests)")

    p_bt = sub.add_parser("backtest",
                           help="walk-forward monthly-rebalance backtest; outputs to data/backtest/")
    p_bt.add_argument("--holdings", default="holdings.csv",
                      help="watchlist source; only the ticker column is used")
    p_bt.add_argument("--start-date", default=None,
                      help="YYYY-MM-DD; defaults to 12 months before --end-date")
    p_bt.add_argument("--end-date", default=None,
                      help="YYYY-MM-DD; defaults to yesterday")
    p_bt.add_argument("--initial-usd", type=float, default=50000.0,
                      help="starting portfolio value in dollars")
    p_bt.add_argument("--out-dir", default="data/backtest/")
    # Parse "1.3y" -> 1.3 from the profile's lookback_period.
    import re as _re
    _m = _re.match(r"(\d+(?:\.\d+)?)", str(fm["lookback_period"]))
    _default_lookback_years = float(_m.group(1)) if _m else 1.3
    p_bt.add_argument("--lookback-years", type=float, default=_default_lookback_years,
                      help="optimizer lookback window in years (default from investor_profile)")
    p_bt.add_argument("--max-weight", type=float, default=0.25)
    p_bt.add_argument("--objective", default=fm["objective"],
                      choices=["max_sharpe", "min_variance", "mean_variance"])
    p_bt.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                      help="lambda in mean_variance objective; see analyze --risk-aversion")
    p_bt.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_bt.add_argument("--benchmarks", nargs="*", default=["SPY"],
                      help="benchmark tickers compared against the backtest's realized return "
                           "(default: SPY). Pass an empty list to skip the benchmark section.")
    p_bt.add_argument("--curator-runs-dir", default=None,
                      help="path to a directory of curator JSON payloads "
                           "(<dir>/_starter.json + <date>-curation.json files). "
                           "When present, switches backtest into curator-driven mode: "
                           "walks the dir chronologically, applies each payload to a "
                           "sandboxed holdings + history, optimizes on the resulting "
                           "watchlist, and computes two baselines (fixed-watchlist same "
                           "cadence; buy-and-hold of starter) for comparison.")

    p_dash = sub.add_parser("dashboard", help="generate docs/index.html from snapshots + recommendations + news")
    p_dash.add_argument("--snapshots", default="data/snapshots.csv")
    p_dash.add_argument("--recommendations", default="data/recommendations.csv")
    p_dash.add_argument("--news", default="data/news_latest.json")
    p_dash.add_argument("--benchmarks", nargs="*", default=["SPY"],
                        help="benchmark tickers to overlay on the portfolio-value chart "
                             "(default: SPY). Pass an empty list to suppress overlays.")
    p_dash.add_argument("--out", default="docs/index.html")
    p_dash.add_argument("--nav-current", default=None,
                        choices=["lambda", "max_weight", "lookback"],
                        help="if set, prepend the sweep-page nav strip with the named "
                             "page highlighted. Only the three sweep pages cross-link; "
                             "live/backtest/news are leaves reachable from the README.")
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
        elif args.cmd == "backtest":
            if args.curator_runs_dir:
                result = portfolio.curator_backtest(
                    runs_dir=args.curator_runs_dir,
                    out_dir=args.out_dir,
                    max_weight=args.max_weight,
                    objective=args.objective,
                    risk_aversion=args.risk_aversion,
                    risk_free_rate=args.risk_free_rate,
                    benchmarks=args.benchmarks,
                )
            else:
                result = portfolio.backtest(
                    holdings_path=args.holdings,
                    start_date=args.start_date, end_date=args.end_date,
                    initial_usd=args.initial_usd, out_dir=args.out_dir,
                    lookback_years=args.lookback_years,
                    max_weight=args.max_weight, objective=args.objective,
                    risk_aversion=args.risk_aversion,
                    risk_free_rate=args.risk_free_rate,
                    benchmarks=args.benchmarks,
                )
        elif args.cmd == "analyze":
            result = portfolio.analyze(
                args.tickers, period=args.period, objective=args.objective,
                max_weight=args.max_weight, risk_free_rate=args.risk_free_rate,
                risk_aversion=args.risk_aversion,
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
                risk_aversion=args.risk_aversion,
                date=args.date, force=args.force,
            )
        elif args.cmd == "curate":
            payload = json.loads(Path(args.input).read_text())
            result = portfolio.apply_curator_decisions(
                payload,
                holdings_path=args.holdings,
                history_path=args.history,
                profile_path=args.profile,
                listing_check=not args.no_listing_check,
                as_of_date=args.as_of_date,
            )
        else:  # dashboard
            result = portfolio.build_dashboard(
                snapshots_path=args.snapshots,
                recommendations_path=args.recommendations,
                out_path=args.out,
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
