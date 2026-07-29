#!/usr/bin/env python3
"""GENTLE cold Wayback-lede fill for the geosplit pools (all URLs, throttle-avoiding).

Fetches each geosplit article's look-ahead-CLEAN archived lede (as-of its publish date) and stores it in
`lede` (+ lede_source=wayback), so the clean re-curation can read Wayback instead of the biased live ledes.
Pool-level and mws-INDEPENDENT: one pass serves all proto-mws{N} runs.

Throttle-avoidance: archive.org tightened rate limits after its Oct-2024 outage, and the default 0.25s pacer
(~4 req/s) trips it on a cold bulk pull. Here the pacer is relaxed to ~1 req/s and workers cut to 4; the
built-in exponential backoff honoring Retry-After handles any residual 429s. Idempotent: per-URL caching
means a re-run is all cache hits, so a throttle-interrupted run resumes for free.

Usage: python scripts/fetch_wayback_ledes.py [interval_seconds]   # default 1.0
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import news_pool as w  # noqa: E402

RUN = ROOT / "data" / "curator_runs" / "gkg-3yr-geosplit"
WORKERS = 4


def main() -> int:
    w.WAYBACK_MIN_INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0  # ~1 req/s polite pace
    w.RUN_DIR = RUN
    w.CACHE = RUN / "_cache"           # keep wayback cache with the pools (resume-friendly)
    w.CACHE.mkdir(exist_ok=True)
    print(f"gentle wayback fill: pace {w.WAYBACK_MIN_INTERVAL}s (~{1/w.WAYBACK_MIN_INTERVAL:.1f} req/s), "
          f"{WORKERS} workers", file=sys.stderr)
    pools = sorted(RUN.glob("*-pool.json"))
    grand_urls = grand_got = 0
    for i, f in enumerate(pools):
        d = json.loads(f.read_text())
        arts = d.get("articles", [])
        pairs = []
        for a in arts:
            u, ds = a.get("url"), (a.get("date") or d.get("as_of_date") or "")
            if u and ds:
                try:
                    pairs.append((u, date.fromisoformat(ds[:10])))
                except ValueError:
                    pass
        ledes = w.wayback_ledes_dated(pairs, workers=WORKERS)
        got = 0
        for a in arts:
            lede = ledes.get(a.get("url", ""))
            if lede:
                a["lede"] = lede
                a["lede_source"] = "wayback"
                got += 1
        f.write_text(json.dumps({"as_of_date": d.get("as_of_date"), "articles": arts}))
        grand_urls += len(pairs); grand_got += got
        st = w._WB_STAT
        print(f"{i + 1}/{len(pools)} {f.stem[:10]}: {got}/{len(arts)} wayback "
              f"(cum {grand_got}/{grand_urls}) | reqs={st['requests']} 429={st['http_429']} "
              f"5xx={st['http_5xx']} timeout={st['timeout']}", file=sys.stderr)
    print(f"DONE wayback fill: {grand_got}/{grand_urls} clean ledes; "
          f"429s={w._WB_STAT['http_429']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
