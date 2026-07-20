#!/usr/bin/env python3
"""Build a look-ahead-CLEAN historical news pool for the curator backtest.

Why this exists: the WebSearch backtest leaks the future — Anthropic web search ignores
`before:` date bounds at a past as-of date and returns present-day "best stocks to buy"
listicles (see data/news_ab/report.md). This module builds a date-honest alternative:

  GDELT (date-honest discovery)  ->  Wayback (as-of-date lede that names the ticker)

GDELT's 2.0 DOC API enforces date bounds server-side, so a query at a 2022 as-of date
returns only articles published by then. GDELT returns headlines only, and a headline names
the theme, not the ticker, so for each URL GDELT returns we fetch that page's as-of-date
archived lede from the Wayback Machine (which usually names the ticker). Both are free /
keyless. The output is one pool file per rebalance date that the watchlist-curator reads in
GDELT mode INSTEAD of running its own WebSearch.

No seeding: the pool is only what GDELT + Wayback surface on their own.

Usage:
    python scripts/news_pool.py --validate --dates 2022-06-30 2023-12-31 2025-06-30
    python scripts/news_pool.py --build --all           # build all 15 quarter-end pools
    python scripts/news_pool.py --build --dates 2022-06-30

Caching: every GDELT response and Wayback lede is cached under the run dir's _cache/
(gitignored), so re-runs are free and the 15s GDELT throttle is paid only once.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "data" / "curator_runs" / "postcovid-gdelt"
CACHE = RUN_DIR / "_cache"
CAP05_STARTER = ROOT / "data" / "curator_runs" / "postcovid-cap05" / "_starter.json"

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"   # (legacy; _wb_snapshot now uses the avail API)
WAYBACK_AVAIL = "http://archive.org/wayback/available"  # single closest-snapshot lookup (fast path)
# archive.org asks automated clients to identify themselves with contact info.
WAYBACK_UA = "portfolio-wave-rider/1.0 (+contact: jmh.datasciences@gmail.com)"
WAYBACK_MIN_INTERVAL = 0.6     # ~100 req/min. The old 1.5s (CDX's ~60/min ceiling) is moot now that
                               # _wb_snapshot uses the lightweight availability API instead of a CDX
                               # scan; probes never saw a single 429/Retry-After, so the pacing (not
                               # archive.org) was the bottleneck. Kept modest as a courtesy margin.
WAYBACK_RETRY_STATUS = {429, 500, 502, 503, 504}
CDX_FROM_DAYS = 120            # CDX scan lower bound (cutoff-120d); without it, busy URLs 504
# A named User-Agent identifies us as a polite client; the default python-requests UA is what
# rate-limiters clamp first. IMPORTANT: every GDELT call must route through gdelt_fetch() so it
# goes through the single serial pacer — never make out-of-band probes (that trips the
# "high-traffic user" block, which is how this got flagged during development).
USER_AGENT = "portfolio-wave-rider/1.0"

# Gem-agnostic discovery beats, each tagged by KIND:
#   "wave" — a technology/thematic wave from the profile. These are the core: they surface
#            wave-vehicle tickers at ANY stage, and the curator judges buildup (add) vs peak
#            (trim) from the catalyst in each lede.
#   "peak" — cross-cutting superlative/momentum beats. These skew LATE: they surface names
#            that have already run, so they are peak/trim detectors, not early-buildup finders.
# NEVER a ticker symbol — the curator must discover the name from the news, not from priors.
# GDELT is lexical: a space is an implicit AND, so keep beats to 1-2 content words (a 3rd
# AND clause over-filters). Tunable in --validate.
BEATS = [
    ("AI stocks", "wave"),
    ("space stocks", "wave"),
    ("rocket launch", "wave"),
    ("nuclear stocks", "wave"),
    ("quantum computing", "wave"),
    ("robotics automation", "wave"),
    ("defense stocks", "wave"),
    ("tanker shipping", "wave"),
    ("healthcare stocks", "wave"),
    ("aging demographics", "wave"),
    ("stock surging", "peak"),
    ("ETF surging", "peak"),
]
PEAK_BEATS = {q for q, kind in BEATS if kind == "peak"}
BEAT_QUERIES = [q for q, _ in BEATS]

LOOKBACK_DAYS = 90        # quarterly rebalance window
CHUNK_DAYS = 30           # split the 90d window into 3 sub-pulls so datedesc+maxrecords
                          # doesn't crowd out early-quarter news (probe: 54/75 in last 2 days)
MAXRECORDS = 80           # per (beat, chunk); GHR uses 80 for backtest pulls (GDELT cap is 250)
PER_BEAT_CAP = 12         # keep only the N most-recent articles per beat before Wayback
                          # enrichment. Bounds pool size + Wayback load (GHR caps its slice
                          # too); ~12 x 12 beats ≈ 100-140 articles after dedup — plenty of
                          # thematic coverage without fetching hundreds of ledes.
GDELT_THROTTLE = 15.0     # seconds between GDELT calls, serial. GHR-proven: 15s succeeds
                          # first-try, far faster overall than a retry storm (10s still
                          # triggered 2-3 retries/chunk). Enforced by _throttle() before EVERY
                          # request via a process-global last-call clock.
GDELT_RETRIES = 1         # on a rate-limit, back off once then TOLERATE THE MISS — do NOT
                          # hammer (hammering during a block only extends it). The miss isn't
                          # cached, so it retries on the next run (resume from cache).
WAYBACK_TIMEOUT = 12       # a CDX snapshot lookup that doesn't answer in 12s is a poorly-archived
                           # deep URL that won't resolve on a 4th retry either — fail fast, cache a
                           # miss (bulk mode), and let the article fall back to its title.

# ticker-naming detection (a validation proxy; the curator does the real extraction).
# Explicit market forms only, to keep false positives low.
_TICKER_PATTERNS = [
    re.compile(r"\(([A-Z]{1,5})\)"),                              # "(RKLB)"
    re.compile(r"(?:NYSE|NASDAQ|NYSEARCA|AMEX|OTC)[:\s]+([A-Z]{1,5})"),  # "NASDAQ: RKLB"
    re.compile(r"\$([A-Z]{1,5})\b"),                             # "$RKLB"
]
# parenthetical uppercase tokens that are NOT tickers
_NOT_TICKERS = {"US", "USA", "UK", "EU", "CEO", "CFO", "GDP", "IPO", "SEC", "FDA", "ETF",
                "AI", "EV", "R&D", "Q1", "Q2", "Q3", "Q4", "UN", "GDPR", "USD", "PPA", "SMR"}

_last_gdelt_call = [0.0]
_wb_last = [0.0]           # archive.org pacer clock (separate from GDELT's)
_wb_lock = threading.Lock()  # guards _wb_last slot reservation so concurrent lede fetches stagger
_WB_STAT = {"requests": 0, "http_429": 0, "http_5xx": 0, "timeout": 0}  # process-cumulative health
_wb_bulk = [False]           # bulk-build mode: cache an exhausted-transient as a confirmed miss so a
                             # poorly-archived URL is not re-fetched (60-80s) on every pass. Off by
                             # default (live path keeps retry-on-next-run); wayback_ledes turns it on.
WAYBACK_ENRICH_WORKERS = 10  # concurrent wayback_lede fetches. The 1.5s start-slot spacing caps us at
                             # ~40/min regardless of worker count, so 10 workers just ensures enough are
                             # in flight to keep the pipe full while CDX's multi-second latencies overlap.
                             # archive.org is NOT rate-limiting us (probe: clean 200s, no Retry-After) —
                             # it's just slow, so overlapping (not hammering) is the win.
_cache_only = [False]      # when True, gdelt_fetch returns cached data only (no HTTP) —
                           # GHR's --no-pull: rebuild/inspect a pool without hitting GDELT


# ------------------------------------------------------------------ cache helpers
def _cache_get(kind: str, key: str):
    f = CACHE / kind / (re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:180] + ".json")
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


def _cache_put(kind: str, key: str, value) -> None:
    d = CACHE / kind
    d.mkdir(parents=True, exist_ok=True)
    f = d / (re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:180] + ".json")
    f.write_text(json.dumps(value))


# ------------------------------------------------------------------ GDELT
def _stamp(d: date, end: bool = False) -> str:
    return d.strftime("%Y%m%d") + ("235959" if end else "000000")


def _throttle() -> None:
    """Serial process-global pacer: guarantee >= GDELT_THROTTLE between GDELT calls. Every
    call goes through here so we never burst (bursting trips GDELT's high-traffic block)."""
    dt = time.monotonic() - _last_gdelt_call[0]
    if dt < GDELT_THROTTLE:
        time.sleep(GDELT_THROTTLE - dt)
    _last_gdelt_call[0] = time.monotonic()


def gdelt_fetch(query: str, start: date, end: date) -> list[dict]:
    """One GDELT DOC-API pull (cached). Returns raw article dicts. GDELT signals a rate-limit
    by returning a non-JSON (text/html) throttle page, so we treat any non-application/json
    response as a rate-limit: back off once, then tolerate the miss (GHR's proven pattern)."""
    key = f"{query}|{start}|{end}"
    hit = _cache_get("gdelt", key)
    if hit is not None:
        return hit
    if _cache_only[0]:        # --no-pull: don't touch GDELT, treat uncached as an empty miss
        return []
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "ArtList", "format": "json",
        "startdatetime": _stamp(start), "enddatetime": _stamp(end, end=True),
        "maxrecords": MAXRECORDS, "sort": "datedesc",
    }
    for _ in range(GDELT_RETRIES + 1):
        _throttle()
        try:
            r = requests.get(GDELT_URL, params=params, timeout=30,
                             headers={"User-Agent": USER_AGENT})
            if r.headers.get("content-type", "").startswith("application/json"):
                arts = r.json().get("articles", []) or []
                _cache_put("gdelt", key, arts)   # cache only a real (JSON) response
                return arts
        except requests.exceptions.RequestException:
            pass
        time.sleep(GDELT_THROTTLE)   # rate-limit text / transient error -> back off, retry once
    # Miss tolerated: do NOT cache, so this (beat, chunk) is retried on the next run.
    print(f"  [gdelt] miss (rate-limited?): {query!r} {start}..{end}", file=sys.stderr)
    return []


