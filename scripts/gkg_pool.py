#!/usr/bin/env python3
"""Build a look-ahead-CLEAN wave-news pool from GDELT's GKG table on BigQuery (DISCOVERY mode).

Why this exists: the GDELT DOC API and Wayback both rate-limit hard; GKG-on-BigQuery sidesteps
both (one date-partitioned SQL query, no throttle). This is the DISCOVERY design: we pull articles
whose URL/title contains a WAVE KEYWORD (rocket, microreactor, quantum computing, hypersonic, ...) —
naming NO tickers in the query — then read the ORGANIZATIONS that GKG extracted from each article
and rank them by wave-coverage volume + tone. The curator downstream reads that ranked list of
company names + sample headlines and figures out the tickers itself (including names we've never
tracked), which is the point: discovery lives in the curator, not in the query.

Beats (wave keywords) + the noise stoplist live in wave_beats.json — the single source of truth
shared by the backtest and the live/forward path. Iterate there; no code change.

GKG has no article body, but it gives, per article: the page title (in Extras), a sentiment tone,
and the extracted organizations with character offsets (a low offset = the article is ABOUT that
company). We use the offset to keep discovery to article SUBJECTS, not passing mentions.

Auth: a BigQuery service-account key at ./gcp-key.json (gitignored), project portfolio-wave-rider.

Usage:
    python scripts/gkg_pool.py --validate --dates 2025-03-31
    python scripts/gkg_pool.py --build --all
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from google.cloud import bigquery
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parent.parent
KEY_PATH = ROOT / "gcp-key.json"
PROJECT = "portfolio-wave-rider"
TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"
RUN_DIR = ROOT / "data" / "curator_runs" / "postcovid-gkg"
CAP05_STARTER = ROOT / "data" / "curator_runs" / "postcovid-cap05" / "_starter.json"
CONFIG_FILE = ROOT / "gkg_config.json"   # ALL GKG solution params, shared backtest+forward

_cfg = json.loads(CONFIG_FILE.read_text())
_eng = _cfg["engine"]
LOOKBACK_DAYS = _eng["lookback_days"]     # trailing news window (scales with rebalance cadence)
ONTOPIC_OFFSET = _eng["ontopic_offset"]   # org counts as SUBJECT only within first N chars
TOP_COMPANIES = _eng["top_companies"]     # cap the discovered-company ranking fed to the curator
SAMPLE_HEADLINES = _eng["sample_headlines"]
MAX_SCAN_GB = _eng["max_scan_gb"]         # dry-run cost guard
WAVE_KEYWORDS = _cfg["wave_keywords"]                       # {wave: [keyword, ...]}
ORG_STOPLIST = {s.lower() for s in _cfg["org_stoplist"]}   # non-company ENTITIES (engine mechanics)


def _profile_source_lists() -> "tuple[set, list]":
    """Read source_block / source_allow (domain substrings) from news_sources.md's YAML front matter
    — the single source of truth for SOURCE curation. news_sources.md is tracked (public), unlike
    investor_profile.md, so the block-list ships with the repo. Returns (block_set, allow_list); empty
    on any missing/parse issue (a missing news_sources.md is non-fatal per CLAUDE.md — the gather then
    runs unfiltered). Only source_block is applied by the single-pass gather (drop content-farm
    domains); source_allow is exposed but not yet used here (staged for the forward engine, task #10)."""
    import re
    import yaml
    p = ROOT / "news_sources.md"
    if not p.exists():
        return set(), []
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", p.read_text(), re.DOTALL)
    if not m:
        return set(), []
    data = yaml.safe_load(m.group(1)) or {}
    block = {str(s).lower() for s in (data.get("source_block") or [])}
    allow = [str(s).lower() for s in (data.get("source_allow") or [])]
    return block, allow


SOURCE_BLOCKLIST, SOURCE_ALLOWLIST = _profile_source_lists()   # content-farm domains / preferred desks
_KW_WAVE = {kw.lower(): wave for wave, kws in WAVE_KEYWORDS.items() for kw in kws}

# Securities class-action / 13F-holding boilerplate spam floods financial news — drop it.
SPAM_TITLE_RE = re.compile(
    r"class action|securities (law|fraud)|lost money on|sued for|deadline to (join|contact)"
    r"|(encourages|reminds|notifies|alerts) (investors|shareholders|\w+ (inc|corp|ltd))"
    r"|investor(s)? (alert|deadline|counsel)|shareholder (alert|rights)|trusted investor counsel"
    r"|rosen law|levi & korsinsky|kessler topaz|pomerantz|bragar eagel|glancy prongay"
    r"|robbins geller|faruqi|schall law|hagens berman|bronstein"
    r"|(sells|buys|purchases|acquires|trims|boosts|lowers|raises|increases|reduces|cuts|grows"
    r"|has|takes|holds|owns) [\w.,$& ]{0,25}?(shares|position|stake|holdings) (of|in) ",
    re.I)

_ORG_SUFFIX_RE = re.compile(
    r"\s+(inc|corp|corporation|co|company|ltd|limited|plc|llc|lp|sa|ag|nv|group|holdings)\.?$", re.I)


# ---------------------------------------------------------------------- BigQuery
def _client() -> bigquery.Client:
    if not KEY_PATH.exists():
        sys.exit(f"missing {KEY_PATH} (BigQuery service-account key)")
    creds = service_account.Credentials.from_service_account_file(str(KEY_PATH))
    return bigquery.Client(credentials=creds, project=PROJECT)


def _keyword_regex() -> str:
    """Alternation of every wave keyword, case-insensitive. A space in a keyword matches a space
    OR a hyphen (so 'quantum computing' also hits the URL slug 'quantum-computing')."""
    frags = []
    for kw in sorted(_KW_WAVE):
        esc = re.escape(kw).replace(r"\ ", r"[\s\-]")
        frags.append(rf"\b{esc}\b")
    return r"(?i)(" + "|".join(frags) + r")"


_FIELDS = ("DATE", "SourceCommonName", "DocumentIdentifier", "V2Organizations", "V2Tone", "Extras")


def gkg_query(client: bigquery.Client, as_of: date) -> list[dict]:
    """Fetch wave-keyword articles in the trailing window (no tickers named), as plain dicts.
    Cached by (date, beats-hash): re-runs that only change the Python filters reuse the cache for
    free; changing wave_beats.json's keywords re-queries. Dry-run cost guard aborts over MAX_SCAN_GB.
    Delete the cache file to force a re-query."""
    lo = as_of - timedelta(days=LOOKBACK_DAYS)
    bhash = hashlib.md5(_keyword_regex().encode()).hexdigest()[:8]
    cache = RUN_DIR / "_cache" / f"{as_of}-kw-{bhash}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    kw = _keyword_regex()
    sql = f"""
    SELECT {', '.join(_FIELDS)}
    FROM `{TABLE}`
    WHERE _PARTITIONTIME BETWEEN TIMESTAMP('{lo}') AND TIMESTAMP('{as_of}')
      AND TranslationInfo IS NULL                       -- English-origin
      AND (REGEXP_CONTAINS(DocumentIdentifier, r'{kw}') OR REGEXP_CONTAINS(Extras, r'{kw}'))
    """
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    gb = dry.total_bytes_processed / 1e9
    if gb > MAX_SCAN_GB:
        sys.exit(f"cost guard: query would scan {gb:.1f} GB > {MAX_SCAN_GB} GB; aborting")
    print(f"  [{as_of}] scanning {gb:.1f} GB (caching for reuse) ...", file=sys.stderr)
    job = client.query(sql)
    rows = [{f: r[f] for f in _FIELDS} for r in job.result()]
    _log_cost(str(as_of), gb, (job.total_bytes_billed or 0) / 1e9)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows))
    return rows


