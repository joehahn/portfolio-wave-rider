#!/usr/bin/env python3
"""Build the PWR news-RETRIEVAL dashboard: docs/retrieval_pwr.html (v2 article-list run).

UPSTREAM of the curator — judges the *news gathering*, not portfolio gains. Answers:
  (1) COMPLETENESS — how much news GKG gathered across time / waves / sources, and where the
      calendar has gaps.
  (2) LOOK-AHEAD-CLEAN + no PageRank-mooning — the pool the curator actually sees is the RANKED
      top-100 (salience + authority + per-wave), resembling live WebSearch results, not a raw dump.
  (3) WAYBACK JOIN — how many of those ranked articles got an archived lede joined (green series).

Reads the v2 run in data/curator_runs/gkg-1yr-weekly-v2/:
  _corpus/gkg-<date>.json  — full gathering (460 day-files, ~98k articles). Used ONLY for the
                             total-gathered stat card.
  <date>-pool.json         — 53 rebalance pools, each the ranked top-100 shown to the curator, with
                             an article list; a non-empty `lede` means a Wayback archived lede joined.

Pool-centric: plots 2-8 use the set of UNIQUE articles across all 53 pools deduped by url (an article
recurs across overlapping 21-day windows); each unique article keeps its date/source and whether ANY
of its pool-appearances carried a Wayback lede ("has_wayback"). Pure Plotly + seaborn to match PWR's
other dashboards. Not linked from the public index.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from datetime import date, timedelta
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, "scripts")
import gkg_pool as g  # noqa: E402  (wave/domain-tier classifiers; needs the repo's scripts/ on path)

ROOT = Path(__file__).resolve().parent.parent
RUN_REL = "data/curator_runs/gkg-1yr-weekly-v2"      # data folder (relative to repo root)
POOLS = sorted(glob.glob(str(ROOT / RUN_REL / "*-pool.json")))
CORPUS = sorted(glob.glob(str(ROOT / RUN_REL / "_corpus" / "gkg-*.json")))
OUT = ROOT / "docs" / "retrieval_pwr.html"

BLUE, GREEN, ORANGE, RED, GREY = "#1f77b4", "#2ca02c", "#ff7f0e", "#e03131", "#adb5bd"
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _read(path):
    """Load JSON defensively — a missing/empty/corrupt file yields None, never a crash."""
    try:
        txt = Path(path).read_text()
        return json.loads(txt) if txt.strip() else None
    except Exception:
        return None


def _iso(s):
    """Parse an ISO date string, or None if malformed/missing."""
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def build():
    # ---- corpus: total gathered (stat card only) ----
    corpus_total = 0
    for f in CORPUS:
        d = _read(f)
        if isinstance(d, list):
            corpus_total += len(d)

    # ---- pools: the ranked top-100 lists the curator receives ----
    pools = []
    for p in POOLS:
        d = _read(p)
        if isinstance(d, dict):
            pools.append(d)
    pools.sort(key=lambda d: d.get("as_of_date", ""))

    as_of = [p.get("as_of_date", "") for p in pools]
    pool_n = [p.get("n_articles", len(p.get("articles", []))) for p in pools]
    pool_lede = [sum(1 for a in p.get("articles", []) if a.get("lede")) for p in pools]
    hit_rate = [p.get("hit_rate") for p in pools]

    # ---- unique articles across all pools, deduped by url ----
    # Each unique article keeps its date/source/title/url and has_wayback = ANY appearance had a lede.
    uniq = {}
    for p in pools:
        for a in p.get("articles", []):
            url = a.get("url")
            if not url:
                continue
            hw = bool(a.get("lede"))
            if url not in uniq:
                uniq[url] = {
                    "date": a.get("date", ""),
                    "source": a.get("source", ""),
                    "title": a.get("title", ""),
                    "url": url,
                    "has_wayback": hw,
                }
            elif hw:
                uniq[url]["has_wayback"] = True
    articles = list(uniq.values())
    n_uniq = len(articles)
    n_wb = sum(1 for a in articles if a["has_wayback"])
    pct_wb = (100.0 * n_wb / n_uniq) if n_uniq else 0.0
    # Average per-window Wayback join-rate = mean of the pools' hit_rate. This is the metric plot 8
    # charts; it differs from n_wb/n_uniq because an article recurs across overlapping windows and
    # counts as a unique "hit" if ANY appearance got a lede (an OR that inflates the unique rate).
    _hr = [h for h in hit_rate if h is not None]
    avg_hit = 100.0 * sum(_hr) / len(_hr) if _hr else 0.0

    # ---- monthly / weekly / daily / day-of-week buckets over UNIQUE articles ----
    mon_g, mon_w = Counter(), Counter()
    wk_g, wk_w = Counter(), Counter()
    day_g, day_w = Counter(), Counter()
    dow_g, dow_w = Counter(), Counter()
    for a in articles:
        dt = _iso(a["date"])
        if dt is None:
            continue
        hw = a["has_wayback"]
        mo = a["date"][:7]
        wk = (dt - timedelta(days=dt.weekday())).isoformat()   # Monday-anchored
        di = dt.isoformat()
        wd = DOW[dt.weekday()]
        mon_g[mo] += 1;  mon_w[mo] += hw
        wk_g[wk] += 1;   wk_w[wk] += hw
        day_g[di] += 1;  day_w[di] += hw
        dow_g[wd] += 1;  dow_w[wd] += hw

    mo_keys = sorted(mon_g)
    wk_keys = sorted(wk_g)
    # fill the daily range so gaps show as zeros
    day_dates = [d for d in (_iso(a["date"]) for a in articles) if d]
    day_x, day_yg, day_yw = [], [], []
    if day_dates:
        d0, d1 = min(day_dates), max(day_dates)
        x = d0
        while x <= d1:
            k = x.isoformat()
            day_x.append(k); day_yg.append(day_g.get(k, 0)); day_yw.append(day_w.get(k, 0))
            x += timedelta(days=1)

    # ---- per-wave (unique articles; first wave from title+url, else "general") ----
    wave_c = Counter()
    for a in articles:
        waves = g._article_waves(f"{a['title']} {a['url']}")
        wave_c[waves[0] if waves else "general"] += 1
    wv = wave_c.most_common()

    # ---- top source domains (unique articles), colored by authority tier ----
    src_c = Counter(a["source"] for a in articles if a["source"])
    top_src = src_c.most_common(18)

    def tier_color(src):
        if g._domain_in(src, g.PREFERRED_DOMAINS):
            return GREEN
        if g._domain_in(src, g.MAJOR_DOMAINS):
            return BLUE
        return GREY

    # ---- figure (7 rows) ---- (pool-size-per-window plot dropped: it was flat at the 100-article
    # cap for all 53 windows; that one fact is now a stat card instead.)
    titles = (
        "1. Unique articles per MONTH<br><sub><i>completeness (coarse): distinct articles shown to the "
        "curator that month</i></sub>",
        "2. Unique articles per WEEK<br><sub><i>gap check (finer), Monday-anchored: the two empty weeks "
        "(mid-June 2025) are a real GDELT/GKG outage, not a pipeline gap</i></sub>",
        "3. Unique articles per DAY<br><sub><i>gap check (finest): zero days break the line "
        "(mid-June-2025 GKG outage)</i></sub>",
        "4. Unique articles by DAY OF WEEK<br><sub><i>publication cadence: weekend dip is normal news "
        "behavior, not a gap</i></sub>",
        "5. Unique articles by wave<br><sub><i>coverage by theme (first matched wave, else general)</i></sub>",
        "6. Top source domains — tier-colored<br><sub><i>green = preferred/specialty desk, blue = major "
        "wire/outlet, grey = other</i></sub>",
        "7. Wayback join-rate over the year<br><sub><i>per-window archived-lede yield (hit_rate): the "
        "quality of the Wayback join across the run</i></sub>",
    )
    fig = make_subplots(rows=7, cols=1, vertical_spacing=0.045, subplot_titles=titles)

    # 1. articles per month (GKG only)
    fig.add_trace(go.Bar(x=mo_keys, y=[mon_g[m] for m in mo_keys], marker_color=BLUE, name="GKG"), row=1, col=1)
    # 2. articles per week (GKG only)
    fig.add_trace(go.Bar(x=wk_keys, y=[wk_g[w] for w in wk_keys], marker_color=BLUE, name="GKG"), row=2, col=1)
    # 3. articles per day (GKG only, linear y)
    fig.add_trace(go.Scatter(x=day_x, y=day_yg, mode="lines", line={"color": BLUE, "width": 1}, name="GKG"), row=3, col=1)
    # 4. day-of-week (GKG only)
    fig.add_trace(go.Bar(x=DOW, y=[dow_g.get(d, 0) for d in DOW], marker_color=BLUE, name="GKG"), row=4, col=1)
    # 5. per-wave horizontal bars
    fig.add_trace(go.Bar(x=[v for _, v in wv][::-1], y=[k for k, _ in wv][::-1],
                         orientation="h", marker_color=GREEN), row=5, col=1)
    # 6. top sources horizontal, tier-colored
    fig.add_trace(go.Bar(x=[c for _, c in top_src][::-1], y=[s for s, _ in top_src][::-1],
                         orientation="h", marker_color=[tier_color(s) for s, _ in top_src][::-1]), row=6, col=1)
    # 7. wayback join-rate over time
    fig.add_trace(go.Scatter(x=as_of, y=hit_rate, mode="lines+markers",
                             line={"color": GREEN, "width": 2}, marker={"size": 5}), row=7, col=1)

    for r in (1, 2, 3, 4):
        fig.update_yaxes(title_text="articles", row=r, col=1)
    fig.update_xaxes(title_text="unique articles", row=5, col=1)
    fig.update_xaxes(title_text="unique articles", row=6, col=1)
    fig.update_yaxes(title_text="join rate", row=7, col=1)
    fig.update_layout(template="seaborn", height=380 * 7, barmode="group", showlegend=False,
                      title={"text": "Portfolio Wave Rider — news retrieval dashboard (GKG + Wayback)",
                             "y": 0.999, "yanchor": "top"},
                      margin={"t": 70, "l": 200}, hovermode="closest")
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    # ---- stat cards ----
    def card(v, label):
        return (f'<div style="display:inline-block;margin:0 1.6em 0.6em 0">'
                f'<b style="font-size:1.5em;color:#0b7285">{v}</b><br>'
                f'<span style="font-size:.8em;color:#555">{label}</span></div>')
    n_full = sum(1 for n in pool_n if n >= 100)
    cards = (card(f"{corpus_total:,}", "articles gathered (full corpus)")
             + card(len(pools), "pools / weekly rebalances")
             + card(f"{n_full}/{len(pools)}", "windows filled to the 100-article cap")
             + card(f"{n_uniq:,}", "unique articles shown to curator")
             + card(f"{avg_hit:.0f}%", "avg Wayback join-rate / window (plot 7)")
             + card("$0", "BigQuery cost (free tier)"))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()

    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>PWR — news retrieval dashboard</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5}h1,h2{color:#111}'
        '.built{position:absolute;top:8px;right:16px;font-size:12px;color:#888}</style></head><body>'
        f'<div class="built">dashboard built {ts}</div>'
        # Minimal cross-page nav: only the README and the sibling Curator DB, since the GKG design
        # has no other published pages yet (mirrors the curator dashboard's nav).
        '<nav style="font-size:14px;color:#555;margin:0 0 1em 0;padding-bottom:0.5em;'
        'border-bottom:1px solid #eee;">'
        '<a href="https://github.com/joehahn/portfolio-wave-rider/blob/main/README.md">README</a>'
        ' · <a href="backtest_gkg_1yr_weekly.html">Curator DB</a>'
        '</nav>'
        '<h1>News retrieval dashboard — GKG + Wayback (upstream of the curator)</h1>'
        '<p style="color:#555">Judges the <b>news gathering</b>, not portfolio gains: completeness of the '
        'historical pull and whether the calendar has gaps, upstream of the curator and free of '
        'PageRank-mooning. The pool the curator actually receives is the <b>ranked top-100</b> '
        '(salience + authority + per-wave weighting) — the shape that resembles live WebSearch results, '
        'not a raw dump. Plot 8 tracks the <b>Wayback join-rate</b> — the share of each window\'s ranked '
        f'articles that got an archived lede. {len(pools)} weekly 21-day windows.</p>'
        f'<p style="color:#555">Raw data (inspect any file): <a href="../{RUN_REL}/"><code>{RUN_REL}/</code></a> — '
        f'per-window <code>&lt;date&gt;-pool.json</code> (ranked article list with a <code>lede</code> '
        f'field = the Wayback join), plus <code>_corpus/</code> (full gathering, one file per day). '
        f'Example: <a href="../{RUN_REL}/2025-05-04-pool.json"><code>2025-05-04-pool.json</code></a>.</p>'
        + f'<div style="margin:1em 0 1.5em">{cards}</div>' + chart_html + '</body></html>'
    )
    OUT.write_text(page)
    print(f"wrote {OUT}")
    print(f"  corpus {corpus_total:,} gathered | {len(pools)} pools | {n_uniq:,} unique shown | "
          f"avg join-rate {avg_hit:.0f}%/window ({n_wb:,} unique w/ lede)")


if __name__ == "__main__":
    build()
