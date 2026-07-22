"""Frozen forward news corpus: append-only, deduped, one-shot-safe.

Forward WebSearch pulls are UNREPEATABLE (results aren't re-queryable; articles get edited,
paywalled, or deleted), so we capture broad and raw at pull time and never mutate. Three files
under ``data/forward_corpus/``:

- ``articles.jsonl``  one record per UNIQUE article (deduped by article_id). Holds the immutable
                      body: title, url, full_text, author, date, source, etc. Written once.
- ``appearances.jsonl`` one row per SIGHTING (article x pull x query). This is what preserves
                      "store broad, rank at selection": every time an article surfaces we log the
                      query, wave, rank, and pull, plus its content_hash so a later edit is visible.
- ``pulls.jsonl``     one row per pull run (manifest): timestamp, retriever, per-query counts, and
                      errors/empties, so GAPS are first-class and queryable (a missed/empty pull is
                      recorded, not silent).

article_id = sha1 of the canonicalized URL (drop scheme/www/tracking-params/fragment), so the same
URL across days and across wave queries dedups to one body. The published date lives as a field, not
in the id (a wobbling reported date must not fragment dedup); content_hash detects edits instead.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "data" / "forward_corpus"
ARTICLES = CORPUS_DIR / "articles.jsonl"
APPEARANCES = CORPUS_DIR / "appearances.jsonl"
PULLS = CORPUS_DIR / "pulls.jsonl"

# tracking / share params to strip when canonicalizing (mirrors scripts/news_pool.py's _TRACK)
_TRACK = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|referrer|cmpid|ncid|mkt_tok|igshid|_hsenc|_hsmi|"
                    r"spm|s_cid|cid$|ei$|oc$|smid|guccounter|guce_)", re.IGNORECASE)

# body fields copied to articles.jsonl (the immutable, deduped record). Everything else on a pull
# record is sighting-specific and goes to appearances.jsonl.
_BODY_FIELDS = ("article_id", "url", "canonical_url", "source_domain", "publisher", "title", "author",
                "published_date", "language", "snippet", "full_text", "extraction_ok", "content_hash",
                "image_url", "tickers_mentioned", "first_pulled_at", "first_query", "first_wave")
_APPEARANCE_FIELDS = ("article_id", "pull_id", "pulled_at", "query", "wave", "result_rank", "content_hash")


def canon_url(url: str) -> str:
    """Canonical form for dedup: lowercase host, drop www., strip tracking params, drop fragment."""
    try:
        s = urlsplit(url.strip())
    except Exception:  # noqa: BLE001
        return url.strip()
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = sorted((k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if not _TRACK.match(k))
    return urlunsplit(("https", host, (s.path or "/").rstrip("/") or "/", urlencode(q), ""))


def article_id(url: str) -> str:
    return hashlib.sha1(canon_url(url).encode("utf-8")).hexdigest()[:16]


# Non-person bylines trafilatura pulls from author/meta fields: PR wires, site brands, newsroom/staff
# labels. Blanked so the author field (and the gains-per-author view) tracks real writers, not publishers.
_PSEUDO_AUTHORS = {
    "business wire", "pr newswire", "prnewswire", "globe newswire", "globenewswire", "accesswire",
    "access newswire", "newsfile corp", "cision", "stock titan", "marketbeat", "market beat",
    "zacks equity research", "zacks", "motley fool transcribing", "the motley fool",
}
_PSEUDO_SUBSTR = ("staff", "newsroom", "editorial", "redakt", "redaction", "transcribing",
                  "research team", "press release", "newswire", "correspondent")


def _load_tiers():
    """(major_domains, specialty_domains) from news_sources.md — mirrors gkg_pool's parsing so forward
    and backtest agree on authority tiers. specialty = every https URL in the prose, MINUS major (major
    wins the overlap). Empty on missing file."""
    import yaml
    p = ROOT / "news_sources.md"
    if not p.exists():
        return set(), set()
    txt = p.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", txt, re.DOTALL)
    major = {str(s).lower() for s in ((yaml.safe_load(m.group(1)) or {}).get("source_major") or [])} if m else set()
    prose = re.sub(r"^---\s*\n.*?\n---\s*\n", "", txt, count=1, flags=re.DOTALL)
    spec = set()
    for mm in re.finditer(r"https?://([A-Za-z0-9.-]+)", prose):
        d = mm.group(1).lower()
        spec.add(d[4:] if d.startswith("www.") else d)
    return major, spec - major


_TIERS = None


def _domain_in(domain: str, domains: set) -> bool:
    d = (domain or "").lower()
    return any(d == x or d.endswith("." + x) for x in domains)


def source_tier(domain: str) -> str:
    """Authority tier of a source domain per news_sources.md: 'specialty' (top), 'major' (wire), or
    'other'. Boundary-aware (finance.yahoo.com matches yahoo.com; proactiveinvestors.com does not
    match investors.com)."""
    global _TIERS
    if _TIERS is None:
        _TIERS = _load_tiers()
    major, spec = _TIERS
    if _domain_in(domain, spec):
        return "specialty"
    if _domain_in(domain, major):
        return "major"
    return "other"


def specialty_domains() -> list[str]:
    """Flat list of specialty-desk domains (for a web_search allowed_domains specialty sweep)."""
    global _TIERS
    if _TIERS is None:
        _TIERS = _load_tiers()
    return sorted(_TIERS[1])


def clean_author(author: "str | None", publisher: "str | None" = None) -> "str | None":
    """Return the byline if it looks like a real person, else None. Drops PR-wire / site-brand /
    newsroom-staff pseudo-authors and site-name-as-byline (author == publisher)."""
    if not author:
        return None
    a = " ".join(str(author).split())
    al = a.lower()
    if publisher and al == str(publisher).strip().lower():
        return None
    if al in _PSEUDO_AUTHORS:
        return None
    if any(s in al for s in _PSEUDO_SUBSTR):
        return None
    if al.startswith("news;") or ("author" in al and ";" in a):   # garbage extractions
        return None
    return a


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _seen_ids() -> set[str]:
    """article_ids already in the corpus (so a re-sighted article's body is stored once)."""
    if not ARTICLES.exists():
        return set()
    ids = set()
    for line in ARTICLES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                ids.add(json.loads(line)["article_id"])
            except Exception:  # noqa: BLE001
                continue
    return ids


def append_pull(pull_id: str, pulled_at: str, retriever: str, retrieval_model: str,
                sightings: list[dict[str, Any]], query_stats: dict[str, Any]) -> dict[str, Any]:
    """Persist one pull. `sightings` = one dict per (result) sighting carrying both body and
    sighting fields (built by the retriever). Dedups bodies by article_id, logs every appearance,
    and writes a manifest row. Returns a summary dict."""
    seen = _seen_ids()
    new_bodies, appearances = [], []
    n_new = 0
    for s in sightings:
        aid = s["article_id"]
        appearances.append({k: s.get(k) for k in _APPEARANCE_FIELDS})
        if aid not in seen:
            new_bodies.append({k: s.get(k) for k in _BODY_FIELDS})
            seen.add(aid)
            n_new += 1
    _append_jsonl(ARTICLES, new_bodies)
    _append_jsonl(APPEARANCES, appearances)
    manifest = {"pull_id": pull_id, "pulled_at": pulled_at, "retriever": retriever,
                "retrieval_model": retrieval_model, "n_sightings": len(sightings),
                "n_new_articles": n_new, "query_stats": query_stats}
    _append_jsonl(PULLS, [manifest])
    return {"pull_id": pull_id, "sightings": len(sightings), "new_articles": n_new,
            "queries": len(query_stats), "corpus_articles": len(seen)}


def read_slice(as_of: str, lookback_days: int) -> list[dict[str, Any]]:
    """Article bodies whose published_date falls in (as_of - lookback_days, as_of]. Used by the
    forward curator (Stage 2) to read the trailing news window. Undated articles are skipped."""
    from datetime import date, timedelta
    if not ARTICLES.exists():
        return []
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=lookback_days)
    out = []
    for line in ARTICLES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            a = json.loads(line)
            d = date.fromisoformat((a.get("published_date") or "")[:10])
        except Exception:  # noqa: BLE001
            continue
        if start < d <= end:
            out.append(a)
    return out