def _parse_gdelt_date(seendate: str):
    """GDELT seendate is 'YYYYMMDDTHHMMSSZ'."""
    try:
        return datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").date()
    except Exception:
        return None


# ------------------------------------------------------------------ Wayback
def _extract_article_text(soup: BeautifulSoup) -> str:
    """Pull the article's own text, preferring embedded JSON-LD structured data (most news
    sites carry `articleBody`/`headline`/`description` there — clean and boilerplate-free),
    then og:description/meta, then body paragraphs as a last resort. Capped at ~800 chars,
    which is plenty for the curator to name the ticker without hauling the whole article."""
    # 1) JSON-LD
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
            for g in graph:
                if not isinstance(g, dict):
                    continue
                body = g.get("articleBody") or g.get("description")
                head = g.get("headline") or ""
                if body:
                    return " ".join(f"{head}. {body}".split())[:800]
    # 2) meta description / og:description
    meta = (soup.find("meta", attrs={"name": "description"})
            or soup.find("meta", attrs={"property": "og:description"}))
    if meta and meta.get("content"):
        return " ".join(meta["content"].split())[:800]
    # 3) first substantial body paragraph
    for p in soup.find_all("p"):
        t = p.get_text(" ", strip=True)
        if len(t) > 80:
            return " ".join(t.split())[:800]
    return ""


