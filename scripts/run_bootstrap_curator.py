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
    p.add_argument("--out", default="docs/curator_bootstrap.html")
    p.add_argument("--heading", default="Curator Bootstrap")
    p.add_argument("--acronym", default="CBS")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--if-due", action="store_true")
    p.add_argument("--force", action="store_true")
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
    fw_pools = []
    d = date.fromisoformat(SINCE) if a.forward_only else date.fromisoformat(bt_pools[-1][0]) + timedelta(days=14)
    while d <= end:
        fw_pools.append((d.isoformat(), corpus.read_slice(d.isoformat(), news_lb), "websearch"))
        d += timedelta(days=14)

    pools = bt_pools + fw_pools     # already chronological
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
         "seed_src": SEED_SRC}, indent=2))     # remember the CBT run so the daily refresh can build the KPI table
    hist = RUN / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    hold = RUN / "_wf_holdings.csv"
    hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in starter + anchors))

    # Walk every date in order so the watchlist/history state rebuilds deterministically: dates that
    # already have a curation JSON are REPLAYED from disk (no LLM), missing dates are curated fresh
    # (reject-and-retry, same discipline as backtest_sdk / the live path).
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
                                 intro=curator.LIVE_INTRO, no_reasoning=True)
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
                                     intro=curator.LIVE_INTRO, no_reasoning=True, retry_feedback=fb)
            cur["as_of_date"] = dd
            cur_path.write_text(json.dumps(cur, indent=2))
        portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
              profile_path="investor_profile.md", listing_check=False, as_of_date=dd, max_watchlist_size=ms)
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
    portfolio.build_curator_dashboard(
        backtest_dir=str(RUN / "_backtest"), runs_dir=str(RUN), out_path=a.out,
        benchmarks=["SPY"], heading=a.heading, acronym=a.acronym, show_max_articles=False,
        handoff_date=HANDOFF, compare_backtest_dir=str(ROOT / SEED_SRC / "_backtest"))
    print(f"  rendered {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
