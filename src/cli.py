"""Single CLI for every portfolio operation a subagent might need.

Each subcommand calls one function in ``src/portfolio.py`` and prints its
result as JSON to stdout. Subagents invoke this via Bash and parse the
JSON from the last command's output.

Usage:
    python -m src.cli fetch-data --tickers AAPL MSFT NVDA --period 3y
    python -m src.cli optimize  --returns-handle returns_1 --objective max_sharpe --max-weight 0.35
    python -m src.cli risk      --returns-handle returns_1 --weights '{"AAPL": 0.5, "MSFT": 0.5}'
    python -m src.cli backtest  --returns-handle returns_1 --weights weights.json --train-fraction 0.7
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import portfolio


def _load_weights(arg: str) -> dict[str, float]:
    """Accept either a JSON literal or a path to a JSON file."""
    raw = json.loads(arg) if arg.startswith("{") else json.loads(Path(arg).read_text())
    return {str(k).upper(): float(v) for k, v in raw.items()}


def _load_wave_views(arg: str) -> dict[str, str]:
    """Accept either a JSON literal or a path to a JSON file mapping ticker -> stage."""
    raw = json.loads(arg) if arg.startswith("{") else json.loads(Path(arg).read_text())
    return {str(k).upper(): str(v) for k, v in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.cli", description="Portfolio math CLI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch-data", help="fetch prices + compute returns")
    p_fetch.add_argument("--tickers", nargs="+", required=True)
    p_fetch.add_argument("--period", default="3y")
    p_fetch.add_argument("--interval", default="1d", choices=["1d", "1wk", "1mo"])
    p_fetch.add_argument("--frequency", default="daily", choices=["daily", "weekly", "monthly"])

    p_opt = sub.add_parser("optimize", help="mean-variance optimization")
    p_opt.add_argument("--returns-handle", required=True)
    p_opt.add_argument("--objective", default="max_sharpe",
                       choices=["max_sharpe", "min_variance", "target_return"])
    p_opt.add_argument("--risk-free-rate", type=float, default=0.04)
    p_opt.add_argument("--target-return", type=float, default=None)
    p_opt.add_argument("--max-weight", type=float, default=1.0)
    p_opt.add_argument("--min-weight", type=float, default=0.0)
    p_opt.add_argument("--wave-views", default=None,
                       help="JSON literal or path mapping ticker -> wave stage "
                            "(buildup|surge|peak|digestion|neutral). Tilts expected returns.")

    p_risk = sub.add_parser("risk", help="risk metrics for a weight vector")
    p_risk.add_argument("--returns-handle", required=True)
    p_risk.add_argument("--weights", required=True, help="JSON literal or path to JSON file")
    p_risk.add_argument("--risk-free-rate", type=float, default=0.04)
    p_risk.add_argument("--var-confidence", type=float, default=0.95)

    p_bt = sub.add_parser("backtest", help="in/out-of-sample backtest")
    p_bt.add_argument("--returns-handle", required=True)
    p_bt.add_argument("--weights", required=True)
    p_bt.add_argument("--train-fraction", type=float, default=0.7)
    p_bt.add_argument("--risk-free-rate", type=float, default=0.04)

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

    args = parser.parse_args(argv)

    try:
        if args.cmd == "fetch-data":
            prices = portfolio.fetch_prices(args.tickers, period=args.period, interval=args.interval)
            returns = portfolio.compute_returns(prices["prices_handle"], frequency=args.frequency)
            result = {"prices": prices, "returns": returns}
        elif args.cmd == "optimize":
            result = portfolio.optimize_portfolio(
                args.returns_handle, objective=args.objective,
                risk_free_rate=args.risk_free_rate, target_return=args.target_return,
                max_weight=args.max_weight, min_weight=args.min_weight,
                wave_views=_load_wave_views(args.wave_views) if args.wave_views else None,
            )
        elif args.cmd == "risk":
            result = portfolio.risk_metrics(
                args.returns_handle, _load_weights(args.weights),
                risk_free_rate=args.risk_free_rate, var_confidence=args.var_confidence,
            )
        elif args.cmd == "backtest":
            result = portfolio.backtest(
                args.returns_handle, _load_weights(args.weights),
                train_fraction=args.train_fraction, risk_free_rate=args.risk_free_rate,
            )
        elif args.cmd == "snapshot":
            result = portfolio.snapshot_holdings(
                holdings_path=args.holdings, out_path=args.out,
                date=args.date, force=args.force,
            )
        else:  # recommend
            result = portfolio.recommend_portfolio(
                holdings_path=args.holdings, out_path=args.out,
                period=args.period, max_weight=args.max_weight,
                risk_free_rate=args.risk_free_rate, objective=args.objective,
                date=args.date, force=args.force,
            )
    except Exception as e:  # noqa: BLE001 — surface any failure as a JSON error line
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1

    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
