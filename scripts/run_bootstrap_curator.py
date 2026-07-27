#!/usr/bin/env python3
"""Run the curator over the BOOTSTRAP dataset (backtest tail + forward WebSearch) and replay through the
optimizer, producing a curator-backtest run dir (data/curator_runs/bootstrap-cbs/) for the Curator
Bootstrap (CBS) dashboard.

Feeds the SAME curator (src/curator.curate -> kimi, LIVE article-list intro) that the live forward path
uses over the 12 bootstrap pool dates (7 biweekly backtest-tail + 5 daily forward). Starter = canon14's
watchlist at the window start (continuity into the forward); optimizer config = the live profile (same as
the CBT). Idempotent-ish: overwrites the run dir each time so a re-run reflects the latest bootstrap.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import build_bootstrap_dashboard as bboot  # noqa: E402  reuse the bootstrap pool assembler
from src import curator, portfolio  # noqa: E402

CANON = "data/curator_runs/gkg-3yr-canon14"
SINCE = "2026-04-22"
RUN = ROOT / "data" / "curator_runs" / "bootstrap-cbs"


def main() -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    for f in list(RUN.glob("*-pool.json")) + list(RUN.glob("*-curation.json")):
        f.unlink()

    fm = portfolio.load_financial_model()
    anchors = fm.get("always_include") or ["SPY", "AGG", "IAU"]
    starter = list(fm["starter_watchlist"])   # profile inception holdings, e.g. [AAPL, GOOGL, AMZN]
    model = portfolio.load_forward_config().get("curator_model") or "moonshotai/kimi-k2.5"

    bt, fw = bboot.load_pools(CANON, "data/forward_corpus", SINCE)
    pools = sorted(bt + fw, key=lambda p: p["as_of_date"])
    dates = [p["as_of_date"] for p in pools]
    for p in pools:
        (RUN / f"{p['as_of_date']}-pool.json").write_text(json.dumps(p, indent=2))
    (RUN / "_starter.json").write_text(json.dumps(
        {"starter_watchlist": starter, "as_of_dates": dates, "rebalance_period": fm["rebalance_period"],
         "initial_usd": 50000.0, "lookback_years": fm["optimizer_lookback_days"] / 365.0,
         "max_watchlist_size": int(fm["max_watchlist_size"]), "start_date": dates[0], "end_date": dates[-1]},
        indent=2))
    hist = RUN / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    hold = RUN / "_wf_holdings.csv"
    hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in starter + anchors))
    print(f"starter @ {SINCE}: {starter} | {len(dates)} rebalances ({len(bt)} backtest + {len(fw)} forward)",
          file=sys.stderr)

    ms = int(fm["max_watchlist_size"])
    for d in dates:
        cur_wl = portfolio.reconstruct_watchlist_at(d, starter, str(hist))
        arts = json.loads((RUN / f"{d}-pool.json").read_text())["articles"]
        ptext = curator.format_pool(arts)
        cur = curator.curate(ptext, cur_wl, as_of=d, model=model, max_size=ms, anchors=anchors,
                             cadence=fm["rebalance_period"], intro=curator.LIVE_INTRO, no_reasoning=True)
        for _ in range(2):   # reject-and-retry (same discipline as backtest_sdk / the live path)
            chk = portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
                  profile_path="investor_profile.md", listing_check=False, as_of_date=d,
                  max_watchlist_size=ms, dry_run=True)
            rej = chk.get("rejections") or []
            if not rej:
                break
            fb = "\n".join(f"- {x.get('ticker')} ({x.get('action')}): {x.get('reason')}" for x in rej)
            cur = curator.curate(ptext, cur_wl, as_of=d, model=model, max_size=ms, anchors=anchors,
                                 cadence=fm["rebalance_period"], intro=curator.LIVE_INTRO, no_reasoning=True,
                                 retry_feedback=fb)
        cur["as_of_date"] = d
        (RUN / f"{d}-curation.json").write_text(json.dumps(cur, indent=2))
        portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
              profile_path="investor_profile.md", listing_check=False, as_of_date=d, max_watchlist_size=ms)
        print(f"  {d}: adds={[x['ticker'] for x in cur.get('adds', [])]} "
              f"removes={[x['ticker'] for x in cur.get('removes', [])]}", file=sys.stderr)

    res = portfolio.curator_backtest(
        runs_dir=str(RUN), out_dir=str(RUN / "_backtest"), max_weight=float(fm["concentration_cap"]),
        risk_aversion=float(fm["risk_aversion"]), benchmarks=["SPY"],
        lookback_years_override=fm["optimizer_lookback_days"] / 365.0, always_include=anchors)
    authors = {}
    for pf in RUN.glob("*-pool.json"):
        for a in json.loads(pf.read_text()).get("articles", []):
            if a.get("author"):
                authors.setdefault(a["url"], a["author"])
    (RUN / "_authors.json").write_text(json.dumps(authors, indent=1))
    print(f"\n=== CBS RESULT: {res['realized_return']*100:+.0f}% (final ${res['final_value']:,.0f}) | "
          f"SPY {res['benchmark_returns']['SPY']*100:+.0f}% | final {res['final_watchlist']}")
    # Render the Curator Bootstrap (CBS) dashboard from this run (parameterized clone of the CBT generator).
    portfolio.build_curator_dashboard(
        backtest_dir=str(RUN / "_backtest"), runs_dir=str(RUN), out_path="docs/curator_bootstrap.html",
        benchmarks=["SPY"], heading="Curator Bootstrap", acronym="CBS", show_max_articles=False,
        handoff_date="2026-07-22")
    print("  rendered docs/curator_bootstrap.html", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
