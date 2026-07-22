"""One-time forward-corpus cleanup.

Drops the dirty WebSearch-ONLY content (early pulls captured before source_block / pseudo-author /
source_tier / specialty were wired) while KEEPING the GDELT backfill (hard-won past the rate limits).
Also normalizes the kept records: drop now-blocked domains, add source_tier, and clean_author. Prune
orphaned appearances + WebSearch pull-manifest rows. A fresh `pull-news` then re-fills the WebSearch
side cleanly. Back up data/forward_corpus/ before running if unsure.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import corpus, retriever

CORPUS = Path("data/forward_corpus")


def _load(name):
    p = CORPUS / name
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _is_blocked(domain, blocked):
    d = (domain or "").lower().removeprefix("www.")
    return any(b in d for b in blocked)


def main():
    arts, apps, pulls = _load("articles.jsonl"), _load("appearances.jsonl"), _load("pulls.jsonl")
    blocked = set(retriever.blocked_domains())

    seen = defaultdict(set)
    for a in apps:
        seen[a["article_id"]].add(a["pull_id"])
    keep_ids = {aid for aid, ps in seen.items() if any(p.startswith("backfill-") for p in ps)}

    new_arts, dropped_ws, dropped_blk = [], 0, 0
    for a in arts:
        if a["article_id"] not in keep_ids:
            dropped_ws += 1
            continue
        if _is_blocked(a.get("source_domain"), blocked):
            dropped_blk += 1
            continue
        a["author"] = corpus.clean_author(a.get("author"), a.get("publisher"))
        a["source_tier"] = corpus.source_tier(a.get("source_domain", ""))
        new_arts.append(a)

    kept = {a["article_id"] for a in new_arts}
    new_apps = [ap for ap in apps if ap["article_id"] in kept and ap["pull_id"].startswith("backfill-")]
    new_pulls = [p for p in pulls if p["pull_id"].startswith("backfill-")]

    for name, rows in [("articles.jsonl", new_arts), ("appearances.jsonl", new_apps), ("pulls.jsonl", new_pulls)]:
        (CORPUS / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"kept {len(new_arts)} articles (dropped {dropped_ws} WebSearch-only + {dropped_blk} now-blocked); "
          f"appearances {len(new_apps)}; pulls {len(new_pulls)} (GDELT backfills only)")


if __name__ == "__main__":
    main()
