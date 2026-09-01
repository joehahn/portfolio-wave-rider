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
import sys
from collections import Counter as _Counter
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
                "image_url", "tickers_mentioned", "first_pulled_at", "first_query", "first_wave",
                "is_article",     # False = quote/landing page: stored, but never fed to a curator
                # page_age is what web_search reports about the result ('3 days ago'); date_source names
                # which rung of retriever._extract_article's chain supplied published_date. Both were
                # being computed and then dropped here, which is why 84 of 833 articles had no date at
                # all: the field existed on the record and never survived the copy into articles.jsonl.
                "page_age", "date_source")
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
    # Multi-part byline ("James Halley; The Motley Fool", "Name; Zacks Equity Research"): keep only the
    # real-person segments, dropping any wire/brand/staff segment. Skip the garbage "author;" form (caught
    # below), and only rewrite when a segment was actually dropped (so co-author lists pass through intact).
    if ";" in a and "author" not in a.lower():
        parts = [p.strip() for p in a.split(";") if p.strip()]
        real = [p for p in parts
                if p.lower() not in _PSEUDO_AUTHORS and not any(s in p.lower() for s in _PSEUDO_SUBSTR)]
        if real and len(real) < len(parts):
            a = "; ".join(real)
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


def _stored_bodies() -> dict[str, dict]:
    """Every stored body, keyed by article_id (so a re-sighted article's body is stored once, and an
    incomplete one can be upgraded in place -- see ingest_pull)."""
    if not ARTICLES.exists():
        return {}
    out: dict[str, dict] = {}
    for line in ARTICLES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
                out[r["article_id"]] = r
            except Exception:  # noqa: BLE001
                continue
    return out


def _rewrite_articles(bodies: dict[str, dict], order: list[str]) -> None:
    """Rewrite articles.jsonl atomically, preserving first-seen order. Used only when an existing
    record is upgraded; new records still append, so the file stays append-mostly."""
    tmp = ARTICLES.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(bodies[i], ensure_ascii=False) for i in order) + "\n",
                   encoding="utf-8")
    tmp.replace(ARTICLES)


def _seen_ids() -> set[str]:
    """article_ids already in the corpus."""
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


# Quote pages, ticker hubs and "latest news" landing pages are not articles: they carry a live price
# widget and a boilerplate caption, no reporting, and their content changes every time anyone loads them.
# They enter the corpus because a web_search hit looks like any other URL. Matched on URL shape and on the
# stock title templates the big finance sites use.
_NON_ARTICLE_URL = re.compile(
    r"/(quote|quotes|symbol|symbols|topic|topics|tag|tags|markets/stocks)/[^/]*/?$"
    r"|/stocks/[a-z]{1,5}/?$|/(watchlist|screener|portfolio)s?/?$", re.I)
_NON_ARTICLE_TITLE = re.compile(
    r"stock price, news, quote|quote & history|latest news and breaking headlines"
    r"|^[^|]{0,40}\(([A-Z]{1,5})\) stock (price|quote)", re.I)


# Fetch failures whose ERROR PAGE text got stored as the lede. These read as content to the curator
# ("503 Service Unavailable...") while carrying no information at all, so treat them as no lede.
_ERROR_LEDE = re.compile(
    r"^\s*(?:4\d\d|5\d\d)\s|service unavailable|access denied|forbidden|not found"
    r"|are you a robot|enable javascript|please enable cookies|subscribe to (?:continue|read)"
    r"|attention required|checking your browser", re.I)


def clean_lede(text: str) -> str:
    """The lede, or "" when what was captured is an error/interstitial page rather than reporting."""
    t = (text or "").strip()
    return "" if (not t or _ERROR_LEDE.search(t[:160])) else t


def is_article(a: dict) -> bool:
    """False for quote/landing pages, which are noise in a curator's news pool."""
    return not (_NON_ARTICLE_URL.search(str(a.get("url") or ""))
                or _NON_ARTICLE_TITLE.search(str(a.get("title") or "")))


