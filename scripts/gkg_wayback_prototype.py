#!/usr/bin/env python3
"""PROTOTYPE: forward-resembling backtest pool = date-clean ARTICLE LIST (title + Wayback lede).

Goal: make the backtest curator's input look like the live WebSearch curator's input — a list of
individual articles with title + snippet, NO volume/tone aggregates, curator discovers tickers.
This joins GKG's date-honest discovery (title, date, source, url) to Wayback ledes (the snippet),
dedupes syndicated titles, and hands the curator an article list instead of a company ranking.

Smoke test on one month to measure: Wayback hit-rate at recent dates, throttle behavior, and
whether the ledes add real snippet-depth over titles alone.

    python scripts/gkg_wayback_prototype.py --start 2026-05-01 --end 2026-05-31 --max 120

Reuses gkg_pool (query + filters) and news_pool.wayback_lede (GHR-grade: paced, backoff, cache).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gkg_pool as g
import news_pool as w

OUT_DIR = g.ROOT / "data" / "curator_runs" / "gkg-wayback-proto"


def build(start: str, end: str, max_articles: int, cutoff: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # GKG discovery (shared source_block/spam/wave/subject-org filters) -> collapse syndication + rank
    # (salience x authority, per-wave top-K). Both functions live in gkg_pool, the single source of
    # truth shared with the backtest ranker AND the forward GKG+Wayback backfill.
    arts = g.rank_stories(g.article_list(g._client(), start, end, OUT_DIR), max_articles)
    print(f"kept articles (ranked, top {max_articles}): {len(arts)}")

    # join Wayback ledes (the snippet). CUTOFF is the DECISION/as-of date, NOT each article's
    # publish date — GHR's key trick: archive.org captures a URL a few days AFTER publication, so
    # a decision-date cutoff (later, tolerating archival lag) finds those captures; a publish-date
    # cutoff rejects them and craters the hit-rate. The snapshot taken is the latest at-or-before
    # cutoff, so it stays look-ahead-clean (content as it existed by the decision date).
    cut = date.fromisoformat(cutoff)
    hit = 0
    for i, a in enumerate(arts):
        lede, ok = w.wayback_lede(a["url"], cut)
        a["lede"] = lede
        hit += int(ok)
        if (i + 1) % 20 == 0:
            print(f"  wayback {i+1}/{len(arts)} ({hit} hits)", file=sys.stderr)

    (OUT_DIR / f"{start}-{end}-articles.json").write_text(json.dumps(
        {"start": start, "end": end, "n_articles": len(arts), "articles": arts}, indent=2))
    print(f"\nWayback hit-rate: {hit}/{len(arts)} ({100*hit/max(len(arts),1):.0f}%)")
    print("\n=== sample article-list items (what the curator would read) ===")
    for a in [x for x in arts if x["lede"]][:8]:
        print(f"\n[{a['date']} | {a['source']}] {a['title'][:80]}")
        print(f"   {a['lede'][:220]}")


def render(path: str) -> str:
    """Format the article list as curator-prompt text (title + lede, no aggregates)."""
    pool = json.loads(Path(path).read_text())
    lines = ["DATE-CLEAN NEWS ARTICLES (title + snippet). Discover the tickers; drop non-investable "
             "noise (war/weather events, private cos, foreign, keyword false-matches):"]
    for a in pool["articles"]:
        snip = (a.get("lede") or a["title"])[:220]
        lines.append(f"\n[{a['date']} | {a['source']}] {a['title'][:90]}\n   {snip} ({a['url']})")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", help="print an articles file as curator-prompt text and exit")
    ap.add_argument("--start", default="2026-05-01")
    ap.add_argument("--end", default="2026-05-31")
    ap.add_argument("--max", type=int, default=120)
    ap.add_argument("--cutoff", default=None, help="Wayback decision-date cutoff (default: --end)")
    a = ap.parse_args()
    if a.render:
        print(render(a.render))
    else:
        build(a.start, a.end, a.max, a.cutoff or a.end)
