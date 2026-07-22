"""Pluggable news retrievers for the curator.

The curator itself is retriever-agnostic: it reads a pool of articles. Two implementations feed it:

- ``WebSearchRetriever`` (FORWARD): drives Anthropic's ``web_search`` server tool with FIXED per-wave
  queries (a cheap model like Haiku, low agency, so the pull is reproducible), collects the RAW result
  list (store broad, not just what the model cited), then fetches + trafilatura-extracts each article's
  full text at pull time. Used forward at as_of=today, where WebSearch carries no look-ahead bias.
- ``GkgWaybackRetriever`` (HISTORICAL, Stage 2): wraps the date-honest GKG + Wayback path for backtesting
  the past, where live WebSearch would leak the future. Stub here.

Retrieval is DECOUPLED from curation: a kimi curator (OpenRouter) cannot call Anthropic's web_search, so
the pull is its own step that produces article records; the curator later reads them from the corpus.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import trafilatura

from . import corpus

ROOT = Path(__file__).resolve().parent.parent

# Fixed per-wave query template. Low agency on purpose: the executor model just runs this string through
# web_search, so the corpus is reproducible and the model's "cleverness" never steers what gets collected.
def _wave_query(keywords: list[str]) -> str:
    return "recent business and stock-market news about " + ", ".join(keywords[:6])

# light ticker detector (a hint for coverage analysis; the curator does the real extraction)
_TICKER_RE = re.compile(r"\((?:NYSE|NASDAQ|NYSE ?American|NYSEARCA|OTC|Nasdaq|CBOE)[:\s]+([A-Z]{1,5})\)|\$([A-Z]{1,5})\b")
_NOT_TICKER = {"CEO", "CFO", "AI", "US", "USA", "GDP", "IPO", "SEC", "FDA", "ETF", "EV", "UK", "EU"}


def _tickers(text: str) -> list[str]:
    out = []
    for a, b in _TICKER_RE.findall(text or ""):
        t = a or b
        if t and t not in _NOT_TICKER and t not in out:
            out.append(t)
    return out


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return ""


def blocked_domains() -> list[str]:
    """Domains to exclude from the forward pull, from news_sources.md's `source_block` front matter
    (the same low-signal / PR-mill list the backtest GKG path drops). Missing file/section -> []."""
    import re
    import yaml
    p = ROOT / "news_sources.md"
    if not p.exists():
        return []
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", p.read_text(), re.DOTALL)
    if not m:
        return []
    data = yaml.safe_load(m.group(1)) or {}
    return [str(d).strip() for d in (data.get("source_block") or []) if str(d).strip()]


class Retriever(Protocol):
    def pull(self, pull_id: str, pulled_at: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return (sightings, query_stats). Each sighting is a flat dict carrying both the article
        body fields and the sighting fields (query, wave, result_rank, pull_id, pulled_at)."""
        ...


def _extract_article(url: str, cid: str, res: dict, pulled_at: str, query: str, wave: str) -> dict[str, Any]:
    """Fetch a live page and extract its body + metadata (title, AUTHOR byline, date, full text) via
    trafilatura. Shared by every retriever, so author capture lives in ONE place. A dead/paywalled/JS
    page yields a body-less record (title + url only). `res` may carry a fallback title/page_age."""
    import hashlib
    title, author, pub_date, language, snippet, full_text, image, publisher = (
        res.get("title", ""), None, None, None, "", "", None, None)
    ok = False
    try:
        html = trafilatura.fetch_url(url)
        if html:
            j = trafilatura.extract(html, output_format="json", with_metadata=True,
                                    favor_precision=True, include_comments=False)
            if j:
                m = json.loads(j)
                full_text = m.get("text") or ""
                title = m.get("title") or title
                author = m.get("author")
                pub_date = m.get("date")
                language = m.get("language")
                snippet = (m.get("description") or full_text[:300]).strip()
                image = m.get("image")
                publisher = m.get("sitename")
                ok = bool(full_text)
    except Exception:  # noqa: BLE001 - a dead/paywalled/JS page just yields a body-less record
        pass
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    blob = full_text or (title + url)
    return {
        "article_id": cid, "url": url, "canonical_url": corpus.canon_url(url),
        "source_domain": host, "publisher": publisher or host, "title": title, "author": author,
        "published_date": (pub_date or res.get("date") or "")[:10] or None, "language": language,
        "snippet": snippet, "full_text": full_text, "extraction_ok": ok,
        "content_hash": hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16],
        "image_url": image, "tickers_mentioned": _tickers(title + " " + full_text),
        "first_pulled_at": pulled_at, "first_query": query, "first_wave": wave,
        "page_age": res.get("page_age"),
    }


