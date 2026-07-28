#!/usr/bin/env python3
"""Augment the geo-split titles pools with LIVE ledes: fetch each article URL's CURRENT page (parallel,
NO Wayback) and store it in `lede_live`. Look-ahead-BIASED (today's page may postdate the article's as_of),
so this is a fast backtest PROTOTYPE input only -- the look-ahead-clean Wayback ledes replace it overnight.

Idempotent: _apply_live_fallback skips any article that already has a lede_live, so a re-run only fills gaps.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import backtest_sdk as B  # noqa: E402

RUN = ROOT / "data" / "curator_runs" / "gkg-3yr-geosplit"


def main() -> int:
    pools = sorted(RUN.glob("*-pool.json"))
    for i, f in enumerate(pools):
        d = json.loads(f.read_text())
        arts = d.get("articles", [])
        B._apply_live_fallback(arts, all_urls=True)   # parallel live fetch (no Wayback); idempotent per-article
        f.write_text(json.dumps({"as_of_date": d.get("as_of_date"), "articles": arts}))
        got = sum(1 for a in arts if a.get("lede_live"))
        print(f"{i + 1}/{len(pools)} {f.stem[:10]}: {got}/{len(arts)} live ledes", file=sys.stderr)
    print("DONE live ledes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