class WaybackTransient(Exception):
    """A retryable archive.org failure (throttle/5xx/timeout). Callers must NOT cache it as a
    permanent miss, or a rate-limit blip poisons the cache with false 'not-archived'."""


def _wb_throttle() -> None:
    """Thread-safe rate limiter: RESERVES the next WAYBACK_MIN_INTERVAL-spaced start slot under the
    lock, then sleeps OUTSIDE it. Concurrent enrich workers thus get evenly staggered starts (still
    <= ~40/min, the safe serial rate) while their multi-second archive.org latencies overlap instead
    of serializing on one shared sleep -- the whole point of the thread pool."""
    with _wb_lock:
        now = time.monotonic()
        slot = max(now, _wb_last[0] + WAYBACK_MIN_INTERVAL)
        _wb_last[0] = slot
    wait = slot - now
    if wait > 0:
        time.sleep(wait)


def _wb_get(url: str, timeout: int = WAYBACK_TIMEOUT, tries: int = 2):
    """GET archive.org with pacing + backoff on 429/5xx (honoring Retry-After). Returns the
    response for any non-retryable status (incl. 404); raises WaybackTransient if retries
    are exhausted or the connection keeps failing. tries=2 (one retry): the diagnostic showed
    CDX timeouts don't resolve on deeper retries, they just burn 60-80s/URL — so fail fast."""
    delay = 4.0
    for attempt in range(tries):
        _wb_throttle()
        _WB_STAT["requests"] += 1
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": WAYBACK_UA})
        except requests.exceptions.RequestException:
            _WB_STAT["timeout"] += 1
            if attempt == tries - 1:
                raise WaybackTransient()
            time.sleep(delay); delay = min(delay * 2, 120); continue
        if r.status_code in WAYBACK_RETRY_STATUS:
            _WB_STAT["http_429" if r.status_code == 429 else "http_5xx"] += 1
            ra = r.headers.get("Retry-After", "")
            time.sleep(min(float(ra), 120) if ra.isdigit() else delay)
            delay = min(delay * 2, 120); continue
        return r
    raise WaybackTransient()