class WebSearchRetriever:
    def __init__(self, model: str, waves: dict[str, list[str]], max_results_per_query: int | None = None):
        self.model = model
        self.waves = waves
        self.cap = max_results_per_query
        self.blocked = blocked_domains()   # news_sources.md source_block -> web_search blocked_domains
        import anthropic
        k = _env("ANTHROPIC_API_KEY")
        if not k:
            raise SystemExit("ANTHROPIC_API_KEY empty in .env")
        self._cli = anthropic.Anthropic(api_key=k)

    def _search(self, query: str) -> list[dict[str, Any]]:
        """Run one web_search and return ALL results (url, title, page_age)."""
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
        if self.blocked:
            tool["blocked_domains"] = self.blocked   # drop news_sources.md source_block junk domains
        resp = self._cli.messages.create(
            model=self.model, max_tokens=1024,
            tools=[tool],
            messages=[{"role": "user", "content":
                       f'Use the web_search tool exactly once, with this exact query and no modification: '
                       f'"{query}". After the results return, reply with only the word done.'}],
        )
        results: list[dict[str, Any]] = []
        for block in resp.content:
            if getattr(block, "type", "") != "web_search_tool_result":
                continue
            items = getattr(block, "content", None)
            if not isinstance(items, list):     # web_search_tool_result_error -> no results this query
                raise RuntimeError(getattr(items, "error_code", "web_search_error"))
            for it in items:
                if getattr(it, "type", "") == "web_search_result":
                    results.append({"url": getattr(it, "url", ""), "title": getattr(it, "title", ""),
                                    "page_age": getattr(it, "page_age", None)})
        return results[: self.cap] if self.cap else results

    def pull(self, pull_id: str, pulled_at: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        sightings: list[dict[str, Any]] = []
        query_stats: dict[str, Any] = {}
        bodies: dict[str, dict] = {}   # fetch each unique article once per pull, log every sighting
        for wave, keywords in self.waves.items():
            query = _wave_query(keywords)
            try:
                results = self._search(query)
                query_stats[query] = {"wave": wave, "results": len(results)}
            except Exception as e:  # noqa: BLE001 - one bad query must not sink the pull; record the gap
                query_stats[query] = {"wave": wave, "error": str(e)[:140]}
                continue
            for rank, res in enumerate(results):
                url = res.get("url")
                if not url:
                    continue
                cid = corpus.article_id(url)
                if cid not in bodies:
                    bodies[cid] = _extract_article(url, cid, res, pulled_at, query, wave)
                sightings.append({**bodies[cid], "pull_id": pull_id, "pulled_at": pulled_at,
                                  "query": query, "wave": wave, "result_rank": rank})
        return sightings, query_stats


# GDELT DOC-API beats for the recent-history backfill, one per wave (short queries; GDELT treats a
# space as implicit AND, so keep each to 1-2 content words).
_GDELT_BEATS = [
    ("AI chip", "AI"),
    ("space stocks", "rockets_spacecraft"),
    ("nuclear reactor", "nuclear"),
    ("quantum computing", "quantum"),
    ("robotics automation", "robotics"),
    ("defense contract", "geopolitical"),
    ("senior housing REIT", "aging_demographics"),
]


class GdeltBackfillRetriever:
    """Recent-history backfill: keyless GDELT DOC-API article discovery (date-honest) + LIVE page
    extraction (full text + AUTHOR, via the shared _extract_article). For a <~30-day window the pages
    are still up, so we fetch them live rather than Wayback (which lags on fresh pages). Run once to
    seed the forward corpus with prior news so the curator has a full trailing window immediately.
    (True OLD-history GKG+Wayback retrieval stays in scripts/backtest_sdk; this is the forward seed.)"""

    def __init__(self, days: int = 21, max_per_beat: int = 15):
        self.days = days
        self.cap = max_per_beat
        self.blocked = blocked_domains()   # drop news_sources.md source_block domains, like the WebSearch pull
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "scripts"))
        import news_pool
        self._np = news_pool

    def _is_blocked(self, url: str, domain: str) -> bool:
        host = (domain or urlsplit(url).hostname or "").lower().removeprefix("www.")
        return any(b in host for b in self.blocked)

    def pull(self, pull_id: str, pulled_at: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=self.days)
        sightings: list[dict[str, Any]] = []
        query_stats: dict[str, Any] = {}
        bodies: dict[str, dict] = {}
        for query, wave in _GDELT_BEATS:
            try:
                arts = self._np.gdelt_fetch(query, start, end)[: self.cap]
                query_stats[query] = {"wave": wave, "results": len(arts)}
            except Exception as e:  # noqa: BLE001 - one bad beat must not sink the backfill
                query_stats[query] = {"wave": wave, "error": str(e)[:140]}
                continue
            for rank, a in enumerate(arts):
                url = a.get("url")
                if not url or self._is_blocked(url, a.get("domain", "")):
                    continue
                cid = corpus.article_id(url)
                if cid not in bodies:
                    d = self._np._parse_gdelt_date(a.get("seendate", ""))
                    res = {"title": a.get("title", ""), "date": d.isoformat() if d else None}
                    bodies[cid] = _extract_article(url, cid, res, pulled_at, query, wave)
                sightings.append({**bodies[cid], "pull_id": pull_id, "pulled_at": pulled_at,
                                  "query": query, "wave": wave, "result_rank": rank})
        return sightings, query_stats