def _log_cost(as_of: str, scanned_gb: float, billed_gb: float) -> None:
    """Append this query's BigQuery cost to a running log (only real queries, not cache hits)."""
    f = RUN_DIR / "_log" / "bigquery_cost.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps({"date": as_of, "scanned_gb": round(scanned_gb, 2),
                             "billed_gb": round(billed_gb, 2), "ts": datetime.now().isoformat()}) + "\n")


def cost_summary() -> None:
    f = RUN_DIR / "_log" / "bigquery_cost.jsonl"
    if not f.exists():
        print("no BigQuery cost logged yet"); return
    es = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    billed = sum(e["billed_gb"] for e in es)
    usd = max(0.0, billed - 1000) / 1000 * 6.25          # $6.25/TB, first 1 TB/month free
    print(f"BigQuery cost: {len(es)} real queries, {billed:.0f} GB billed, "
          f"~${usd:.2f} (first 1000 GB/month free -> ${usd:.2f} over)")


# ---------------------------------------------------------------------- helpers
def _page_title(extras: str) -> str:
    m = re.search(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", extras or "", re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _slug_title(url: str) -> str:
    seg = re.sub(r"[?#].*$", "", (url or "").rstrip("/")).split("/")[-1]
    seg = re.sub(r"\.(html?|php|aspx?)$", "", seg)
    seg = re.sub(r"\b\d{5,}\b", "", seg)
    words = re.split(r"[-_]+", seg)
    return " ".join(w.capitalize() for w in words if w).strip() or "(headline in URL)"


def _tone(v2tone: str) -> float:
    try:
        return float((v2tone or "0").split(",")[0])
    except (ValueError, IndexError):
        return 0.0


def _gkg_date(d) -> str:
    s = str(d)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _subject_orgs(orgs: str) -> list[str]:
    """Organizations the article is ABOUT: those appearing within ONTOPIC_OFFSET chars, minus the
    stoplist. Returned as normalized display names (trailing Inc/Corp/Ltd... stripped for dedup)."""
    best = {}
    for part in (orgs or "").split(";"):
        if "," not in part:
            continue
        name, off = part.rsplit(",", 1)
        if not off.isdigit() or int(off) > ONTOPIC_OFFSET:
            continue
        norm = _ORG_SUFFIX_RE.sub("", name.strip()).strip()
        low = norm.lower()
        if len(norm) < 4 or any(s in low for s in ORG_STOPLIST):   # substring stoplist match
            continue
        best[norm.lower()] = norm            # keep a representative display form
    return list(best.values())


def _article_waves(text: str) -> list[str]:
    """Which waves' keywords appear in this article's title/URL."""
    low = (text or "").lower()
    waves = set()
    for kw, wave in _KW_WAVE.items():
        if re.search(rf"\b{re.escape(kw).replace(chr(92)+' ', '[ -]')}\b", low):
            waves.add(wave)
    return sorted(waves)


# ---------------------------------------------------------------------- pool build
def build_pool(client: bigquery.Client, as_of_str: str, write: bool = True) -> dict:
    as_of = date.fromisoformat(as_of_str)
    rows = gkg_query(client, as_of)
    # aggregate per discovered company
    agg = collections.defaultdict(lambda: {"articles": 0, "tone": [], "waves": collections.Counter(),
                                           "samples": []})
    # filter audit: WHY articles were dropped, so we can verify (later) we aren't discarding
    # useful news — especially which domains the source_block list removed and how many each.
    audit = {"dropped_blocklist": 0, "dropped_spam": 0, "dropped_no_wave": 0,
             "dropped_no_subject_org": 0, "blocked_domains": collections.Counter(),
             "kept_sources": collections.Counter(),      # KEPT article domains (source diversity)
             "kept_by_age": collections.Counter(),        # KEPT article age-in-window (look-ahead/recency)
             "kept_dates": collections.Counter()}         # KEPT articles per calendar day (gap detection)
    kept = 0
    for r in rows:
        url = r["DocumentIdentifier"] or ""
        if not url:
            continue
        src = (r["SourceCommonName"] or "").lower()
        hit = next((b for b in SOURCE_BLOCKLIST if b in src), None)
        if hit:                                          # drop content-farm / 13F-mill domains
            audit["dropped_blocklist"] += 1
            audit["blocked_domains"][r["SourceCommonName"] or hit] += 1
            continue
        title = _page_title(r["Extras"]) or _slug_title(url)
        if SPAM_TITLE_RE.search(title):
            audit["dropped_spam"] += 1
            continue
        waves = _article_waves(f"{title} {url}")
        if not waves:                                    # keyword only matched embedded links etc.
            audit["dropped_no_wave"] += 1
            continue
        orgs = _subject_orgs(r["V2Organizations"])
        if not orgs:
            audit["dropped_no_subject_org"] += 1
            continue
        kept += 1
        audit["kept_sources"][r["SourceCommonName"] or "?"] += 1
        _ad = _gkg_date(r["DATE"])
        audit["kept_by_age"][min((as_of - date.fromisoformat(_ad)).days // 15, 6)] += 1  # 15-day buckets
        audit["kept_dates"][_ad] += 1
        tone = _tone(r["V2Tone"])
        for org in orgs:
            a = agg[org]
            a["articles"] += 1
            a["tone"].append(tone)
            for w in waves:
                a["waves"][w] += 1
            if len(a["samples"]) < SAMPLE_HEADLINES:
                a["samples"].append({"title": title, "date": _gkg_date(r["DATE"]),
                                     "source": r["SourceCommonName"] or "", "url": url})

    companies = []
    for name, a in agg.items():
        companies.append({
            "company": name,
            "articles": a["articles"],
            "avg_tone": round(sum(a["tone"]) / len(a["tone"]), 1),
            "waves": [w for w, _ in a["waves"].most_common()],
            "samples": a["samples"],
        })
    companies.sort(key=lambda c: -c["articles"])
    companies = companies[:TOP_COMPANIES]
    audit["blocked_domains"] = dict(audit["blocked_domains"].most_common())
    audit["kept_sources"] = dict(audit["kept_sources"].most_common(40))
    audit["kept_by_age"] = {str(k): v for k, v in sorted(audit["kept_by_age"].items())}
    audit["kept_dates"] = dict(sorted(audit["kept_dates"].items()))
    audit["kept"] = kept

    if write:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / f"{as_of_str}-pool.json").write_text(json.dumps(
            {"as_of_date": as_of_str, "lookback_days": LOOKBACK_DAYS, "source": "gkg-discovery",
             "wave_keywords": WAVE_KEYWORDS, "filter_audit": audit, "companies": companies}, indent=2))
        # standalone filter-audit for easy review (what the filters dropped, by reason + domain)
        (RUN_DIR / "_log").mkdir(exist_ok=True)
        (RUN_DIR / "_log" / f"{as_of_str}-filter-audit.json").write_text(json.dumps(audit, indent=2))
    return {"as_of": as_of_str, "rows": len(rows), "wave_articles": kept,
            "companies": len(agg), "top": companies, "audit": audit}


# ---------------------------------------------------------------------- prompt rendering
def render_pool(pool_path: str) -> str:
    """Format a discovery pool as curator-prompt text: each discovered company with its
    wave-coverage volume, tone, waves, and a few sample headlines."""
    pool = json.loads(Path(pool_path).read_text())
    lines = ["DISCOVERED COMPANIES (ranked by wave-news coverage; map to tickers, drop non-investable):"]
    for c in pool.get("companies", []):
        lines.append(f"\n{c['company']} — {c['articles']} articles, tone {c['avg_tone']:+.1f}, "
                     f"waves: {', '.join(c['waves'])}")
        for s in c.get("samples", []):
            lines.append(f"   • [{s['date']} | {s['source']}] {s['title'][:110]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI
def _dates(args) -> list[str]:
    if args.all:
        return json.loads(CAP05_STARTER.read_text())["as_of_dates"]
    return args.dates or []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build GKG (BigQuery) keyword-discovery wave-news pools.")
    ap.add_argument("--validate", action="store_true", help="print the discovered-company ranking")
    ap.add_argument("--build", action="store_true", help="write pool JSON files")
    ap.add_argument("--dates", nargs="*", help="YYYY-MM-DD rebalance dates")
    ap.add_argument("--all", action="store_true", help="all 15 quarter-ends from cap05")
    ap.add_argument("--render", help="print a pool file as curator-prompt text and exit")
    ap.add_argument("--cost", action="store_true", help="print accumulated BigQuery cost and exit")
    args = ap.parse_args(argv)

    if args.render:
        print(render_pool(args.render))
        return 0
    if args.cost:
        cost_summary()
        return 0

    dates = _dates(args)
    if not dates:
        sys.exit("pass --dates YYYY-MM-DD ... or --all")
    if not (args.validate or args.build):
        sys.exit("pass --validate and/or --build")

    client = _client()
    for d in dates:
        s = build_pool(client, d, write=args.build)
        print(f"{d}: rows={s['rows']}  wave_articles={s['wave_articles']}  "
              f"companies_discovered={s['companies']}")
        if args.validate:
            print("  top discovered companies (curator maps these to tickers, filters non-investable):")
            for c in s["top"][:25]:
                print(f"    {c['articles']:4d}  tone{c['avg_tone']:+.1f}  {'/'.join(c['waves']):22} {c['company']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
