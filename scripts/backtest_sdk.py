#!/usr/bin/env python3
"""SDK backtest harness — the whole GKG+Wayback backtest as ONE Python program.

Replaces the skill-driven, Claude-Code-in-the-loop orchestration (which had to fire the curator
by hand, one batch per turn). Here the walk-forward runs itself:

  for each rebalance date:
    build a date-clean ARTICLE LIST (GKG discovery + Wayback lede, look-ahead-clean)   [retrieval]
    -> call the watchlist-curator via the Anthropic SDK (it discovers tickers, filters noise)
    -> apply the curation to a sandboxed watchlist (walk-forward)
  then replay the saved curations through the mean-variance optimizer                 [curation+opt]

This is the article-list variant validated as "option C" (the curator's LLM sifts the raw firehose).
Everything is a CLI parameter so we can sweep configs. Curator = client.messages (system prompt =
.claude/agents/watchlist-curator.md). Auth: ANTHROPIC_API_KEY + gcp-key.json in .env / repo root.

Example (1-year weekly, up to the day before the daily cron started):
  python scripts/backtest_sdk.py --start 2025-05-04 --end 2026-05-03 --cadence weekly \
    --run-dir data/curator_runs/oneyear-gkg-wayback --log-llm
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root, for `from src import ...`
import gkg_pool as g            # GKG query + filters (reused)
import news_pool as w           # wayback_lede (GHR-grade, reused)
from google.cloud import bigquery
from src import curator          # SHARED curator (prompt + SDK call + parse); forward uses it too

ROOT = g.ROOT
CADENCE_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91}


# ----------------------------------------------------------------- keys / clients
def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return ""


def _anthropic():
    import anthropic
    k = _env("ANTHROPIC_API_KEY")
    if not k:
        sys.exit("ANTHROPIC_API_KEY empty in .env")
    return anthropic.Anthropic(api_key=k)


NO_REASONING = False   # set by --no-reasoning; disables OpenRouter models' reasoning (kimi etc. run ~free of the slow thinking pass)


def _llm_complete(model: str, system: str, user: str, max_tokens: int, anthropic_cli):
    """One completion, provider-agnostic. A `claude-*` model routes to the Anthropic SDK; a
    `vendor/model` id (e.g. `deepseek/deepseek-v4-flash`) routes to OpenRouter's OpenAI-compatible
    chat/completions endpoint (raw requests, no extra dep). Returns (text, tokens_in, tokens_out)."""
    if model.startswith("claude"):
        r = anthropic_cli.messages.create(model=model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": user}])
        txt = "".join(getattr(b, "text", "") for b in r.content).strip()
        return txt, r.usage.input_tokens, r.usage.output_tokens
    import requests
    key = _env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY empty in .env")
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if NO_REASONING:
        body["reasoning"] = {"enabled": False}   # skip the reasoning pass (huge speed-up on reasoning models)
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body, timeout=120)
    resp.raise_for_status()
    j = resp.json()
    txt = (j["choices"][0]["message"].get("content") or "").strip()
    u = j.get("usage", {})
    return txt, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


# ----------------------------------------------------------------- retrieval (article list)
# INGESTION is decoupled from the curator's read window: the whole backtest period is pulled from GKG
# ONCE into a per-day corpus (ingest_gkg_corpus), and each rebalance's pool is a pure SLICE of that
# corpus (build_article_pool). So changing news_lookback_days never re-queries GKG — it just re-slices.
CORPUS_INGEST_BUFFER = 95   # extra days pulled BEFORE the first rebalance so any news_lookback up to a
                            # quarter is already in the corpus (no GKG re-pull when the window widens).


def _month_starts(start: date, end: date) -> list[date]:
    """First-of-month dates spanning [start, end] inclusive (month chunks for the GKG scan)."""
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return out


def _filtered_articles(rows) -> list[dict]:
    """Apply the source-block / spam / wave / subject-org filters to raw GKG rows -> article dicts
    (no cross-day dedup here; the read layer dedups syndicated titles across its window)."""
    arts = []
    for r in rows:
        url = r["DocumentIdentifier"] or ""
        src = (r["SourceCommonName"] or "").lower()
        if not url or g._domain_in(src, g.SOURCE_BLOCKLIST):
            continue
        title = g._page_title(r["Extras"]) or g._slug_title(url)
        if g.SPAM_TITLE_RE.search(title) or not g._article_waves(f"{title} {url}") \
           or not g._subject_orgs(r["V2Organizations"]):
            continue
        arts.append({"title": title, "date": g._gkg_date(r["DATE"]),
                     "source": r["SourceCommonName"] or "", "url": url})
    return arts


def ingest_gkg_corpus(bq, start: date, end: date, run_dir: Path) -> None:
    """Pull + filter the WHOLE GKG window ONCE into per-day caches (_corpus/gkg-<date>.json). The
    per-rebalance pool is then a pure slice of these day-files, so widening news_lookback_days never
    re-queries GKG. No ledes are fetched here (they are as_of-dependent, joined at read time); the
    corpus is purely the date-honest article metadata. Idempotent: a month whose every day-file
    already exists is skipped, so a re-run costs only the BigQuery scans for genuinely-new days."""
    cdir = run_dir / "_corpus"
    cdir.mkdir(parents=True, exist_ok=True)
    kw = g._keyword_regex()
    for ms in _month_starts(start, end):
        me = date(ms.year + (ms.month == 12), (ms.month % 12) + 1, 1) - timedelta(days=1)
        lo, hi = max(ms, start), min(me, end)
        days = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
        if all((cdir / f"gkg-{d}.json").exists() for d in days):
            continue
        sql = f"""SELECT {', '.join(g._FIELDS)} FROM `{g.TABLE}`
          WHERE _PARTITIONTIME BETWEEN TIMESTAMP('{lo}') AND TIMESTAMP('{hi} 23:59:59')
            AND TranslationInfo IS NULL
            AND (REGEXP_CONTAINS(DocumentIdentifier, r'{kw}') OR REGEXP_CONTAINS(Extras, r'{kw}'))"""
        dry = bq.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
        if dry.total_bytes_processed / 1e9 > g.MAX_SCAN_GB:
            sys.exit(f"cost guard: {dry.total_bytes_processed/1e9:.0f} GB for {lo}..{hi}")
        by_day = collections.defaultdict(list)
        for art in _filtered_articles(bq.query(sql).result()):
            by_day[art["date"]].append(art)
        for d in days:                                  # one file per day (empty list if no articles)
            (cdir / f"gkg-{d}.json").write_text(json.dumps(by_day.get(str(d), [])))
        print(f"  corpus {lo}..{hi}: {sum(len(v) for v in by_day.values())} articles", file=sys.stderr)


_authority = g.authority   # source-authority multiplier; moved to gkg_pool, shared with the forward path


_TITLE_STOP = {"the", "and", "for", "with", "that", "this", "from", "have", "will", "been", "are",
               "was", "new", "said", "its", "has", "after", "into", "about", "over", "more", "than",
               "2023", "2024", "2025", "2026", "inc", "corp", "ltd", "plc", "news", "report", "update",
               "says", "amid", "could", "would", "first", "week", "year", "day"}


def _title_consistent(title: str, lede: str) -> bool:
    """Guard against URL-recycling aggregators (finanznachrichten, tmcnet-style wires) that serve a
    DIFFERENT, often LATER article at the same URL — the worst live-fallback look-ahead case. Keep a
    live lede only if it still shares a distinctive word with the GKG-recorded title. Deliberately
    conservative: reject ONLY when the title has enough distinctive words to judge (>=3) AND the live
    text shares NONE of them, so benign extraction drift (same story, different span) is kept."""
    toks = [t for t in re.findall(r"[a-z0-9]+", title.lower()) if len(t) >= 4 and t not in _TITLE_STOP]
    dist = list(dict.fromkeys(toks))
    if len(dist) < 3:
        return True                 # too few distinctive words to judge -> keep (don't over-reject)
    low = lede.lower()
    return any(t in low for t in dist)


def _apply_live_fallback(arts: list[dict], all_urls: bool = False) -> None:
    """LOOK-AHEAD-BIASED lede recovery: fetch the source URL as it exists TODAY (trafilatura), gated by
    _title_consistent, and store it in a SEPARATE `lede_live` field (never `lede`). The clean Wayback
    lede in `lede` is untouched, so which render an arm gets is decided at render time (clean / fuller /
    live-only), not here.

    all_urls=False (default): only Wayback-MISSES are fetched; a gated hit promotes lede_source
      none->live. This serves the clean (Wayback-only) and fuller (Wayback+live) arms.
    all_urls=True: ALSO fetch Wayback-HITS, attaching lede_live as an alternative WITHOUT changing
      lede_source (it stays "wayback"). This additionally serves the live-only arm (which renders
      lede_live for every article, ignoring the clean field) — the "do we even need Wayback" test."""
    targets = [a["url"] for a in arts
               if not a.get("lede_live") and (all_urls or not a.get("lede"))]
    if not targets:
        return
    live = w.live_ledes(list(dict.fromkeys(targets)))
    for a in arts:
        if a.get("lede_live"):
            continue
        lv = live.get(a["url"], "")
        if not lv or not _title_consistent(a["title"], lv):
            continue                      # dead link OR a topic-swapped URL-recycle we reject
        a["lede_live"] = lv
        if not a.get("lede"):
            a["lede_source"] = "live"     # a Wayback-miss promoted; hits keep lede_source="wayback"


def build_article_pool(as_of: date, news_lookback_days: int, max_articles: int,
                       run_dir: Path, live_fallback: bool = False, live_all: bool = False) -> list[dict]:
    """Assemble one rebalance's pool by SLICING the pre-ingested corpus for (as_of - news_lookback,
    as_of], then RANKING it like a search engine (so the curator's input resembles live WebSearch),
    NOT sampling arbitrarily. No GKG query here — changing news_lookback_days only re-slices.

    Ranking (all signals date-honest, so no PageRank-mooning):
      1. Salience = distinct outlets that carried the story that window (contemporaneous coverage) —
         syndicated copies are collapsed and COUNTED, not just deduped.
      2. Authority = preferred-source multiplier (news_sources.md allow-list).
      3. Per-wave allocation = top-K per wave beat (mimics one query per beat), then fill remaining
         slots by overall score. This fills the whole max_articles budget (fixes the old undershoot).
    Ledes are fetched ONLY for the selected top articles, at the as_of cutoff."""
    cache = run_dir / "_cache" / f"pool-{as_of}-{news_lookback_days}d-{max_articles}.json"
    if cache.exists():
        arts = json.loads(cache.read_text())
        changed = False
        if live_fallback and not all("lede_source" in a for a in arts):
            _apply_live_fallback(arts); changed = True     # cache predates live-fallback: augment
        if live_all and any(not a.get("lede_live") and a.get("lede") for a in arts):
            _apply_live_fallback(arts, all_urls=True); changed = True   # attach live to Wayback-hits too
        if changed:
            cache.write_text(json.dumps(arts))             # rewrite (no GKG re-query; Wayback/live cached)
        return arts
    cache.parent.mkdir(parents=True, exist_ok=True)
    cdir = run_dir / "_corpus"

    day_arts: list[dict] = []
    d = as_of - timedelta(days=news_lookback_days)
    while d <= as_of:
        f = cdir / f"gkg-{d}.json"
        if f.exists():
            day_arts.extend(json.loads(f.read_text()))
        d += timedelta(days=1)

    # collapse syndication into stories + rank like a search engine (salience x authority, per-wave
    # top-K). Shared with the forward GKG+Wayback backfill so the two select identically.
    arts = g.rank_stories(day_arts, max_articles)

    # Ledes use the as_of cutoff (look-ahead-clean, and gives archive.org the full window to have
    # captured the URL). A stable per-article cutoff would fully decouple Wayback from the lookback,
    # but it starves archival time and craters the hit-rate (a 7-day cutoff misses captures that land
    # weeks after publication). GKG is already decoupled (the corpus); Wayback is pay-once-per-(url,
    # as_of): widening the lookback fetches only the newly-included older articles' ledes, then caches.
    ledes = w.wayback_ledes([a["url"] for a in arts], as_of)
    for a in arts:
        a["lede"] = ledes.get(a["url"], "")
        a["lede_source"] = "wayback" if a["lede"] else "none"   # tagged; live-fallback may upgrade "none"
    if live_fallback or live_all:
        _apply_live_fallback(arts, all_urls=live_all)
    cache.write_text(json.dumps(arts))
    return arts


def render_articles(arts: list[dict], mode: str = "clean") -> str:
    """Render the pool as the curator's news input, choosing which lede each article shows:
      clean     — only look-ahead-CLEAN Wayback ledes (`lede`), title-only otherwise. The trustworthy floor.
      fuller    — clean Wayback lede, else the biased live-fallback (`lede_live`). Wayback + fallback.
      live_only — the live lede (`lede_live`) for EVERY article, ignoring the clean field; title-only if
                  no live lede. Fully look-ahead-biased — the "do we even need Wayback" arm."""
    lines = ["DATE-CLEAN NEWS ARTICLES (title + snippet). Discover the tickers; discard non-investable "
             "noise (war/weather events, private cos, foreign/OTC, keyword false-matches):"]
    for a in arts:
        if mode == "live_only":
            lede = a.get("lede_live", "")
        elif mode == "fuller":
            lede = a.get("lede") or a.get("lede_live", "")
        else:
            lede = a.get("lede", "")
        snip = (lede or a["title"])[:220]
        lines.append(f"\n[{a['date']} | {a['source']}] {a['title'][:90]}\n   {snip} ({a['url']})")
    return "\n".join(lines)


# ----------------------------------------------------------------- curator (Anthropic SDK)
_CURATOR_SYSTEM = (ROOT / ".claude" / "agents" / "watchlist-curator.md").read_text()


def _try_parse(txt: str) -> dict | None:
    """Extract the curator's JSON object, tolerating a trailing-comma emission. Returns the dict, or None
    if the text has no salvageable JSON object (side-effect-free so the caller can retry)."""
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    block = m.group(0)
    for cand in (block, re.sub(r",(\s*[}\]])", r"\1", block)):       # 2nd pass: strip trailing commas
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def call_curator(cli, model: str, as_of: str, watchlist: list[str], thesis: str, exclusions: str,
                 max_size: int, anchors: list[str], articles_text: str, cadence: str,
                 log_path: Path | None, fail_dir: Path) -> dict:
    # Delegates to the SHARED curator (src/curator.py) so the backtest and the forward loop run one
    # prompt + parse + retry path. intro=_BT_INTRO keeps the backtest's exact wording (byte-identical
    # prompt), and NO_REASONING preserves the --no-reasoning speed setting.
    return curator.curate(articles_text, watchlist, as_of=as_of, model=model, anthropic_cli=cli,
                          thesis=thesis, exclusions=exclusions, max_size=max_size, anchors=anchors,
                          cadence=cadence, intro=curator._BT_INTRO, no_reasoning=NO_REASONING,
                          log_path=log_path, fail_dir=fail_dir)


# ----------------------------------------------------------------- walk-forward
def _rebalance_dates(start: str, end: str, cadence_days: int) -> list[str]:
    out, d, e = [], date.fromisoformat(start), date.fromisoformat(end)
    while d <= e:
        out.append(d.isoformat()); d += timedelta(days=cadence_days)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SDK GKG+Wayback backtest harness.")
    ap.add_argument("--start", default=None, help="default: investor_profile.md backtest.start_date")
    ap.add_argument("--end", default=None, help="default: investor_profile.md backtest.end_date")
    # These default to investor_profile.md (the source of truth); a CLI flag overrides per invocation.
    ap.add_argument("--cadence", default=None, help="default: profile rebalance_period")
    ap.add_argument("--news-lookback-days", type=int, default=None, help="default: profile news_lookback_days")
    ap.add_argument("--optimizer-lookback-days", type=int, default=None, help="default: profile optimizer_lookback_days")
    ap.add_argument("--max-watchlist-size", type=int, default=None, help="default: profile max_watchlist_size")
    ap.add_argument("--max-weight", type=float, default=None, help="default: profile concentration_cap")
    ap.add_argument("--risk-aversion", type=float, default=None, help="default: profile risk_aversion")
    ap.add_argument("--max-articles", type=int, default=None, help="default: profile max_articles")
    ap.add_argument("--starter", nargs="+", default=None,
                    help="default: investor_profile.md starter_watchlist (the buy-and-hold baseline)")
    ap.add_argument("--model", default=None, help="curator LLM; default: profile backtest.curator_model "
                    "(claude-* -> Anthropic, vendor/model -> OpenRouter)")
    ap.add_argument("--no-reasoning", action="store_true",
                    help="disable OpenRouter models' reasoning pass (much faster on reasoning models like kimi)")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--log-llm", action="store_true")
    ap.add_argument("--pools-only", action="store_true", help="build article pools, skip curator + replay")
    ap.add_argument("--live-fallback", action="store_true",
                    help="fetch live ledes for Wayback-MISSES (LOOK-AHEAD-BIASED, stored in lede_live); "
                         "fills the pool data but only feeds the curator per --news-mode")
    ap.add_argument("--live-all", action="store_true",
                    help="ALSO fetch live ledes for Wayback-HITS (attached as lede_live, clean field kept) "
                         "so the pool can serve the live_only arm; implied by --news-mode live_only")
    ap.add_argument("--news-mode", choices=["clean", "fuller", "live_only"], default="clean",
                    help="which lede the curator reads: clean=Wayback-only (floor); fuller=Wayback+live "
                         "fallback; live_only=live ledes for every url (ignores Wayback). Default clean")
    a = ap.parse_args(argv)
    if a.news_mode == "fuller":
        a.live_fallback = True            # can't show live ledes we never fetched
    if a.news_mode == "live_only":
        a.live_all = True                 # live_only renders lede_live for every article
    if a.live_all:
        a.live_fallback = True

    from src import portfolio
    fm = portfolio.load_financial_model()            # investor_profile.md is authoritative; CLI overrides
    bt = portfolio.load_backtest_config()            # backtest window (start/end) also lives in the profile
    if a.start is None:
        a.start = bt["start_date"]
    if a.end is None:
        a.end = bt["end_date"]
    if a.starter is None:
        a.starter = fm["starter_watchlist"]   # buy-and-hold baseline == investor_profile.md starter_watchlist
    if not a.starter:
        sys.exit("no starter: pass --starter or set starter_watchlist in investor_profile.md")
    if not a.start or not a.end:
        sys.exit("no window: pass --start/--end or set backtest.start_date/end_date in investor_profile.md")
    if a.cadence is None:
        a.cadence = fm["rebalance_period"]
    if a.cadence not in CADENCE_DAYS:
        sys.exit(f"unsupported cadence {a.cadence!r}; choose from {list(CADENCE_DAYS)}")
    if a.news_lookback_days is None:
        a.news_lookback_days = fm.get("news_lookback_days") or CADENCE_DAYS[a.cadence]
    if a.optimizer_lookback_days is None:
        a.optimizer_lookback_days = fm.get("optimizer_lookback_days") or 30
    if a.max_watchlist_size is None:
        a.max_watchlist_size = int(fm["max_watchlist_size"])
    if a.max_weight is None:
        a.max_weight = float(fm["concentration_cap"])
    if a.risk_aversion is None:
        a.risk_aversion = float(fm["risk_aversion"])
    if a.max_articles is None:
        a.max_articles = int(fm.get("max_articles") or 100)
    if a.model is None:
        a.model = portfolio.load_backtest_config().get("curator_model") or "claude-sonnet-5"
    global NO_REASONING
    NO_REASONING = a.no_reasoning

    news_lb = a.news_lookback_days
    run_dir = ROOT / a.run_dir; run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "_log").mkdir(exist_ok=True)
    dates = _rebalance_dates(a.start, a.end, CADENCE_DAYS[a.cadence])
    print(f"{len(dates)} rebalances ({a.cadence}, news window {news_lb}d), {a.start}..{a.end}", file=sys.stderr)

    bq = g._client()
    cli = _anthropic() if (not a.pools_only and a.model.startswith("claude")) else None  # only for Anthropic models
    thesis = ("Ride durable waves to early exposure, trim before the crest. Current wave = AI. Next tech "
              "waves: rockets & spacecraft, robotics, quantum, nuclear (SMRs near-term, fusion long-term). "
              "Non-tech: geopolitical realignment (defense/rearmament, tankers/shipping, drones), aging demographics.")
    exclusions = "solar energy, wind energy"
    anchors = ["SPY", "AGG", "IAU"]
    tok_in = tok_out = 0

    hist = run_dir / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    sb_hold = run_dir / "_wf_holdings.csv"
    sb_hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in a.starter + anchors))

    # Ingest the whole GKG corpus ONCE (buffered before the first rebalance so any news_lookback is
    # covered). After this, each pool is a pure slice — changing news_lookback_days re-queries nothing.
    ingest_gkg_corpus(bq, date.fromisoformat(a.start) - timedelta(days=CORPUS_INGEST_BUFFER),
                      date.fromisoformat(a.end), run_dir)
    # Write _starter.json up front (not after the loop) so a dashboard can be rendered mid-run against
    # whatever curations have completed so far — the run builds chronologically, so it grows in time.
    (run_dir / "_starter.json").write_text(json.dumps(
        {"starter_watchlist": a.starter, "as_of_dates": dates, "rebalance_period": a.cadence,
         "initial_usd": 50000.0, "lookback_years": a.optimizer_lookback_days / 365.0,
         "max_watchlist_size": a.max_watchlist_size, "start_date": a.start, "end_date": a.end}, indent=2))
    # Pool-building is watchlist-INDEPENDENT (it slices the corpus + fetches Wayback ledes), so
    # pre-fetch every week's pool in a background worker while the sequential curator loop below (which
    # IS watchlist-dependent — walk-forward) consumes them. This overlaps the curator's ~1 min/week with
    # the ongoing Wayback fetching instead of interleaving them, cutting wall time toward max(Wayback,
    # curator). max_workers=1: pools build one at a time (each already parallelizes its own Wayback via a
    # 10-worker pool, and serial pool-builds avoid racing the module-global _wb_bulk flag in news_pool).
    pool_ex = ThreadPoolExecutor(max_workers=1)
    pool_futs = {d: pool_ex.submit(build_article_pool, date.fromisoformat(d), news_lb,
                                   a.max_articles, run_dir, a.live_fallback, a.live_all)
                 for d in dates}
    try:
        for d in dates:
            arts = pool_futs[d].result()          # blocks until this week's pool is built (usually ahead)
            src_split = collections.Counter(x.get("lede_source", "wayback" if x.get("lede") else "none")
                                            for x in arts)
            (run_dir / f"{d}-pool.json").write_text(json.dumps(
                {"as_of_date": d, "news_lookback_days": news_lb, "source": "gkg-wayback-articles",
                 "n_articles": len(arts),
                 "hit_rate": round(src_split["wayback"] / max(len(arts), 1), 2),   # clean Wayback rate
                 "lede_sources": dict(src_split),   # {wayback (clean), live (look-ahead-biased), none}
                 "articles": arts}, indent=2))
            if a.pools_only:
                print(f"  {d}: {len(arts)} articles", file=sys.stderr); continue
            # Resume-friendly: reuse an existing curation (skip the LLM call) but still APPLY it so the
            # walk-forward history rebuilds; only fire the curator for dates not yet curated.
            cur_path = run_dir / f"{d}-curation.json"
            if cur_path.exists():
                cur = json.loads(cur_path.read_text())
            else:
                cur_wl = portfolio.reconstruct_watchlist_at(d, a.starter, str(hist))
                log = (run_dir / "_log" / f"{d}-curator.json") if a.log_llm else None
                cur = call_curator(cli, a.model, d, cur_wl, thesis, exclusions, a.max_watchlist_size,
                                   anchors, render_articles(arts, a.news_mode), a.cadence, log,
                                   run_dir / "_parse_fail")
                cur["as_of_date"] = d
                cur_path.write_text(json.dumps(cur, indent=2))
                if log:
                    lg = json.loads(log.read_text()); tok_in += lg["usage"]["in"]; tok_out += lg["usage"]["out"]
            portfolio.apply_curator_decisions(cur, holdings_path=str(sb_hold), history_path=str(hist),
                                              profile_path="investor_profile.md", listing_check=False, as_of_date=d)
            print(f"  {d}: {len(arts)} arts | adds={[x['ticker'] for x in cur.get('adds',[])]} "
                  f"removes={[x['ticker'] for x in cur.get('removes',[])]}", file=sys.stderr)
    finally:
        pool_ex.shutdown(wait=True)

    if a.pools_only:
        return 0
    # _starter.json was written up front; replay the full run now.
    res = portfolio.curator_backtest(
        runs_dir=str(run_dir), out_dir=str(run_dir / "_backtest"), max_weight=a.max_weight,
        risk_aversion=a.risk_aversion, benchmarks=["SPY"],
        lookback_years_override=a.optimizer_lookback_days / 365.0, always_include=anchors)
    print(f"\n=== RESULT: {res['realized_return']*100:+.0f}% (final ${res['final_value']:,.0f}) | "
          f"SPY {res['benchmark_returns']['SPY']*100:+.0f}% | final {res['final_watchlist']}")
    if a.log_llm:
        print(f"LLM cost: {tok_in:,} in + {tok_out:,} out tokens over {len(dates)} curator calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
