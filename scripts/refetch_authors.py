#!/usr/bin/env python3
"""Post-run author refetch: write <run_dir>/_authors.json = {evidence_url: byline} by fetching the byline
for each URL a curator cited as add-evidence (Wayback as-of the citation date, else live page). The CBT
author plots (12-13) and the RBT author plot read this file. The backtest itself is UNTOUCHED (no
re-curation) — this only fills in attribution the backtest pool build dropped.

Default: evidence URLs only (a handful per run — fast). --all also does every pooled article URL (slow:
one Wayback/live fetch per URL, ~hours for a full 3-year run) for the RBT "articles per author" plot.

Usage: python scripts/refetch_authors.py data/curator_runs/gkg-3yr-canon14 [--all]
"""
import glob
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import news_pool as w  # noqa: E402


def _byline(url: str, target: date) -> str:
    """Wayback byline as-of target (fetch+cache), else live byline. '' if neither page carried one."""
    w.wayback_lede(url, target)                 # fetches the archived page, caching {lede, author}
    au = w.wayback_author(url, target)
    if not au:
        w.live_lede(url)                        # live fallback: fetch today's page, cache {lede, author}
        au = w.live_author(url)
    return au or ""


def refetch(run_dir: str, do_all: bool = False) -> dict:
    rd = Path(run_dir)
    w.CACHE = rd / "_cache"                      # point news_pool's cache at THIS run
    w.CACHE.mkdir(parents=True, exist_ok=True)

    # (url -> citation date) for every URL cited as add-evidence in the curations
    pairs: dict[str, str] = {}
    for f in sorted(glob.glob(str(rd / "2*-curation.json"))):
        cj = json.loads(Path(f).read_text())
        d = cj.get("as_of_date") or Path(f).name[:10]
        for a in cj.get("adds", []):
            for e in (a.get("news_evidence") or []):
                s = e if isinstance(e, str) else (e.get("url", "") if isinstance(e, dict) else "")
                for u in re.findall(r"https?://[^\s)\]]+", s if isinstance(s, str) else ""):
                    pairs.setdefault(u, d)
    if do_all:                                   # also every pooled article (for the RBT author plot)
        for pf in sorted(glob.glob(str(rd / "*-pool.json"))):
            d = Path(pf).name.split("pool")[0].rstrip("-")[-10:] if "pool" in Path(pf).name else ""
            arts = json.loads(Path(pf).read_text())
            for a in (arts if isinstance(arts, list) else []):
                if isinstance(a, dict) and a.get("url"):
                    pairs.setdefault(a["url"], a.get("date", d)[:10])

    authors = {}
    for i, (u, d) in enumerate(pairs.items(), 1):
        try:
            au = _byline(u, date.fromisoformat(d[:10]))
        except Exception:
            au = ""
        if au:
            authors[u] = au
        if i % 200 == 0:
            print(f"  {i}/{len(pairs)} fetched, {len(authors)} bylines", flush=True)
    (rd / "_authors.json").write_text(json.dumps(authors, indent=1))
    print(f"  {run_dir}: {len(pairs)} URLs -> {len(authors)} bylines "
          f"({len(authors) / max(len(pairs), 1) * 100:.0f}%) -> _authors.json")
    return authors


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    refetch(args[0] if args else "data/curator_runs/gkg-3yr-canon14", do_all="--all" in sys.argv)
