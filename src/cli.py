"""Single CLI for every portfolio operation.

Seven subcommands. Each calls one function in ``src/portfolio.py`` and
prints the result as JSON to stdout. The cron job invokes ``snapshot``
and ``dashboard``. ``curate`` applies a watchlist-curator JSON payload
to holdings.csv and appends to data/curation_history.csv. ``backtest``
is a math-only spot-check tool with no LLM in the loop; a curator-driven
walk-forward variant lands in stage C2.

Usage:
    python -m src.cli init-holdings      --allocations '{"AAPL": 5000, ...}' --out holdings.csv
    python -m src.cli analyze            --tickers AAPL MSFT NVDA --period 0.5y [--max-weight 0.80]
    python -m src.cli curate             --input curator_payload.json [--as-of-date YYYY-MM-DD]
    python -m src.cli snapshot           [--date YYYY-MM-DD] [--force]
    python -m src.cli recommend          [--max-weight 0.80] [--force]
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


def _write_review_report(date, decision, apply_res, rec, n_articles, lookback, model):
    """Thin wrapper: the report writer itself lives in portfolio.py so the paper-portfolio
    runs (CBS / FT, via scripts/run_bootstrap_curator.py) emit the SAME report format."""
    return portfolio.write_review_report(date, decision, apply_res, rec, n_articles,
                                         lookback, model)


def main(argv: list[str] | None = None) -> int:
    # Load profile-driven defaults for the optimizer-related flags. Missing
    # profile / missing financial_model section -> hard-coded defaults so
    # nothing breaks. CLI flags still override the profile values explicitly.
    fm = portfolio.load_financial_model()
    bc = portfolio.load_backtest_config()

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
    p_an.add_argument("--max-weight", type=float, default=fm["concentration_cap"],
                      help="optimizer per-position max weight; defaults to the profile's "
                           "concentration_cap")
    p_an.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_an.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                      help="lambda in the mean-variance utility μᵀw - λ·wᵀΣw; "
                           "small λ favors return, large λ favors variance reduction. "
                           "The optimizer is always mean-variance — λ is the only knob.")

    p_snap = sub.add_parser("snapshot", help="append today's $ values to data/snapshots.csv")
    p_snap.add_argument("--holdings", default="holdings.csv")
    p_snap.add_argument("--out", default="data/snapshots.csv")
    p_snap.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")
    p_snap.add_argument("--force", action="store_true",
                        help="overwrite an existing row for this date")

    p_rec = sub.add_parser("recommend", help="optimize and append weights to data/recommendations.csv")
    p_rec.add_argument("--holdings", default="holdings.csv",
                       help="real positions; shares>0 rows join the optimizer universe")
    p_rec.add_argument("--watchlist", default="watchlist.csv",
                       help="curator-managed universe; unioned with held tickers + anchors")
    p_rec.add_argument("--out", default="data/recommendations.csv")
    p_rec.add_argument("--period", default=fm["lookback_period"])
    p_rec.add_argument("--max-weight", type=float, default=fm["concentration_cap"],
                       help="optimizer per-position max weight; defaults to the profile's "
                            "concentration_cap")
    p_rec.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_rec.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                       help="lambda in the mean-variance utility; see analyze --risk-aversion")
    p_rec.add_argument("--date", default=None)
    p_rec.add_argument("--force", action="store_true")

    p_cur = sub.add_parser("curate",
                            help="apply a watchlist-curator JSON payload to watchlist.csv + curation_history.csv")
    p_cur.add_argument("--input", required=True,
                       help="path to the curator agent's JSON output")
    p_cur.add_argument("--watchlist", default="watchlist.csv",
                       help="curator-managed watchlist file to mutate (a ticker,shares file also works)")
    p_cur.add_argument("--history", default="data/curation_history.csv")
    p_cur.add_argument("--profile", default="investor_profile.md")
    p_cur.add_argument("--as-of-date", default=None,
                       help="override the payload's as_of_date (used in backtest replays)")
    p_cur.add_argument("--no-listing-check", action="store_true",
                       help="skip the yfinance listing-date check on adds (offline tests)")

    p_bt = sub.add_parser("backtest",
                           help="walk-forward backtest; outputs to data/backtest/. The math-only "
                                "path (no --curator-runs-dir) is hardcoded to monthly rebalances "
                                "and ignores investor_profile.md's rebalance_period; only the "
                                "curator-driven path (--curator-runs-dir) respects the profile's "
                                "cadence via the runs dir's _starter.json")
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
    p_bt.add_argument("--max-weight", type=float, default=fm["concentration_cap"],
                      help="per-position cap (default from investor_profile's concentration_cap)")
    p_bt.add_argument("--risk-aversion", type=float, default=fm["risk_aversion"],
                      help="lambda in the mean-variance utility; see analyze --risk-aversion")
    p_bt.add_argument("--risk-free-rate", type=float, default=fm["risk_free_rate"])
    p_bt.add_argument("--min-trade-frac", type=float, default=fm["min_trade_size_frac"],
                      help="curator mode only: no-trade band -- suppress rebalancing trades smaller "
                           "than this fraction of the book (default from investor_profile's "
                           "min_trade_size_frac; 0 rebalances on every signal).")
    p_bt.add_argument("--benchmarks", nargs="*", default=["SPY"],
                      help="benchmark tickers compared against the backtest's realized return "
                           "(default: SPY). Pass an empty list to skip the benchmark section.")
    p_bt.add_argument("--t-update-days", type=int, default=bc["t_update_days"],
                      help="curator mode only: trading-day lag between a rebalance "
                           "signal (decided on the rebalance date's close) and the "
                           "trade actually landing. Models the gap between running a "
                           "review and placing the order. Defaults to t_update_days "
                           "in investor_profile.md (1 = next session); 0 reproduces "
                           "the optimistic same-close 'smart money' run.")
    p_bt.add_argument("--forward-split-date", default=bc["forward_split_date"],
                      help="curator mode only: split realized performance into "
                           "in-sample (<= this date) and out-of-sample (> this date) "
                           "segments in report.md and the dashboard -- the forward "
                           "test for overfitting. Reporting only; never affects the "
                           "optimizer or live recs. Defaults to forward_split_date in "
                           "investor_profile.md's backtest section (None = no split).")
    p_bt.add_argument("--curator-runs-dir", default=None,
                      help="path to a directory of curator JSON payloads "
                           "(<dir>/_starter.json + <date>-curation.json files). "
                           "When present, switches backtest into curator-driven mode: "
                           "walks the dir chronologically, applies each payload to a "
                           "sandboxed holdings + history, optimizes on the resulting "
                           "watchlist, and computes a buy-and-hold-of-starter baseline "
                           "for comparison.")

    p_dash = sub.add_parser("dashboard", help="generate docs/index.html from snapshots + recommendations")
    p_dash.add_argument("--snapshots", default="data/snapshots.csv")
    p_dash.add_argument("--recommendations", default="data/recommendations.csv")
    p_dash.add_argument("--benchmarks", nargs="*", default=["SPY"],
                        help="benchmark tickers to overlay on the portfolio-value chart "
                             "(default: SPY). Pass an empty list to suppress overlays.")
    p_dash.add_argument("--out", default="docs/index.html")
    p_dash.add_argument("--thesis-baseline", default="data/thesis_baseline.json",
                        help="if the file exists, time-series charts are scoped to dates "
                             ">= the thesis date. Pass an empty string to disable (the "
                             "backtest dashboard does this since its data predates any thesis).")
    p_dash.add_argument("--curator-backtest-dir", default=None,
                        help="if set, generate the curator-backtest dashboard instead of "
                             "the live dashboard. Reads snapshots.csv, baselines_totals.csv, "
                             "and curation_summary.json from this directory.")
    p_dash.add_argument("--curator-runs-dir", default=None,
                        help="only used with --curator-backtest-dir. Path to the runs dir "
                             "(contains _starter.json + dated *-curation.json files) so "
                             "the Gantt chart can color tickers by wave_bucket.")
    p_dash.add_argument("--curator-model", default=None,
                        help="only used with --curator-backtest-dir. The LLM that produced this run's "
                             "curations; shown in a parameter note above plot 1. MUST match the model that "
                             "actually curated the run (do NOT pass the profile default for a run curated by "
                             "another model).")

    p_pull = sub.add_parser("pull-news",
                            help="FORWARD news pull: run the profile's wave queries through Anthropic "
                                 "web_search and append the raw articles to the frozen corpus (data/forward_corpus/).")
    p_pull.add_argument("--as-of", default=None, help="logical date for this pull (default: today)")
    p_pull.add_argument("--model", default=None,
                        help="retrieval executor model; default: forward.retrieval_model in the profile")
    p_pull.add_argument("--limit-waves", type=int, default=None,
                        help="only pull the first N waves (smoke test)")
    p_pull.add_argument("--max-results", type=int, default=None,
                        help="cap results kept per query (smoke test)")
    p_pull.add_argument("--backfill", action="store_true",
                        help="one-time cold-start seed: fill the corpus with prior news via the GKG+Wayback "
                             "pipeline (BigQuery GKG discovery + Wayback lede + title-gated live fallback), "
                             "instead of a same-day WebSearch pull")
    p_pull.add_argument("--days", type=int, default=21, help="--backfill window in days (default 21)")
    p_pull.add_argument("--dry-run", action="store_true",
                        help="fetch and report what WOULD be pulled (sightings + new-article counts) "
                             "without writing anything to the frozen corpus")

    p_rev = sub.add_parser("review",
                           help="FORWARD rebalance: curate the watchlist from the corpus news slice, "
                                "re-optimize, and write a recommendation-only report. No trades executed.")
    p_rev.add_argument("--as-of", default=None, help="rebalance date (default: today)")
    p_rev.add_argument("--model", default=None, help="curator model; default: forward.curator_model")
    p_rev.add_argument("--news-lookback", type=int, default=None,
                       help="trailing news days the curator reads; default: forward.news_lookback_days")
    p_rev.add_argument("--dry-run", action="store_true",
                       help="curate only: print the decision, do NOT apply to holdings.csv or recommend")
    p_rev.add_argument("--if-due", action="store_true",
                       help="only run if a full rebalance_period has elapsed since the last review "
                            "(self-gating: safe to call daily from cron; handles catch-up + idempotency)")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "init-holdings":
            allocations = _load_allocations(args.allocations)
            prices_df = portfolio.fetch_prices(list(allocations.keys()), period="7d")
            last_prices = {t: float(prices_df[t].iloc[-1]) for t in prices_df.columns}
            result = portfolio.initialize_holdings(allocations, last_prices, holdings_path=args.out)
        elif args.cmd == "pull-news":
            from datetime import datetime
            from . import corpus, retriever
            fw = portfolio.load_forward_config()
            pulled_at = datetime.now().isoformat(timespec="seconds")
            as_of = args.as_of or pulled_at[:10]
            if args.backfill:
                pull_id = f"backfill-{pulled_at.replace(':', '').replace('-', '')}"
                r = retriever.GkgWaybackRetriever(days=args.days, max_articles=args.max_results or 160)
                sightings, query_stats = r.pull(pull_id, pulled_at)
                result = corpus.append_pull(pull_id, pulled_at, "gkg-wayback-backfill", f"{args.days}d",
                                            sightings, query_stats, dry_run=args.dry_run)
            else:
                waves = json.loads(Path("retrieval_config.json").read_text()).get("wave_keywords", {})
                if args.limit_waves:
                    waves = dict(list(waves.items())[: args.limit_waves])
                pull_id = f"pull-{pulled_at.replace(':', '').replace('-', '')}"
                model = args.model or fw["retrieval_model"]
                r = retriever.WebSearchRetriever(model, waves, max_results_per_query=args.max_results)
                sightings, query_stats = r.pull(pull_id, pulled_at)
                result = corpus.append_pull(pull_id, pulled_at, fw["retriever"], model, sightings, query_stats,
                                            dry_run=args.dry_run)
            result["as_of"] = as_of
        elif args.cmd == "review":
            from datetime import datetime
            import pandas as pd
            from . import corpus, curator
            fw = portfolio.load_forward_config()
            as_of = args.as_of or datetime.now().strftime("%Y-%m-%d")
            if args.if_due:   # self-gate: skip unless a full rebalance_period elapsed since the last review
                import glob
                from datetime import date
                _pd = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91}.get(fm["rebalance_period"], 7)
                _prior = sorted(glob.glob("data/curator_runs/live/2*-curation.json"))
                _last = _prior[-1].split("/")[-1][:10] if _prior else None
                if _last and (date.fromisoformat(as_of) - date.fromisoformat(_last)).days < _pd:
                    print(json.dumps({"as_of": as_of, "skipped": f"not due (last review {_last}, "
                                      f"{fm['rebalance_period']} cadence, need {_pd}d)"}))
                    return 0
            anchors = fm.get("always_include", [])
            # The live curation stream was retired on 2026-08-01 (FT is the recommendation of record), so
            # watchlist.csv no longer exists by default. Fail with an instruction instead of a traceback.
            if not Path("watchlist.csv").exists():
                raise SystemExit(
                    "watchlist.csv not found. The live review stream is retired -- the Forwardtest (FT) "
                    "paper portfolio is the recommendation of record, curated by "
                    "scripts/run_bootstrap_curator.py. To use `review` anyway, recreate watchlist.csv "
                    "(one `ticker` column) or seed it with `init-holdings`.")
            all_tk = pd.read_csv("watchlist.csv")["ticker"].astype(str).str.upper().tolist()
            watchlist = [t for t in all_tk if t not in anchors]      # anchors sit outside max_watchlist_size
            lookback = args.news_lookback or fw["news_lookback_days"]
            pool = corpus.read_slice(as_of, lookback)
            model = args.model or fw["curator_model"]
            live = Path("data/curator_runs/live")
            cli_a = curator.anthropic_client() if model.startswith("claude") else None
            decision = curator.curate(
                curator.format_pool(pool), watchlist, as_of=as_of, model=model, anthropic_cli=cli_a,
                thesis=portfolio.load_wave_thesis(), exclusions=portfolio.load_exclusions(),
                max_size=int(fm["max_watchlist_size"]), anchors=anchors, cadence=fm["rebalance_period"],
                intro=curator.LIVE_INTRO, no_reasoning=True,
                log_path=(None if args.dry_run else live / f"{as_of}-curator.json"),
                fail_dir=(None if args.dry_run else live / "_parse_fail"))
            if args.dry_run:
                # A dry run writes NOTHING: no curator log, no curation JSON, no holdings/report change --
                # and it leaves the --if-due cadence clock untouched (that gate reads live/*-curation.json).
                result = {"as_of": as_of, "articles_read": len(pool), "dry_run": True, "decision": decision}
            else:
                live.mkdir(parents=True, exist_ok=True)
                (live / f"{as_of}-curation.json").write_text(json.dumps(decision, indent=2))
                # Recommendation-only: apply updates the WATCHLIST (shares=0 adds; the validator blocks
                # removing a ticker with shares>0), never real share counts. Then re-optimize + report.
                apply_res = portfolio.apply_curator_decisions(decision, listing_check=True, as_of_date=as_of)
                rec = portfolio.recommend_portfolio(
                    period=fm["lookback_period"], max_weight=fm["concentration_cap"],
                    risk_free_rate=fm["risk_free_rate"], objective="mean_variance",
                    risk_aversion=fm["risk_aversion"], date=as_of, force=True)
                report = _write_review_report(as_of, decision, apply_res, rec, len(pool), lookback, model)
                result = {"as_of": as_of, "articles_read": len(pool),
                          "applied_adds": apply_res.get("applied_adds"),
                          "applied_removes": apply_res.get("applied_removes"),
                          "rejections": apply_res.get("rejections"),
                          "recommended_weights": rec.get("weights"), "report": report}
        elif args.cmd == "backtest":
            if args.curator_runs_dir:
                # Backtest-only optimizer overrides from investor_profile.md's
                # `backtest` section take precedence over the live values when set,
                # so a candidate config can be tested on the backtest before going live.
                # When unset (None), the live financial_model / concentration_cap values
                # (already the argparse defaults) are used, so backtest == live by default.
                ra = bc["risk_aversion"] if bc["risk_aversion"] is not None else args.risk_aversion
                mw = bc["concentration_cap"] if bc["concentration_cap"] is not None else args.max_weight
                lb = bc["lookback_years"] if bc["lookback_years"] is not None else args.lookback_years
                result = portfolio.curator_backtest(
                    runs_dir=args.curator_runs_dir,
                    out_dir=args.out_dir,
                    max_weight=mw,
                    objective="mean_variance",
                    risk_aversion=ra,
                    risk_free_rate=args.risk_free_rate,
                    benchmarks=args.benchmarks,
                    t_update_days=args.t_update_days,
                    lookback_years_override=lb,
                    forward_split_date=args.forward_split_date,
                    always_include=fm["always_include"],
                    min_trade_frac=args.min_trade_frac,
                )
            else:
                result = portfolio.backtest(
                    holdings_path=args.holdings,
                    start_date=args.start_date, end_date=args.end_date,
                    initial_usd=args.initial_usd, out_dir=args.out_dir,
                    lookback_years=args.lookback_years,
                    max_weight=args.max_weight, objective="mean_variance",
                    risk_aversion=args.risk_aversion,
                    risk_free_rate=args.risk_free_rate,
                    benchmarks=args.benchmarks,
                )
        elif args.cmd == "analyze":
            result = portfolio.analyze(
                args.tickers, period=args.period, objective="mean_variance",
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
                holdings_path=args.holdings, watchlist_path=args.watchlist, out_path=args.out,
                period=args.period, max_weight=args.max_weight,
                risk_free_rate=args.risk_free_rate, objective="mean_variance",
                risk_aversion=args.risk_aversion,
                date=args.date, force=args.force,
            )
        elif args.cmd == "curate":
            payload = json.loads(Path(args.input).read_text())
            result = portfolio.apply_curator_decisions(
                payload,
                holdings_path=args.watchlist,
                history_path=args.history,
                profile_path=args.profile,
                listing_check=not args.no_listing_check,
                as_of_date=args.as_of_date,
            )
        else:  # dashboard
            if args.curator_backtest_dir:
                # When --curator-backtest-dir is set, default --out flips to
                # docs/backtest_curator.html unless the caller overrode it.
                out_path = args.out
                if out_path == "docs/index.html":
                    out_path = "docs/backtest_curator.html"
                # No parameter note above plot 1: the Parameter-settings table below already lists these
                # knobs (read from the profile), so a duplicate line would just be redundant.
                config_note = None
                result = portfolio.build_curator_dashboard(
                    backtest_dir=args.curator_backtest_dir,
                    runs_dir=args.curator_runs_dir or "",
                    out_path=out_path,
                    benchmarks=args.benchmarks,
                    config_note=config_note,
                )
            else:
                result = portfolio.build_dashboard(
                    snapshots_path=args.snapshots,
                    recommendations_path=args.recommendations,
                    out_path=args.out,
                    benchmarks=args.benchmarks,
                    thesis_baseline_path=args.thesis_baseline or None,
                )
    except Exception as e:  # noqa: BLE001 — surface any failure as a JSON error line
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1

    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