def append_pull(pull_id: str, pulled_at: str, retriever: str, retrieval_model: str,
                sightings: list[dict[str, Any]], query_stats: dict[str, Any],
                dry_run: bool = False) -> dict[str, Any]:
    """Persist one pull. `sightings` = one dict per (result) sighting carrying both body and
    sighting fields (built by the retriever). Dedups bodies by article_id, logs every appearance,
    and writes a manifest row. Returns a summary dict.

    With ``dry_run=True`` the dedup/counting runs exactly as normal (so the returned summary is
    accurate) but nothing is written to the corpus files -- for a no-op preview of a pull."""
    stored = _stored_bodies()
    order = list(stored)                       # first-seen order, preserved across any rewrite
    new_bodies, appearances = [], []
    n_new, n_nonart, n_upgraded = 0, 0, 0
    nonart_by_wave: dict[str, int] = {}
    upgraded_fields: dict[str, int] = {}
    for s in sightings:
        aid = s["article_id"]
        appearances.append({k: s.get(k) for k in _APPEARANCE_FIELDS})
        if aid not in stored:
            # Tag rather than drop: the archive stays complete and reversible (a filter bug can be fixed
            # and every past pool re-derives), while read_slice keeps quote pages away from the curator.
            # The per-wave tally is the signal that a wave's query itself needs rewording.
            _ok = is_article(s)
            body = {k: s.get(k) for k in _BODY_FIELDS}
            body["is_article"] = _ok
            new_bodies.append(body)
            stored[aid] = body
            order.append(aid)
            n_new += 1
            if not _ok:
                n_nonart += 1
                _w = str(s.get("first_wave") or s.get("wave") or "?")
                nonart_by_wave[_w] = nonart_by_wave.get(_w, 0) + 1
        else:
            # UPGRADE AN INCOMPLETE RECORD. The retriever re-fetches every unique URL on every pull,
            # including ones already stored, so on a re-sighting we already hold a fresh extraction --
            # and used to throw it away, because the first write won permanently. When that first
            # write lost a race with a 503 or a paywall it stored a body-less, dateless record, and no
            # later sighting could ever repair it: 70 of 89 body-less articles had been seen again,
            # 69 of them still undated, all of them withheld from the curator by read_slice's
            # date-or-text rule. Only ever fills a field that is EMPTY -- a stored value is never
            # overwritten, so this cannot rewrite history, only complete it. This is what
            # scripts/heal_corpus_text.py does after the fact; doing it here is the same repair at
            # the source, on data already in hand, with no extra fetch.
            cur = stored[aid]
            for _f in ("full_text", "snippet", "published_date", "author", "page_age", "date_source"):
                if not cur.get(_f) and s.get(_f):
                    cur[_f] = s[_f]
                    upgraded_fields[_f] = upgraded_fields.get(_f, 0) + 1
                    if _f == "full_text":
                        cur["extraction_ok"] = True
                    n_upgraded += 1
    if not dry_run:
        if upgraded_fields:                    # an in-place edit forces a rewrite; new rows still append
            _rewrite_articles(stored, order)
        else:
            _append_jsonl(ARTICLES, new_bodies)
        _append_jsonl(APPEARANCES, appearances)
        # FUNNEL COUNTS, not just totals. "5 new articles" has several unrelated causes -- a quiet news
        # day, a throttled engine, every result already stored -- and they are indistinguishable from
        # outside. Recording where articles landed, and which ones still carry no date, is what makes a
        # silent regression visible in a day instead of a month.
        # Judged on the STORED record's final state, after any upgrade above -- a re-sighting that comes
        # back dateless is not interesting when we already hold a date for that article.
        undated = [{"url": s.get("url"), "source": s.get("source_domain"),
                    "title": (s.get("title") or "")[:160], "had_text": bool(s.get("full_text")),
                    "had_page_age": bool(s.get("page_age"))}
                   for s in sightings if not stored.get(s["article_id"], s).get("published_date")]
        manifest = {"pull_id": pull_id, "pulled_at": pulled_at, "retriever": retriever,
                    "retrieval_model": retrieval_model, "n_sightings": len(sightings),
                    "n_new_articles": n_new, "n_new_non_article": n_nonart,
                    "non_article_by_wave": nonart_by_wave,
                    "n_upgraded_fields": n_upgraded, "upgraded_by_field": upgraded_fields,
                    "n_undated": len({u["url"] for u in undated}),
                    "date_sources": dict(_Counter(str(s.get("date_source") or "none")
                                                  for s in sightings)),
                    "query_stats": query_stats}
        _append_jsonl(PULLS, [manifest])
        if undated:    # write the drops down, don't merely count them: a page we never fetched (no text,
            # no date) is a retrieval failure worth chasing; one we read that carries no machine-readable
            # date is not, and a bare tally cannot tell them apart.
            _seen_u, _rows = set(), []
            for u in undated:
                if u["url"] not in _seen_u:
                    _seen_u.add(u["url"])
                    _rows.append({"pull_id": pull_id, "pulled_at": pulled_at, **u})
            _append_jsonl(CORPUS_DIR / "undateable.jsonl", _rows)
    # Underscore keys in query_stats are per-pull diagnostics (_lede_sources, _uncrawlable_specialty_domains),
    # not queries, so they must not inflate the query count.
    n_queries = sum(1 for k in query_stats if not str(k).startswith("_"))
    return {"pull_id": pull_id, "sightings": len(sightings), "new_articles": n_new,
            "new_non_article": n_nonart, "non_article_by_wave": nonart_by_wave,
            "upgraded_fields": n_upgraded, "upgraded_by_field": upgraded_fields,
            "undated": len({s.get("url") for s in sightings
                            if not stored.get(s["article_id"], s).get("published_date")}),
            "queries": n_queries, "corpus_articles": len(stored), "dry_run": dry_run}