def _wb_snapshot(url: str, cutoff: date) -> str | None:
    """Availability API: the timestamp of the snapshot CLOSEST to cutoff, returned iff that snapshot
    is AT-OR-BEFORE cutoff (look-ahead-clean). None if the URL was never archived, or the closest
    snapshot is AFTER cutoff (a clean miss — we never use future content; this makes hit-rate a safe
    LOWER bound). Raises WaybackTransient on a throttle/parse blip.

    Why not CDX search: a diagnostic showed CDX's from/to scan runs 20-83s/URL (deep news URLs make
    it 504/timeout, then blind-retry) at a 15% hit-rate, while this single-lookup availability API
    runs ~5s/URL at a 63% hit-rate — 12x faster AND 4x more hits on the identical URL set."""
    ts = cutoff.strftime("%Y%m%d") + "235959"
    r = _wb_get(f"{WAYBACK_AVAIL}?url={requests.utils.quote(url, safe='')}&timestamp={ts}")
    if r.status_code >= 400:
        return None
    try:
        snap = r.json().get("archived_snapshots", {}).get("closest", {})
    except Exception:
        raise WaybackTransient()          # 200 but non-JSON = a throttle page; retry later
    snap_ts = snap.get("timestamp", "") if snap.get("available") else ""
    if not snap_ts or snap_ts[:8] > cutoff.strftime("%Y%m%d"):
        return None                       # not archived, or closest capture is AFTER cutoff -> miss
    return snap_ts


def wayback_lede(url: str, target: date) -> tuple[str, bool]:
    """Fetch the as-of-date archived page's lede (ticker-naming lead text). Returns (lede,
    hit). Caches a CONFIRMED result (a real lede, or a confirmed not-archived miss); on a
    transient throttle it returns ('', False) WITHOUT caching, so a later run retries."""
    key = f"{url}|{target}"
    hit = _cache_get("wayback", key)
    if hit is not None:
        return hit.get("lede", ""), hit.get("hit", False)
    # Note: --no-pull gates only GDELT (the rate-limited API); Wayback always enriches, since
    # it self-paces (WAYBACK_MIN_INTERVAL) and never caches a transient throttle.
    try:
        ts = _wb_snapshot(url, target)
        if not ts:
            _cache_put("wayback", key, {"lede": "", "hit": False})   # confirmed not archived
            return "", False
        # `id_` raw-content modifier: original page HTML, not archive.org's wrapper/toolbar.
        r = _wb_get(f"http://web.archive.org/web/{ts}id_/{url}")
        if r.status_code >= 400:
            _cache_put("wayback", key, {"lede": "", "hit": False})
            return "", False
        lede = _extract_article_text(BeautifulSoup(r.text, "html.parser"))
        if any(s in lede.lower() for s in
               ("capture a web page as it appears now", "wayback machine",
                "internet archive", "sign fight for the future")):
            lede = ""                     # never let an archive.org interstitial leak in
        _cache_put("wayback", key, {"lede": lede, "hit": bool(lede)})
        return lede, bool(lede)
    except WaybackTransient:
        if _wb_bulk[0]:                   # bulk build: accept the miss and cache it (no lede for this
            _cache_put("wayback", key, {"lede": "", "hit": False})   # URL) so we never re-burn 60-80s
            return "", False
        return "", False                  # live path: DO NOT cache — retry on the next run


