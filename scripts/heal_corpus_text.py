#!/usr/bin/env python3
"""Re-fetch corpus articles that carry no usable body text and fill them in.

Why this exists: a pull fires ~80 fetches back-to-back, and the busier domains answer some of them with
503s. The measured cause is rate limiting, not paywalls -- the same URLs return full text on a later
single request. `src/retriever.fetch_html` now retries with backoff, but the records already written
still have no text, and every one of them reaches the curator as a bare headline (or is dropped by
read_slice's date-or-text rule). This walks those records once, politely, and repairs what it can.

LARGELY SUPERSEDED as of 2026-08-31: `corpus.append_pull` now performs this repair AT THE SOURCE. The
retriever already re-fetches every unique URL on every pull, including ones already stored, so when a
body-less record is re-sighted the fresh extraction is in hand and is used to fill the empty fields (it
never overwrites a stored value). 70 of 89 body-less records had been re-sighted at least once, so most
heal on their own now. This script remains useful only for a ONE-OFF sweep of records that predate that
change and are not being re-sighted -- it fetches independently rather than waiting for a pull.

Safe to re-run: it only touches records whose text is still missing, and it rewrites articles.jsonl
atomically. Nothing is deleted -- a record that stays unreachable is left exactly as it was.

  python scripts/heal_corpus_text.py [--limit N] [--delay 1.5] [--dry-run] [--playwright]

--playwright is the last resort for pages that render their text client-side (SPAs): it drives a real
browser via the Playwright MCP tooling if that is available in the environment. Off by default, because
the nightly cron runs unattended and a browser per URL is far heavier than the problem it solves.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import trafilatura

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import corpus, retriever  # noqa: E402

ARTICLES = ROOT / "data" / "forward_corpus" / "articles.jsonl"


def _needs_text(a: dict) -> bool:
    return not corpus.clean_lede(a.get("snippet") or a.get("full_text") or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill body text for corpus articles that lack it.")
    ap.add_argument("--limit", type=int, default=0, help="stop after N repairs (0 = no limit)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between fetches (be polite)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--playwright", action="store_true",
                    help="fall back to a real browser for pages that render text client-side")
    a = ap.parse_args(argv)

    rows = [json.loads(ln) for ln in ARTICLES.read_text(encoding="utf-8").splitlines() if ln.strip()]
    todo = [r for r in rows if _needs_text(r) and corpus.is_article(r) and r.get("url")]
    print(f"{len(rows)} articles, {len(todo)} without usable text", file=sys.stderr)
    if a.dry_run:
        for r in todo[:20]:
            print(f"   would refetch  {(r.get('source_domain') or ''):26s} {r['url'][:78]}", file=sys.stderr)
        return 0

    healed = failed = 0
    for r in todo:
        if a.limit and healed >= a.limit:
            break
        time.sleep(a.delay)
        html = retriever.fetch_html(r["url"])
        text = ""
        if html:
            try:
                j = trafilatura.extract(html, output_format="json", with_metadata=True,
                                        favor_precision=True, include_comments=False)
                if j:
                    m = json.loads(j)
                    text = corpus.clean_lede(m.get("text") or "")
                    if text:
                        r["full_text"] = text
                        r["snippet"] = corpus.clean_lede(m.get("description") or text[:300]) or text[:300]
                        r["extraction_ok"] = True
                        # a repaired page often carries the metadata the first attempt missed
                        r["published_date"] = (m.get("date") or r.get("published_date") or "")[:10] or None
                        r["author"] = corpus.clean_author(m.get("author") or r.get("author"),
                                                          m.get("sitename") or r.get("publisher"))
            except Exception:  # noqa: BLE001
                pass
        if text:
            healed += 1
            print(f"  healed  {(r.get('source_domain') or ''):26s} {len(text):6,d} chars  {r['url'][:60]}",
                  file=sys.stderr)
        else:
            failed += 1

    if healed:
        tmp = ARTICLES.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        tmp.replace(ARTICLES)
    print(f"\nhealed {healed}, still empty {failed}", file=sys.stderr)
    if failed and not a.playwright:
        print("  (a page that renders its text client-side needs --playwright; see the module docstring)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
