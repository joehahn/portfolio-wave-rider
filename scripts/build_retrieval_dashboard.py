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
import re
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
DEFAULT_RUN_REL = "data/curator_runs/gkg-2yr-weekly"   # data folder (relative to repo root)

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


def build(run_rel, out):
    POOLS = sorted(glob.glob(str(ROOT / run_rel / "*-pool.json")))
    CORPUS = sorted(glob.glob(str(ROOT / run_rel / "_corpus" / "gkg-*.json")))

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
    # Two per-window lede rates: CLEAN (Wayback, look-ahead-safe = hit_rate) and TOTAL coverage
    # (clean + live-fallback). The gap between them is the LOOK-AHEAD-BIASED live-fallback contribution.
    def _rate(p, keys):
        ls, n = p.get("lede_sources", {}), max(p.get("n_articles", 1), 1)
        return sum(ls.get(k, 0) for k in keys) / n
    clean_rate = [p.get("hit_rate", _rate(p, ("wayback",))) for p in pools]
    total_rate = [_rate(p, ("wayback", "live")) for p in pools]

    # ---- unique articles across all pools, deduped by url ----
    # Each unique article keeps its date/source/title/url and a lede_state: "clean" (Wayback lede, ANY
    # appearance), else "live" (live-fallback lede, ANY appearance), else "none". Clean wins over live.
    uniq = {}
    for p in pools:
        for a in p.get("articles", []):
            url = a.get("url")
            if not url:
                continue
            clean, live = bool(a.get("lede")), bool(a.get("lede_live"))
            if url not in uniq:
                uniq[url] = {"date": a.get("date", ""), "source": a.get("source", ""),
                             "title": a.get("title", ""), "url": url,
                             "has_clean": clean, "has_live": live}
            else:
                uniq[url]["has_clean"] |= clean
                uniq[url]["has_live"] |= live
    articles = list(uniq.values())
    for a in articles:
        a["has_wayback"] = a["has_clean"]           # back-compat alias for the per-bucket overlays below
    n_uniq = len(articles)
    n_clean = sum(1 for a in articles if a["has_clean"])
    n_live = sum(1 for a in articles if a["has_live"] and not a["has_clean"])
    n_none = n_uniq - n_clean - n_live
    pct_clean = (100.0 * n_clean / n_uniq) if n_uniq else 0.0
    pct_cov = (100.0 * (n_clean + n_live) / n_uniq) if n_uniq else 0.0
    # Average per-window rates (mean of the pools' rates); differ from the unique %s because an article
    # recurs across overlapping windows and counts once as unique if ANY appearance got a lede.
    avg_clean = 100.0 * sum(clean_rate) / len(clean_rate) if clean_rate else 0.0
    avg_total = 100.0 * sum(total_rate) / len(total_rate) if total_rate else 0.0

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

    # ---- monthly lede coverage (plot 6): aggregate each month's pools' clean/live/n ----
    mon_led = {}   # 'YYYY-MM' -> [wayback, live, n]
    for p in pools:
        mo = p.get("as_of_date", "")[:7]
        if not mo:
            continue
        ls = p.get("lede_sources", {})
        acc = mon_led.setdefault(mo, [0, 0, 0])
        acc[0] += ls.get("wayback", 0); acc[1] += ls.get("live", 0); acc[2] += p.get("n_articles", 0)
    led_months = sorted(mon_led)
    clean_rate_m = [mon_led[m][0] / max(mon_led[m][2], 1) for m in led_months]
    total_rate_m = [(mon_led[m][0] + mon_led[m][1]) / max(mon_led[m][2], 1) for m in led_months]

    # ---- per-wave (unique articles; first wave from title+url, else "general") ----
    wave_c = Counter()
    for a in articles:
        waves = g._article_waves(f"{a['title']} {a['url']}")
        wave_c[waves[0] if waves else "general"] += 1
    wv = wave_c.most_common()

    # ---- source utilization (unique articles): EVERY configured recognized desk, incl. the ones
    # that contributed ZERO articles (GKG under-indexes many paywalled/specialty domains), plus the
    # top non-configured contributors for context. Colored by authority tier. ----
    src_c = Counter(a["source"] for a in articles if a["source"])
    n_src_total = len(src_c)

    def _dom_count(dom):                              # articles from a configured domain + its subdomains
        return sum(c for s, c in src_c.items() if g._domain_in(s, {dom}))
    rec_rows = [(d, _dom_count(d), GREEN if d in g.PREFERRED_DOMAINS else BLUE)
                for d in sorted(g.PREFERRED_DOMAINS | g.MAJOR_DOMAINS)]
    n_rec_zero = sum(1 for _, c, _ in rec_rows if c == 0)
    N_GREY = 12
    grey_rows = sorted(((s, c, GREY) for s, c in src_c.items()
                        if not g._domain_in(s, g.RECOGNIZED_DOMAINS)), key=lambda r: -r[1])[:N_GREY]
    src_rows = sorted(rec_rows + grey_rows, key=lambda r: r[1])   # ascending -> highest at top (h-bars)

    # ---- articles per SEARCH KEYWORD (plot 8): the retriever's surfacing mechanism is gkg_config.json's
    # wave_keywords, so each keyword IS a query term. Geopolitical is split into the profile's subwaves
    # for display; profile waves/subwaves with NO keyword are appended at 0 (red) so the coverage GAPS
    # are explicit (aging-population is a whole missing wave; geo tankers + reconstruction are missing
    # subwaves — documented, left unfixed per user choice 2026-07-20). ----
    KW_WAVE = g._KW_WAVE                                  # {keyword: wave}
    GEO_SUB = {"geo-defense": {"hypersonic", "missile", "fighter jet", "warship", "munition", "defense contract"},
               "geo-drones": {"loitering", "counter-drone", "drone swarm"}}
    WAVE_COLOR = {"AI": "#1f77b4", "rockets_spacecraft": "#ff7f0e", "nuclear": "#2ca02c",
                  "quantum": "#9467bd", "robotics": "#8c564b", "geo-defense": "#d62728", "geo-drones": "#e377c2"}

    def _kw_group(kw, wave):
        if wave == "geopolitical":
            return next((s for s, ks in GEO_SUB.items() if kw in ks), "geo-other")
        return wave
    kw_cnt = Counter()
    for a in articles:
        text = f"{a['title']} {a['url']}".lower()
        for kw in KW_WAVE:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                kw_cnt[kw] += 1
    kw_rows = [(f"[{_kw_group(kw, wave)}] {kw}", kw_cnt.get(kw, 0), WAVE_COLOR.get(_kw_group(kw, wave), GREY))
               for kw, wave in KW_WAVE.items()]
    kw_rows += [("[geo-tankers] — NO KEYWORDS (uncovered subwave)", 0, RED),
                ("[geo-reconstruction] — NO KEYWORDS (uncovered subwave)", 0, RED),
                ("[aging-population] — NO KEYWORDS (whole wave uncovered)", 0, RED)]
    kw_rows.sort(key=lambda r: r[1])                     # ascending -> highest at top (h-bars)
    n_kw_zero = sum(1 for _, c, _ in kw_rows if c == 0)

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
        "6. Lede coverage by MONTH — clean vs clean+live<br><sub><i>monthly lede yield: GREEN = "
        "clean Wayback join (look-ahead-safe), ORANGE = clean + live-fallback (the gap = the "
        "look-ahead-BIASED live ledes that fill Wayback-misses)</i></sub>",
        f"7. Source utilization — every configured desk (incl. {n_rec_zero} that GKG surfaced ZERO "
        f"times) + top {N_GREY} others<br><sub><i>green = specialty (2.0), blue = major wire (1.5), "
        "grey = other (1.0); zero-length bars = configured desks GKG never indexed (e.g. paywalled "
        f"wires). {n_src_total} distinct sources appeared overall.</i></sub>",
        "8. Articles per SEARCH KEYWORD — the retriever's surfacing terms (gkg_config.json)<br><sub><i>"
        "each keyword is a query term; colored by wave, geopolitical split into the profile's subwaves. "
        f"RED = profile waves/subwaves with NO keyword ({n_kw_zero} zero-yield rows incl. aging-population, "
        "geo-tankers, geo-reconstruction — coverage gaps)</i></sub>",
    )
    # Plots 7 & 8 list many rows, so give those rows much more vertical room than the others.
    fig = make_subplots(rows=8, cols=1, vertical_spacing=0.025, subplot_titles=titles,
                        row_heights=[1, 1, 1, 1, 1, 1, 3.6, 2.4])

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
    # 6. lede coverage BY MONTH: clean (Wayback) and clean+live (total). The band between them is the
    # look-ahead-biased live-fallback contribution. Aggregated monthly (each month's pools pooled).
    fig.add_trace(go.Scatter(x=led_months, y=total_rate_m, mode="lines+markers", name="clean + live",
                             line={"color": ORANGE, "width": 2}, marker={"size": 6}), row=6, col=1)
    fig.add_trace(go.Scatter(x=led_months, y=clean_rate_m, mode="lines+markers", name="clean (Wayback)",
                             line={"color": GREEN, "width": 2}, marker={"size": 6}), row=6, col=1)
    # 7. source utilization horizontal, tier-colored (all recognized incl zeros + top other).
    # Log x-axis; a zero-contributor is plotted at 0.5 so it shows a tiny bar (log 0 is undefined).
    # The hover keeps the TRUE count so the 0.5 substitution isn't misleading.
    fig.add_trace(go.Bar(x=[c if c > 0 else 0.5 for _, c, _ in src_rows], y=[s for s, _, _ in src_rows],
                         orientation="h", marker_color=[col for _, _, col in src_rows],
                         customdata=[c for _, c, _ in src_rows],
                         hovertemplate="%{y}: %{customdata} articles<extra></extra>"), row=7, col=1)
    # 8. articles per SEARCH KEYWORD, wave-colored (geopolitical split into subwaves); log x, 0 -> 0.5.
    fig.add_trace(go.Bar(x=[c if c > 0 else 0.5 for _, c, _ in kw_rows], y=[k for k, _, _ in kw_rows],
                         orientation="h", marker_color=[col for _, _, col in kw_rows],
                         customdata=[c for _, c, _ in kw_rows],
                         hovertemplate="%{y}: %{customdata} articles<extra></extra>"), row=8, col=1)

    for r in (1, 2, 3, 4):
        fig.update_yaxes(title_text="articles", row=r, col=1)
    fig.update_xaxes(title_text="unique articles", row=5, col=1)
    fig.update_yaxes(title_text="lede rate (clean vs +live)", row=6, col=1)
    fig.update_xaxes(title_text="unique articles (log; 0 plotted at 0.5)", type="log", row=7, col=1)
    fig.update_yaxes(dtick=1, tickfont={"size": 9}, row=7, col=1)   # force EVERY source label (no every-other skip)
    fig.update_xaxes(title_text="unique articles (log; 0 plotted at 0.5)", type="log", row=8, col=1)
    fig.update_yaxes(dtick=1, tickfont={"size": 9}, row=8, col=1)   # force EVERY keyword label
    fig.update_layout(template="seaborn", height=int(340 * 12.0), barmode="group", showlegend=False,
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
             + card(f"{n_uniq:,}", "unique articles shown to curator")
             + card(f"{avg_clean:.0f}%", "avg CLEAN Wayback lede / window (plot 6, green)")
             + card(f"{avg_total:.0f}%", "avg CLEAN+LIVE coverage / window (plot 6, orange)")
             + card(f"{n_rec_zero}/{len(rec_rows)}", "configured desks GKG surfaced 0x (plot 7)")
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
        ' · <a href="pool_browser.html">Pool browser</a>'
        ' · <a href="backtest_gkg_2yr_weekly.html">Curator DB</a>'
        '</nav>'
        '<h1>News retrieval dashboard — GKG + Wayback (upstream of the curator)</h1>'
        '<p style="color:#555">Judges the <b>news gathering</b>, not portfolio gains: completeness of the '
        'historical pull and whether the calendar has gaps, upstream of the curator and free of '
        'PageRank-mooning. The pool the curator actually receives is the <b>ranked top-100</b> '
        '(salience + authority + per-wave weighting) — the shape that resembles live WebSearch results, '
        'not a raw dump. Each article carries a <b>lede</b>: first a look-ahead-<b>clean</b> Wayback '
        'snapshot (≤ as_of); where Wayback has no capture, a <b>live-fallback</b> fetches today’s '
        'page (LOOK-AHEAD-BIASED, tagged separately). Plot 6 tracks both: the clean join-rate and the '
        f'clean+live coverage. {len(pools)} weekly 21-day windows.</p>'
        f'<p style="color:#555">Raw data (inspect any file): <a href="../{run_rel}/"><code>{run_rel}/</code></a> — '
        f'per-window <code>&lt;date&gt;-pool.json</code> (ranked article list; <code>lede</code> = clean '
        f'Wayback join, <code>lede_live</code> = biased live-fallback, <code>lede_sources</code> = the '
        f'clean/live/none split), plus <code>_corpus/</code> (full gathering, one file per day).</p>'
        + f'<div style="margin:1em 0 1.5em">{cards}</div>' + chart_html + '</body></html>'
    )
    out.write_text(page)
    print(f"wrote {out}")
    print(f"  corpus {corpus_total:,} gathered | {len(pools)} pools | {n_uniq:,} unique shown | "
          f"clean {pct_clean:.0f}% + live {100.0*n_live/max(n_uniq,1):.0f}% = {pct_cov:.0f}% covered "
          f"(per-window avg: clean {avg_clean:.0f}% / total {avg_total:.0f}%)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build the PWR news-retrieval dashboard.")
    ap.add_argument("--run-dir", default=DEFAULT_RUN_REL, help="pool run dir (relative to repo root)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "retrieval_pwr.html"))
    args = ap.parse_args()
    build(args.run_dir, Path(args.out))