def wayback_ledes(urls: list[str], target: date,
                  workers: int = WAYBACK_ENRICH_WORKERS) -> dict[str, str]:
    """Concurrent batch of wayback_lede: fetch every URL's as-of-`target` lede through a small
    thread pool and return {url: lede}. CDX snapshot lookups are individually slow (7-22s) but
    archive.org isn't rate-limiting, so overlapping them is a ~Nx speedup over the serial loop.
    _wb_throttle (slot-reserved) keeps request STARTS <= ~40/min; per-URL caching is unchanged, so
    a re-run is all cache hits. Dedupes URLs. Misses / transient failures map to ''."""
    uniq = list(dict.fromkeys(u for u in urls if u))
    out: dict[str, str] = {}
    _wb_bulk[0] = True            # cache exhausted-transients as misses (don't re-burn 60-80s/URL)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(wayback_lede, u, target): u for u in uniq}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    out[u] = fut.result()[0]
                except Exception:
                    out[u] = ""
    finally:
        _wb_bulk[0] = False
    return out


# ------------------------------------------------------------------ ticker hints
def find_ticker_hints(text: str) -> list[str]:
    hints = set()
    for pat in _TICKER_PATTERNS:
        for m in pat.findall(text or ""):
            if m and m not in _NOT_TICKERS:
                hints.add(m)
    return sorted(hints)


# ------------------------------------------------------------------ pool build
def _chunks(as_of: date):
    """Yield (start, end) sub-windows covering the trailing LOOKBACK_DAYS, newest first."""
    end = as_of
    lo = as_of - timedelta(days=LOOKBACK_DAYS)
    while end > lo:
        start = max(lo, end - timedelta(days=CHUNK_DAYS))
        yield start, end
        end = start


def build_pool(as_of_str: str, write: bool = True) -> dict:
    """Build the GDELT+Wayback pool for one rebalance date. Returns stats; writes the pool
    JSON to <RUN_DIR>/<as_of>-pool.json when write=True."""
    as_of = date.fromisoformat(as_of_str)
    lo = as_of - timedelta(days=LOOKBACK_DAYS)
    raw_rows = 0
    # Collect per beat, date-gated, then keep only the PER_BEAT_CAP most-recent per beat
    # BEFORE Wayback enrichment (enriching every URL from every chunk would be hundreds of
    # fetches for no decision gain).
    by_beat: dict[str, list] = {}
    for beat, _kind in BEATS:
        arts = []
        for start, end in _chunks(as_of):
            for a in gdelt_fetch(beat, start, end):
                raw_rows += 1
                url = a.get("url", "")
                d = _parse_gdelt_date(a.get("seendate", ""))
                if not url or d is None or not (lo < d <= as_of):   # date gate, fail closed
                    continue
                arts.append((d, url, a))
        arts.sort(key=lambda x: x[0], reverse=True)     # newest first
        by_beat[beat] = arts[:PER_BEAT_CAP]

    seen: dict[str, dict] = {}          # url -> item (dedup across beats)
    for beat, arts in by_beat.items():
        for d, url, a in arts:
            if url not in seen:
                seen[url] = {"title": a.get("title", ""), "url": url,
                             "date": d.isoformat(), "source": a.get("domain", ""),
                             "beats": [beat], "lede": "", "ticker_hints": []}
            elif beat not in seen[url]["beats"]:
                seen[url]["beats"].append(beat)

    # Wayback-enrich each unique article with its as-of-date lede.
    wb_hits = 0
    for item in seen.values():
        lede, ok = wayback_lede(item["url"], date.fromisoformat(item["date"]))
        item["lede"] = lede
        wb_hits += int(ok)
        item["ticker_hints"] = find_ticker_hints(f"{item['title']} {lede}")
        # late-stage if it surfaced ONLY via peak/momentum beats (a trim signal, not buildup)
        item["late_stage"] = bool(item["beats"]) and all(b in PEAK_BEATS for b in item["beats"])

    items = sorted(seen.values(), key=lambda x: x["date"], reverse=True)
    if write:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / f"{as_of_str}-pool.json").write_text(json.dumps(
            {"as_of_date": as_of_str, "lookback_days": LOOKBACK_DAYS,
             "beats": BEAT_QUERIES, "items": items}, indent=2))

    n = len(items)
    with_lede = sum(1 for i in items if i["lede"])
    with_tick = sum(1 for i in items if i["ticker_hints"])
    return {"as_of": as_of_str, "raw_rows": raw_rows, "unique": n,
            "wayback_hit": wb_hits, "with_lede": with_lede, "with_ticker": with_tick,
            "wayback_pct": round(100 * wb_hits / n, 1) if n else 0.0,
            "ticker_pct": round(100 * with_tick / n, 1) if n else 0.0}