def read_slice(as_of: str, lookback_days: int) -> list[dict[str, Any]]:
    """Article bodies in the trailing news window (as_of - lookback_days, as_of], for the forward curator
    (Stage 2). The window is keyed on ``published_date``; an article with NO usable publish date falls back
    to its ``first_pulled_at`` (pull date) so recent-but-undated news is not lost, and is flagged with
    ``date_is_pull_fallback: True`` (a per-slice copy -- the stored record is never mutated). The flag lets
    downstream count how often the fallback fired (``sum(a.get("date_is_pull_fallback") for a in slice)``);
    read_slice also logs that count. Articles with neither a publish nor a pull date are skipped.

    An article is ALSO excluded when it was first pulled after ``as_of``: publishing before the decision
    date is not enough, the retriever had to have actually harvested it by then. This is a no-op on a live
    run (today's pulls are always <= today) and bites only on a REPLAY of a past rebalance, where the
    corpus has since grown -- without it, replaying 2026-07-22 handed the curator 180 articles when the
    corpus held 18 that day. That is retrieval look-ahead, and it is exactly what the forward test exists
    to rule out. Records with no ``first_pulled_at`` (e.g. blended backtest-pool articles) are unaffected."""
    from datetime import date, timedelta
    if not ARTICLES.exists():
        return []
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=lookback_days)
    out, n_fallback, n_nonart, n_undated, n_unpulled = [], 0, 0, 0, 0
    for line in ARTICLES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            a = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        try:
            d, fallback = date.fromisoformat((a.get("published_date") or "")[:10]), False
        except Exception:  # noqa: BLE001 - no usable publish date: fall back to the pull date, flagged
            try:
                d, fallback = date.fromisoformat((a.get("first_pulled_at") or "")[:10]), True
            except Exception:  # noqa: BLE001 - no usable date at all
                continue
        if start < d <= end:
            # Harvested by as_of? (see docstring: blocks retrieval look-ahead when replaying a past date)
            _pulled = (a.get("first_pulled_at") or "")[:10]
            if _pulled and _pulled > as_of:
                n_unpulled += 1
                continue
            if not is_article(a):        # quote page / ticker hub, not reporting
                n_nonart += 1
                continue
            # An article admitted on the PULL-date fallback has no publication date, so its true age is
            # unknown -- measured on the live corpus, every such record also failed extraction and carries
            # no body text, and the set is dominated by market-report mills, hubs and (worst) SEC filings
            # from 2005-2007 that would enter as "fresh". Require a date OR readable text, never neither.
            if fallback and not clean_lede(a.get("snippet") or a.get("full_text") or a.get("lede") or ""):
                n_undated += 1
                continue
            if fallback:
                a = {**a, "date_is_pull_fallback": True}
                n_fallback += 1
            out.append(a)
    if n_fallback or n_nonart or n_undated or n_unpulled:
        print(f"read_slice({as_of}, {lookback_days}d): {len(out)} articles"
              + (f", {n_fallback} used a pull-date fallback (no publish date)" if n_fallback else "")
              + (f", {n_nonart} quote/landing pages dropped" if n_nonart else "")
              + (f", {n_undated} undated-and-textless dropped" if n_undated else "")
              + (f", {n_unpulled} not yet harvested on this date" if n_unpulled else ""), file=sys.stderr)
    return out
