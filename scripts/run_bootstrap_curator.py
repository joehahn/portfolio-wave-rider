#!/usr/bin/env python3
"""Run the curator over the BOOTSTRAP dataset (1-year backtest tail + forward WebSearch) and replay through
the optimizer, producing the Curator Bootstrap (CBS) dashboard (docs/curator_bootstrap.html).

Design (biweekly throughout -- matches the profile's rebalance_period):
  - ONE biweekly rebalance timeline from SINCE to today.
  - Each rebalance reads a trailing news_lookback_days window from the right source:
      * date <= HANDOFF : canon14's own biweekly GKG+Wayback pool (news already 14d-windowed at build time).
      * date >  HANDOFF : the live forward corpus via corpus.read_slice(date, news_lb) (daily WebSearch
                          ingests, surfaced as a trailing 14d window -- the SAME reader the live path uses).
  - Day-0 portfolio = the CBT (canon14) RECOMMENDED weights on the nearest rebalance <= SINCE, so the CBS
    continues the backtest's recommended portfolio into the bootstrap.
  - Optimizer config + curator params all come from investor_profile.md (the canonical settings).

Lookbacks: the optimizer's 90d price window is auto-fetched by curator_backtest (start - 120d); the
curator's 14d news window is satisfied by the per-date pools above. The curator is fired ONCE per biweekly
date (not per daily ingest); curator_backtest batches decisions and rebalances biweekly.

INCREMENTAL, so it is safe to schedule: a date that already has a `<date>-curation.json` is replayed from
that file (no LLM call, no cost); only dates missing one are curated. Flags:
  --dry-run  print the plan (dates, seed, pool sizes, which dates would be curated) and write nothing.
  --if-due   cron mode: exit immediately when every rebalance date already has a curation.
  --force    delete the pool + curation JSONs first and re-curate the whole window from scratch.
  --report   write data/reports/<date>-<acronym>-curation.md for each date curated on this run.

PARAMETERIZED, so the same machinery drives both paper portfolios; they differ only in seed date and news
source, which is what makes comparing them meaningful:
  CBS (default) : --since 2026-04-22, backtest-tail GKG pools up to the handoff, forward corpus after.
  FT            : --forward-only --since 2026-07-22 --run-dir .../forward-ft --out docs/index.html
                  --heading Forwardtest --acronym FT   (no backtest news at all).
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import corpus, curator, portfolio  # noqa: E402


def _args(argv=None):
    p = argparse.ArgumentParser(description="Curate a forward paper portfolio and render its dashboard.")
    p.add_argument("--run-dir", default="data/curator_runs/bootstrap-cbs")
    p.add_argument("--pool-src", default="data/curator_runs/gkg-3yr-geosplit",
                   help="backtest-tail news pools (ignored with --forward-only)")
    p.add_argument("--seed-src", default="data/curator_runs/proto-mws16",
                   help="the canonical CBT run: day-0 seed weights + starter watchlist come from here")
    p.add_argument("--since", default="2026-04-22", help="day-0 of the paper portfolio")
    p.add_argument("--handoff", default="2026-07-22", help="last backtest-news date; forward corpus after it")
    p.add_argument("--forward-only", action="store_true",
                   help="read EVERY date from the live corpus (no backtest news); FT mode")
    p.add_argument("--blend-backtest-news", action="store_true",
                   help="with --forward-only: also feed backtest-tail articles that fall inside the\n"
                        "trailing news window, so an early forward date is not starved. The blend\n"
                        "weans itself off automatically as the window clears the handoff date.")
    p.add_argument("--out", default="docs/curator_bootstrap.html")
    p.add_argument("--heading", default="Curator Bootstrap")
    p.add_argument("--acronym", default="CBS")
    p.add_argument("--actual-csv", default="", help="real snapshots.csv to overlay (FT only)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--if-due", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--rebalance-now", action="store_true",
                   help="insert an EXTRA off-grid rebalance today, on top of the regular cadence")
    p.add_argument("--extra-date", default="", help="insert an extra off-grid rebalance on this date")
    p.add_argument("--report", action="store_true",
                   help="write a markdown report to data/reports/ for each date curated on this run")
    p.add_argument("--report-backfill", action="store_true",
                   help="with --report: write one for EVERY curated date, not just this run's")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = _args(argv)
    dry, if_due, force = a.dry_run, a.if_due, a.force
    POOL_SRC, SEED_SRC, SINCE, HANDOFF = a.pool_src, a.seed_src, a.since, a.handoff
    RUN = ROOT / a.run_dir
    RUN.mkdir(parents=True, exist_ok=True)
    if force and not dry:
        for f in list(RUN.glob("*-pool.json")) + list(RUN.glob("*-curation.json")):
            f.unlink()

    fm = portfolio.load_financial_model()
    anchors = [t.upper() for t in (fm.get("always_include") or ["SPY", "AGG", "IAU"])]
    ms = int(fm["max_watchlist_size"])
    news_lb = int(fm["news_lookback_days"])
    model = portfolio.load_forward_config().get("curator_model") or "moonshotai/kimi-k2.5"
    # claude-* routes through the Anthropic SDK and REQUIRES a client; without it every call raises
    # AttributeError, burns the retry budget, and silently degrades to a no_changes curation.
    cli_a = curator.anthropic_client() if model.startswith("claude") else None
    thesis = portfolio.load_wave_thesis()      # from investor_profile.md (canonical thesis)
    excl = portfolio.load_exclusions()

    end = date.today()
    # --- Backtest-tail pools: the canonical run's own biweekly pools in [SINCE, HANDOFF] (14d Wayback news
    #     baked in). FT skips these entirely: --forward-only reads every date from the live corpus.
    bt_pools = []
    if not a.forward_only:
        for f in sorted((ROOT / POOL_SRC).glob("*-pool.json")):
            d = f.stem.replace("-pool", "")
            if SINCE <= d <= HANDOFF:
                bt_pools.append((d, json.loads(f.read_text()).get("articles", []), "gkg-wayback"))
        if not bt_pools:
            print(f"no pools in [{SINCE}, {HANDOFF}] under {POOL_SRC}", file=sys.stderr)
            return 1

    # --- Forward dates: biweekly, each read as a trailing news_lb window over the live corpus. In
    #     --forward-only mode the timeline STARTS at SINCE; otherwise it continues from the last
    #     backtest-tail date (and is empty until the first forward biweekly date arrives).
    # Backtest-tail articles available for blending: every article in the backtest pools, keyed by its own
    # published date, so a forward date can pull in the ones inside its trailing window. Loaded once.
    _bt_articles = []
    if a.forward_only and a.blend_backtest_news:
        for f in sorted((ROOT / POOL_SRC).glob("*-pool.json")):
            for _art in json.loads(f.read_text()).get("articles", []):
                _ad = str(_art.get("date") or _art.get("published_date") or "")[:10]
                if _ad:
                    _bt_articles.append((_ad, _art))

    def _blend(_iso: str, _live: list) -> list:
        """live-corpus slice + any backtest-tail article published inside the same trailing window."""
        if not _bt_articles:
            return _live
        _lo = (date.fromisoformat(_iso) - timedelta(days=news_lb)).isoformat()
        _have = {str(x.get("url") or "") for x in _live}
        _extra = [x for _ad, x in _bt_articles
                  if _lo <= _ad <= _iso and str(x.get("url") or "") not in _have]
        return _live + _extra

    fw_pools = []
    d = date.fromisoformat(SINCE) if a.forward_only else date.fromisoformat(bt_pools[-1][0]) + timedelta(days=14)
    while d <= end:
        _iso = d.isoformat()
        fw_pools.append((_iso, _blend(_iso, corpus.read_slice(_iso, news_lb)), "websearch"))
        d += timedelta(days=14)

    # Off-grid manual rebalances. These are EXTRA dates merged into the regular cadence, never a
    # replacement for it: the biweekly grid still runs from SINCE, so a scheduled date keeps firing on
    # schedule. They persist in _starter.json (`manual_dates`) because this script rewrites that file on
    # every run, and `curator_backtest` globs the curation JSONs, so a merged date replays like any other.
    _prev_manual = []
    _sp = RUN / "_starter.json"
    if _sp.exists():
        try:
            _prev_manual = list(json.loads(_sp.read_text()).get("manual_dates") or [])
        except Exception:  # noqa: BLE001
            pass
    _manual = sorted({*_prev_manual, *([end.isoformat()] if a.rebalance_now else []),
                      *([a.extra_date] if a.extra_date else [])})
    _grid = {dd for dd, _, _ in bt_pools + fw_pools}
    for _md in _manual:
        if _md not in _grid and _md <= end.isoformat():
            fw_pools.append((_md, _blend(_md, corpus.read_slice(_md, news_lb)), "websearch"))

    pools = sorted(bt_pools + fw_pools, key=lambda r: r[0])
    dates = [dd for dd, _, _ in pools]
    if not dates:
        print(f"no rebalance dates in [{SINCE}, {end}]", file=sys.stderr)
        return 1

    # --- Seed: the canonical CBT run's RECOMMENDED weights on the nearest rebalance <= SINCE (the portfolio
    #     in effect when this paper portfolio starts). Anchors (e.g. IAU) stay in the seed weights; they are
    #     dropped from the curator's starter watchlist (optimizer anchors, not curator-managed tickers).
    recs = pd.read_csv(ROOT / SEED_SRC / "_backtest" / "recommendations.csv", parse_dates=["date"])
    seed_date = recs[recs.date <= pd.Timestamp(SINCE)]["date"].max()
    seed = recs[(recs["date"] == seed_date) & (recs["weight"] > 1e-6)]
    initial_weights = {str(r.ticker).upper(): float(r.weight) for r in seed.itertuples()}
    _tot = sum(initial_weights.values()) or 1.0
    initial_weights = {k: round(v / _tot, 6) for k, v in initial_weights.items()}   # renormalize off rounding
    # starter watchlist = canon14's active watchlist at SINCE (so the curator continues from there) plus any
    # seed ticker, minus anchors.
    periods, _ = portfolio._build_ticker_periods(SEED_SRC, fm["starter_watchlist"], pd.Timestamp(end.isoformat()))
    _at = pd.Timestamp(SINCE)
    starter = sorted(({tk for tk, s, e, _wb in periods if s <= _at <= e} | set(initial_weights)) - set(anchors))
    naive_benchmark = [str(t).upper() for t in fm["starter_watchlist"]]

    # INCREMENTAL: a date that already has a curation JSON is replayed from that file, never re-curated.
    # Only the missing dates cost an LLM call, so this is safe to run on a schedule.
    done = {f.stem.replace("-curation", "") for f in RUN.glob("*-curation.json")}
    missing = [dd for dd in dates if dd not in done]

    print(f"{a.acronym} plan: {len(bt_pools)} backtest-tail (biweekly GKG) + {len(fw_pools)} forward "
          f"(WebSearch) = {len(dates)} rebalances, {len(missing)} needing curation", file=sys.stderr)
    print(f"  window {dates[0]} -> {dates[-1]} (biweekly)  | seed @ {seed_date.date()}: {initial_weights}",
          file=sys.stderr)
    print(f"  starter watchlist: {starter}", file=sys.stderr)
    for dd, arts, src in pools:
        print(f"    {dd}  {src:11s} {len(arts):4d} articles  {'CURATE' if dd in missing else 'cached'}",
              file=sys.stderr)
    if dry:
        print("(dry run -- no curator calls, no files written)", file=sys.stderr)
        return 0
    if if_due and not missing:
        print(json.dumps({"as_of": end.isoformat(), "skipped": f"not due (all {len(dates)} dates curated, "
                          f"last {dates[-1]})"}))
        return 0

    # write pool JSONs + _starter.json
    for dd, arts, src in pools:
        (RUN / f"{dd}-pool.json").write_text(json.dumps(
            {"as_of_date": dd, "source": src, "n_articles": len(arts), "articles": arts}, indent=2))
    (RUN / "_starter.json").write_text(json.dumps(
        {"starter_watchlist": starter, "as_of_dates": dates, "rebalance_period": fm["rebalance_period"],
         "initial_usd": float(fm.get("initial_investment_usd", 50000.0)),
         "lookback_years": fm["optimizer_lookback_days"] / 365.0,
         "max_watchlist_size": ms, "start_date": dates[0], "end_date": end.isoformat(),
         "initial_weights": initial_weights, "naive_benchmark": naive_benchmark,
         "manual_dates": _manual,              # off-grid rebalances, re-merged on every later run
         "seed_src": SEED_SRC}, indent=2))     # remember the CBT run so the daily refresh can build the KPI table
    hist = RUN / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    hold = RUN / "_wf_holdings.csv"
    hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in starter + anchors))

    # Walk every date in order so the watchlist/history state rebuilds deterministically: dates that
    # already have a curation JSON are REPLAYED from disk (no LLM), missing dates are curated fresh
    # (reject-and-retry, same discipline as backtest_sdk / the live path).
    _fresh: list[tuple] = []      # dates curated on THIS run -> one report each
    for dd, arts, src in pools:
        cur_path = RUN / f"{dd}-curation.json"
        if cur_path.exists():
            cur, tag = json.loads(cur_path.read_text()), "cached"
        else:
            tag = "curated"
            cur_wl = portfolio.reconstruct_watchlist_at(dd, starter, str(hist))
            ptext = curator.format_pool(arts)
            cur = curator.curate(ptext, cur_wl, as_of=dd, model=model, anthropic_cli=cli_a,
                                 max_size=ms, anchors=anchors,
                                 thesis=thesis, exclusions=excl, cadence=fm["rebalance_period"],
                                 intro=curator.LIVE_INTRO, no_reasoning=True,
                                 # token usage -> _log/<date>-curator.json, which is what the dashboard's
                                 # "LLM cost" card sums; without it that card can only read n/a.
                                 log_path=RUN / "_log" / f"{dd}-curator.json",
                                 fail_dir=RUN / "_parse_fail")
            for _ in range(2):
                chk = portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
                      profile_path="investor_profile.md", listing_check=False, as_of_date=dd,
                      max_watchlist_size=ms, dry_run=True)
                rej = chk.get("rejections") or []
                if not rej:
                    break
                fb = "\n".join(f"- {x.get('ticker')} ({x.get('action')}): {x.get('reason')}" for x in rej)
                cur = curator.curate(ptext, cur_wl, as_of=dd, model=model, anthropic_cli=cli_a,
                                     max_size=ms, anchors=anchors,
                                     thesis=thesis, exclusions=excl, cadence=fm["rebalance_period"],
                                     intro=curator.LIVE_INTRO, no_reasoning=True, retry_feedback=fb,
                                     log_path=RUN / "_log" / f"{dd}-curator.json",
                                     fail_dir=RUN / "_parse_fail")
            cur["as_of_date"] = dd
            cur_path.write_text(json.dumps(cur, indent=2))
        _applied = portfolio.apply_curator_decisions(
              cur, holdings_path=str(hold), history_path=str(hist),
              profile_path="investor_profile.md", listing_check=False, as_of_date=dd, max_watchlist_size=ms)
        if tag == "curated":
            _fresh.append((dd, cur, _applied, len(arts)))
        print(f"  {dd} [{src}] {tag}: adds={[x['ticker'] for x in cur.get('adds', [])]} "
              f"removes={[x['ticker'] for x in cur.get('removes', [])]}", file=sys.stderr)

    # replay through the optimizer at the canonical config (all from investor_profile.md), then render
    res = portfolio.curator_backtest(
        runs_dir=str(RUN), out_dir=str(RUN / "_backtest"), max_weight=float(fm["concentration_cap"]),
        risk_aversion=float(fm["risk_aversion"]), risk_free_rate=float(fm["risk_free_rate"]),
        t_update_days=int(portfolio.load_backtest_config()["t_update_days"]), benchmarks=["SPY"],
        lookback_years_override=fm["optimizer_lookback_days"] / 365.0, always_include=anchors,
        min_trade_frac=float(fm["min_trade_size_frac"]))
    authors = {}
    for pf in RUN.glob("*-pool.json"):
        for art in json.loads(pf.read_text()).get("articles", []):   # NB: not `a` -- that is the arg namespace
            if art.get("author"):
                authors.setdefault(art.get("url", ""), art["author"])
    (RUN / "_authors.json").write_text(json.dumps(authors, indent=1))
    print(f"\n=== {a.acronym} RESULT: {res['realized_return']*100:+.0f}% (final ${res['final_value']:,.0f}) | "
          f"SPY {res['benchmark_returns']['SPY']*100:+.0f}% | final {res['final_watchlist']}", file=sys.stderr)
    # One markdown report per date curated on THIS run, only when --report is passed. Off by default
    # because reporting belongs to the recommendation of record (FT), not to curation in general: CBS is
    # a comparison portfolio and stays out of data/reports/. Same writer and format as the live
    # `cli review` path, so every report reads the same wherever it fired from. The recommended weights
    # come from the replay's own last rebalance block.
    _want = _fresh
    if a.report and a.report_backfill:      # every curated date, using each one's own decision + weights
        _want = []
        for _dd0, _arts0, _src0 in pools:
            _cp0 = RUN / f"{_dd0}-curation.json"
            if _cp0.exists():
                _c0 = json.loads(_cp0.read_text())
                _want.append((_dd0, _c0,
                              {"applied_adds": [x.get("ticker") for x in _c0.get("adds") or []],
                               "applied_removes": [x.get("ticker") for x in _c0.get("removes") or []]},
                              len(_arts0)))
    if _want and a.report:
        try:
            _rc = pd.read_csv(RUN / "_backtest" / "recommendations.csv")
        except Exception:  # noqa: BLE001
            _rc = None

        def _rec_at(_when):
            """The optimizer block for the rebalance this decision produced: the first recommendation
            dated on/after the decision (execution can lag it by t_update_days), else the latest."""
            if _rc is None or _rc.empty:
                return {}
            _sel = _rc[_rc["date"] >= _when]
            _blk = _sel[_sel["date"] == _sel["date"].min()] if not _sel.empty else \
                _rc[_rc["date"] == _rc["date"].max()]
            # `as_of` = the date these weights were actually computed for. When it predates the decision
            # (an on-demand rebalance on a day no session has priced yet), the report labels them as
            # carried forward rather than printing stale weights as if they were this cycle's.
            return {"weights": {str(r.ticker): float(r.weight) for r in _blk.itertuples()},
                    "as_of": str(_blk["date"].iloc[0])[:10],
                    "sharpe_ratio": float(_blk["sharpe_ratio"].iloc[0]),
                    "expected_annual_return": float(_blk["expected_return"].iloc[0]),
                    "annual_volatility": float(_blk["annual_volatility"].iloc[0])}

        _pool_by_date = {_d0: _arts0 for _d0, _arts0, _s0 in pools}
        for _dd, _cur, _app, _nart in _want:
            _rec = _rec_at(_dd)
            # watchlist AFTER this call, replayed from the history file (same source the dashboard uses)
            try:
                _wl_after = portfolio.reconstruct_watchlist_at(
                    (pd.Timestamp(_dd) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), starter, str(hist))
            except Exception:  # noqa: BLE001
                _wl_after = None
            _rp = portfolio.write_review_report(
                _dd, _cur, _app, _rec, _nart, news_lb, model,
                label=f"{a.acronym.lower()}-curation", title=f"{a.heading} ({a.acronym}) curation",
                watchlist=_wl_after, max_size=ms, pool=_pool_by_date.get(_dd))
            print(f"  report: {_rp}", file=sys.stderr)

    portfolio.build_curator_dashboard(
        backtest_dir=str(RUN / "_backtest"), runs_dir=str(RUN), out_path=a.out,
        benchmarks=["SPY"], heading=a.heading, acronym=a.acronym, show_max_articles=False,
        handoff_date=HANDOFF, compare_backtest_dir=str(ROOT / SEED_SRC / "_backtest"),
        actual_csv=(a.actual_csv or None))
    print(f"  rendered {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
