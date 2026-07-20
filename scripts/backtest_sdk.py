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
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gkg_pool as g            # GKG query + filters (reused)
import news_pool as w           # wayback_lede (GHR-grade, reused)
from google.cloud import bigquery

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


# ----------------------------------------------------------------- retrieval (article list)
def build_article_pool(bq, as_of: date, news_lookback_days: int, cutoff: date,
                       max_articles: int, run_dir: Path) -> list[dict]:
    """Date-clean article list for the window (as_of - news_lookback, as_of], title + Wayback lede,
    sampled evenly per-day (not top-by-recency, which collapses to the newest day). Cached by window."""
    lo = as_of - timedelta(days=news_lookback_days)
    cache = run_dir / "_cache" / f"arts-{lo}-{as_of}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    cache.parent.mkdir(parents=True, exist_ok=True)

    kw = g._keyword_regex()
    sql = f"""SELECT {', '.join(g._FIELDS)} FROM `{g.TABLE}`
      WHERE _PARTITIONTIME BETWEEN TIMESTAMP('{lo}') AND TIMESTAMP('{as_of}')
        AND TranslationInfo IS NULL
        AND (REGEXP_CONTAINS(DocumentIdentifier, r'{kw}') OR REGEXP_CONTAINS(Extras, r'{kw}'))"""
    dry = bq.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    if dry.total_bytes_processed / 1e9 > g.MAX_SCAN_GB:
        sys.exit(f"cost guard: {dry.total_bytes_processed/1e9:.0f} GB")
    rows = list(bq.query(sql).result())

    seen, by_day = set(), collections.defaultdict(list)
    for r in rows:
        url = r["DocumentIdentifier"] or ""
        src = (r["SourceCommonName"] or "").lower()
        if not url or any(b in src for b in g.SOURCE_BLOCKLIST):
            continue
        title = g._page_title(r["Extras"]) or g._slug_title(url)
        if g.SPAM_TITLE_RE.search(title) or not g._article_waves(f"{title} {url}") \
           or not g._subject_orgs(r["V2Organizations"]):
            continue
        nt = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
        if nt in seen:
            continue
        seen.add(nt)
        d = g._gkg_date(r["DATE"])
        by_day[d].append({"title": title, "date": d, "source": r["SourceCommonName"] or "", "url": url})

    # even per-day sampling up to max_articles
    per_day = max(1, max_articles // max(len(by_day), 1))
    arts = [a for d in sorted(by_day) for a in by_day[d][:per_day]][:max_articles]
    # join Wayback ledes at the DECISION cutoff, CONCURRENTLY (6-worker pool) — CDX snapshot
    # lookups are individually slow (7-22s) but archive.org isn't rate-limiting, so overlapping
    # them turns ~3 ledes/min (serial) into ~40/min. Per-URL cache is shared, so re-runs are free.
    ledes = w.wayback_ledes([a["url"] for a in arts], cutoff)
    for a in arts:
        a["lede"] = ledes.get(a["url"], "")
    cache.write_text(json.dumps(arts))
    return arts


def render_articles(arts: list[dict]) -> str:
    lines = ["DATE-CLEAN NEWS ARTICLES (title + snippet). Discover the tickers; discard non-investable "
             "noise (war/weather events, private cos, foreign/OTC, keyword false-matches):"]
    for a in arts:
        snip = (a.get("lede") or a["title"])[:220]
        lines.append(f"\n[{a['date']} | {a['source']}] {a['title'][:90]}\n   {snip} ({a['url']})")
    return "\n".join(lines)


# ----------------------------------------------------------------- curator (Anthropic SDK)
_CURATOR_SYSTEM = (ROOT / ".claude" / "agents" / "watchlist-curator.md").read_text()


def call_curator(cli, model: str, as_of: str, watchlist: list[str], thesis: str, exclusions: str,
                 max_size: int, anchors: list[str], articles_text: str, cadence: str,
                 log_path: Path | None) -> dict:
    user = f"""Backtest, article-list mode (forward-resembling: a raw list of date-clean news ARTICLES with title + snippet, like live WebSearch results — you discover the tickers and filter the noise yourself).
- as_of_date: {as_of}
- current_watchlist: {watchlist}
- max_watchlist_size: {max_size} (managed slots; {anchors} are always_include anchors, off-limits, don't count). Any ADD needs a paired REMOVE, or no_changes.
- rebalance_period: {cadence} (you are re-run every {cadence} — calibrate churn to this cadence; most {cadence} windows warrant no_changes, act only on a genuine catalyst)
- profile_wave_thesis: {thesis}
- exclusions: {exclusions}

news_pool (read it, discover US-listed wave tickers with real catalysts, DISCARD the noise):
{articles_text}

Only swap (add+remove together) if a clearly stronger rising wave vehicle appears vs a current holding, else no_changes. In rationale_overall, note what noise you filtered. Emit ONLY the JSON object per your output schema."""
    # max_tokens must cover the model's (default) thinking block PLUS the JSON output — 2000 was
    # entirely consumed by thinking, leaving no text and silently defaulting every call to no_changes.
    r = cli.messages.create(model=model, max_tokens=8000, system=_CURATOR_SYSTEM,
                            messages=[{"role": "user", "content": user}])
    # concatenate all text blocks (the model may emit a ThinkingBlock before the TextBlock)
    txt = "".join(getattr(b, "text", "") for b in r.content).strip()
    if log_path:
        log_path.write_text(json.dumps({"as_of": as_of, "model": model, "user": user, "response": txt,
                                        "usage": {"in": r.usage.input_tokens, "out": r.usage.output_tokens}}, indent=2))
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {"as_of_date": as_of, "adds": [], "removes": [], "no_changes": True}


# ----------------------------------------------------------------- walk-forward
def _rebalance_dates(start: str, end: str, cadence_days: int) -> list[str]:
    out, d, e = [], date.fromisoformat(start), date.fromisoformat(end)
    while d <= e:
        out.append(d.isoformat()); d += timedelta(days=cadence_days)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SDK GKG+Wayback backtest harness.")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--cadence", default="weekly", choices=list(CADENCE_DAYS))
    ap.add_argument("--news-lookback-days", type=int, default=None, help="default = cadence")
    ap.add_argument("--optimizer-lookback-days", type=int, default=30)
    ap.add_argument("--max-articles", type=int, default=100)
    ap.add_argument("--starter", nargs="+", default=["AAPL", "MSFT", "GOOGL", "NVDA", "SPY"])
    ap.add_argument("--max-watchlist-size", type=int, default=5)
    ap.add_argument("--max-weight", type=float, default=1.0)
    ap.add_argument("--risk-aversion", type=float, default=0.5)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--log-llm", action="store_true")
    ap.add_argument("--pools-only", action="store_true", help="build article pools, skip curator + replay")
    a = ap.parse_args(argv)

    news_lb = a.news_lookback_days or CADENCE_DAYS[a.cadence]
    run_dir = ROOT / a.run_dir; run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "_log").mkdir(exist_ok=True)
    dates = _rebalance_dates(a.start, a.end, CADENCE_DAYS[a.cadence])
    print(f"{len(dates)} rebalances ({a.cadence}, news window {news_lb}d), {a.start}..{a.end}", file=sys.stderr)

    bq = g._client()
    cli = None if a.pools_only else _anthropic()
    thesis = ("Ride durable waves to early exposure, trim before the crest. Current wave = AI. Next tech "
              "waves: rockets & spacecraft, robotics, quantum, nuclear (SMRs near-term, fusion long-term). "
              "Non-tech: geopolitical realignment (defense/rearmament, tankers/shipping, drones), aging demographics.")
    exclusions = "solar energy, wind energy"
    anchors = ["SPY", "AGG", "IAU"]
    tok_in = tok_out = 0

    from src import portfolio
    hist = run_dir / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    sb_hold = run_dir / "_wf_holdings.csv"
    sb_hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in a.starter + anchors))

    for d in dates:
        cur_wl = portfolio.reconstruct_watchlist_at(d, a.starter, str(hist))
        arts = build_article_pool(bq, date.fromisoformat(d), news_lb, date.fromisoformat(d),
                                  a.max_articles, run_dir)
        (run_dir / f"{d}-pool.json").write_text(json.dumps(
            {"as_of_date": d, "news_lookback_days": news_lb, "source": "gkg-wayback-articles",
             "n_articles": len(arts), "hit_rate": round(sum(1 for x in arts if x.get("lede"))/max(len(arts),1), 2),
             "articles": arts}, indent=2))
        if a.pools_only:
            print(f"  {d}: {len(arts)} articles", file=sys.stderr); continue
        log = (run_dir / "_log" / f"{d}-curator.json") if a.log_llm else None
        cur = call_curator(cli, a.model, d, cur_wl, thesis, exclusions, a.max_watchlist_size,
                           anchors, render_articles(arts), a.cadence, log)
        cur["as_of_date"] = d
        (run_dir / f"{d}-curation.json").write_text(json.dumps(cur, indent=2))
        portfolio.apply_curator_decisions(cur, holdings_path=str(sb_hold), history_path=str(hist),
                                          profile_path="investor_profile.md", listing_check=False, as_of_date=d)
        if log:
            lg = json.loads(log.read_text()); tok_in += lg["usage"]["in"]; tok_out += lg["usage"]["out"]
        print(f"  {d}: {len(arts)} arts | adds={[x['ticker'] for x in cur.get('adds',[])]} "
              f"removes={[x['ticker'] for x in cur.get('removes',[])]}", file=sys.stderr)

    if a.pools_only:
        return 0
    # write _starter.json and replay
    (run_dir / "_starter.json").write_text(json.dumps(
        {"starter_watchlist": a.starter, "as_of_dates": dates, "rebalance_period": a.cadence,
         "initial_usd": 50000.0, "lookback_years": a.optimizer_lookback_days / 365.0,
         "max_watchlist_size": a.max_watchlist_size, "start_date": a.start, "end_date": a.end}, indent=2))
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
