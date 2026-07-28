#!/usr/bin/env python3
"""Titles-only max_watchlist_size PROTOTYPE sweep.

Re-curates canon14's pools at the NEW (geo-split) thesis for a given max_watchlist_size, using GKG TITLES
ONLY (no Wayback, no live ledes) so it is fast and clean (period-correct headlines). Writes curations to
data/curator_runs/proto-mws{N}/ for the free optimizer/param sweep (scripts) to replay.

Reuses canon14's on-disk pools -- no GKG re-ingest, no Wayback. This is a fast PROXY to find promising
(mws, cap, lambda, lookback, min_trade) regions; the winner is confirmed on FULLER ledes later.

Usage:  python scripts/sweep_mws_proto.py <mws>        # curate one watchlist size (run several in parallel)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import curator, portfolio  # noqa: E402

CANON = ROOT / "data" / "curator_runs" / "gkg-3yr-geosplit"   # geo-split titles pools (4 geo sub-wave budgets)


def _titles(arts):
    """Render the pool for the curator: title + LIVE lede (`lede_live`) when present, else title-only. The
    same function serves the titles-only prototype AND the titles+live prototype (pools augmented by
    fetch_live_ledes.py). Wayback ledes (`lede`) are used if present (the overnight clean pass)."""
    lines = ["DATE-CLEAN NEWS ARTICLES (title + snippet). Discover the tickers; discard non-investable noise "
             "(war/weather events, private cos, foreign/OTC, keyword false-matches):"]
    for a in arts:
        snip = (a.get("lede_live") or a.get("lede") or a.get("title", ""))[:220]
        lines.append(f"\n[{a.get('date', '')} | {a.get('source', '')}] {a.get('title', '')[:90]}\n"
                     f"   {snip} ({a.get('url', '')})")
    return "\n".join(lines)


def main() -> int:
    ms = int(sys.argv[1])
    fm = portfolio.load_financial_model()
    anchors = [t.upper() for t in (fm.get("always_include") or [])]
    model = portfolio.load_forward_config().get("curator_model") or "moonshotai/kimi-k2.5"
    thesis = portfolio.load_wave_thesis()      # NEW geo-split thesis
    excl = portfolio.load_exclusions()
    starter = fm["starter_watchlist"]
    cadence = fm["rebalance_period"]

    st = json.loads((CANON / "_starter.json").read_text())
    dates = st["as_of_dates"]
    pools = {d: json.loads((CANON / f"{d}-pool.json").read_text()).get("articles", []) for d in dates}

    RUN = ROOT / "data" / "curator_runs" / f"proto-mws{ms}"
    RUN.mkdir(parents=True, exist_ok=True)
    for f in list(RUN.glob("*-curation.json")):
        f.unlink()
    _st = dict(st)
    _st["max_watchlist_size"] = ms
    (RUN / "_starter.json").write_text(json.dumps(_st, indent=2))
    hist = RUN / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    hold = RUN / "_wf_holdings.csv"
    hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in starter + anchors))

    for i, d in enumerate(dates):
        wl = portfolio.reconstruct_watchlist_at(d, starter, str(hist))
        ptext = _titles(pools[d])
        cur = curator.curate(ptext, wl, as_of=d, model=model, max_size=ms, anchors=anchors,
                             thesis=thesis, exclusions=excl, cadence=cadence,
                             intro=curator.LIVE_INTRO, no_reasoning=True)
        for _ in range(2):        # reject-and-retry (same discipline as the other curation paths)
            chk = portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
                  profile_path="investor_profile.md", listing_check=False, as_of_date=d,
                  max_watchlist_size=ms, dry_run=True)
            rej = chk.get("rejections") or []
            if not rej:
                break
            fb = "\n".join(f"- {x.get('ticker')} ({x.get('action')}): {x.get('reason')}" for x in rej)
            cur = curator.curate(ptext, wl, as_of=d, model=model, max_size=ms, anchors=anchors,
                                 thesis=thesis, exclusions=excl, cadence=cadence,
                                 intro=curator.LIVE_INTRO, no_reasoning=True, retry_feedback=fb)
        cur["as_of_date"] = d
        (RUN / f"{d}-curation.json").write_text(json.dumps(cur, indent=2))
        portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
              profile_path="investor_profile.md", listing_check=False, as_of_date=d, max_watchlist_size=ms)
        if (i + 1) % 10 == 0:
            print(f"mws{ms}: {i + 1}/{len(dates)}", file=sys.stderr)
    print(f"mws{ms}: DONE {len(dates)} dates -> {RUN}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