# ------------------------------------------------------------------ prompt rendering
def render_pool(pool_path: str) -> str:
    """Format a pool file as one line per article for the curator prompt, mirroring GHR's
    `_block`: `[date | source] title — lede[:200] (url)`. Falls back to the title when a
    Wayback lede is missing (headline-only degradation)."""
    pool = json.loads(Path(pool_path).read_text())
    lines = []
    for a in pool.get("items", []):
        snippet = (a.get("lede") or a.get("title") or "")[:200]
        # flag momentum-only items so the curator reads them as peak/trim signals, not buildup
        tag = " [peak/momentum beat — read as trim signal, not early buildup]" if a.get("late_stage") else ""
        lines.append(f"[{a.get('date','')} | {a.get('source','')}] {a.get('title','')}"
                     f" — {snippet} ({a.get('url','') or 'no url'}){tag}")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def _dates(args) -> list[str]:
    if args.all:
        return json.loads(CAP05_STARTER.read_text())["as_of_dates"]
    return args.dates or []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build GDELT+Wayback news pools for the backtest.")
    ap.add_argument("--validate", action="store_true",
                    help="build (or load cached) pools and print per-date quality metrics")
    ap.add_argument("--build", action="store_true", help="write pool JSON files")
    ap.add_argument("--dates", nargs="*", help="YYYY-MM-DD rebalance dates")
    ap.add_argument("--all", action="store_true", help="use all 15 quarter-ends from cap05")
    ap.add_argument("--render", help="print a pool file as curator-prompt text and exit")
    ap.add_argument("--no-pull", action="store_true",
                    help="don't hit GDELT (use cached GDELT only); Wayback still enriches")
    args = ap.parse_args(argv)

    if args.render:
        print(render_pool(args.render))
        return 0

    _cache_only[0] = args.no_pull

    dates = _dates(args)
    if not dates:
        sys.exit("no dates: pass --dates YYYY-MM-DD ... or --all")
    if not (args.validate or args.build):
        sys.exit("pass --validate and/or --build")

    stats = []
    for d in dates:
        s = build_pool(d, write=args.build)
        stats.append(s)
        print(f"{d}: raw={s['raw_rows']:4d} unique={s['unique']:3d} "
              f"wayback={s['wayback_hit']:3d} ({s['wayback_pct']:.0f}%) "
              f"lede={s['with_lede']:3d} ticker-named={s['with_ticker']:3d} ({s['ticker_pct']:.0f}%)")

    if args.validate and stats:
        n = sum(s["unique"] for s in stats)
        print("\n=== GATE METRICS (avg across dates) ===")
        print(f"  unique articles/date : {n // len(stats)}")
        print(f"  Wayback hit-rate     : {round(sum(s['wayback_hit'] for s in stats) / max(n,1) * 100)}%")
        print(f"  ledes naming a ticker: {round(sum(s['with_ticker'] for s in stats) / max(n,1) * 100)}%")
        print("  --> gate: is article volume + ticker-naming rate high enough to feed the curator?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
