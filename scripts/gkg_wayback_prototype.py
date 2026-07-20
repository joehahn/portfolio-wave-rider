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
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gkg_pool as g
import news_pool as w
from google.cloud import bigquery

OUT_DIR = g.ROOT / "data" / "curator_runs" / "gkg-wayback-proto"


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def gkg_month(client, start: str, end: str) -> list[dict]:
    """One GKG pull over an arbitrary [start,end] date range (not the 90-day as-of window)."""
    kw = g._keyword_regex()
    sql = f"""
    SELECT {', '.join(g._FIELDS)}
    FROM `{g.TABLE}`
    WHERE _PARTITIONTIME BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
      AND TranslationInfo IS NULL
      AND (REGEXP_CONTAINS(DocumentIdentifier, r'{kw}') OR REGEXP_CONTAINS(Extras, r'{kw}'))
    """
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    gb = dry.total_bytes_processed / 1e9
    if gb > g.MAX_SCAN_GB:
        sys.exit(f"cost guard: {gb:.1f} GB > {g.MAX_SCAN_GB}")
    print(f"scanning {gb:.1f} GB for {start}..{end} ...", file=sys.stderr)
    return [{f: r[f] for f in g._FIELDS} for r in client.query(sql).result()]


def build(start: str, end: str, max_articles: int, cutoff: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache = OUT_DIR / f"_rows-{start}-{end}.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        rows = gkg_month(g._client(), start, end)
        cache.write_text(json.dumps(rows))
    print(f"raw rows: {len(rows)}")

    # same filters as the discovery pool, but keep ARTICLES (not a company aggregate)
    seen_title, arts = set(), []
    for r in rows:
        url = r["DocumentIdentifier"] or ""
        if not url:
            continue
        src = (r["SourceCommonName"] or "").lower()
        if g._domain_in(src, g.SOURCE_BLOCKLIST):
            continue
        title = g._page_title(r["Extras"]) or g._slug_title(url)
        if g.SPAM_TITLE_RE.search(title):
            continue
        if not g._article_waves(f"{title} {url}"):
            continue
        if not g._subject_orgs(r["V2Organizations"]):      # keep only articles ABOUT a company
            continue
        nt = _norm_title(title)
        if nt in seen_title:                                # dedupe syndicated republications
            continue
        seen_title.add(nt)
        arts.append({"title": title, "date": g._gkg_date(r["DATE"]),
                     "source": r["SourceCommonName"] or "", "url": url})
    arts.sort(key=lambda a: a["date"], reverse=True)
    arts = arts[:max_articles]
    print(f"kept articles (deduped, top {max_articles}): {len(arts)}")

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
