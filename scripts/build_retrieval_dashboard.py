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
import dash_nav  # shared cross-page nav (Forward | Backtest groups)  # noqa: E402
import gkg_pool as g  # noqa: E402  (wave/domain-tier classifiers; needs the repo's scripts/ on path)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import portfolio as _pf  # noqa: E402  (load_financial_model -> the param-settings table)
DEFAULT_RUN_REL = "data/curator_runs/gkg-3yr-geosplit"   # canonical retriever pools (geosplit, Wayback-filled)

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

    # ---- per-POOL (biweekly) lede-source COUNTS for the composition plot: one row per rebalance pool,
    # from its lede_sources dict, at the finest granularity so a single Wayback-miss pool shows as its own dip. ----
    _bw = sorted(((p.get("as_of_date", ""), p.get("lede_sources", {}), max(p.get("n_articles", 1), 1))
                  for p in pools if p.get("as_of_date")), key=lambda r: r[0])

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

    # ---- articles per SEARCH KEYWORD (plot 8): the retriever's surfacing mechanism is retrieval_config.json's
    # wave_keywords, so each keyword IS a query term. Geopolitical is split into its four profile subwaves
    # for display (defense, drones, tankers, reconstruction). All profile waves/subwaves now carry keywords
    # (the tanker/reconstruction/aging gaps were filled 2026-07-24); rows for the newly-added terms read low
    # until the next GKG re-ingest, since the current corpus predates them. ----
    KW_WAVE = g._KW_WAVE                                  # {keyword: wave}
    GEO_SUB = {"geo-defense": {"hypersonic", "missile", "fighter jet", "warship", "munition", "defense contract"},
               "geo-drones": {"loitering", "counter-drone", "drone swarm"},
               "geo-tankers": {"oil tanker", "crude tanker", "product tanker", "tanker rates", "vlcc",
                               "strait of hormuz", "tanker demand"},
               "geo-reconstruction": {"gaza reconstruction", "syria reconstruction", "lebanon reconstruction",
                                      "iran reconstruction", "middle east reconstruction", "reconstruction contract",
                                      "vision 2030", "megaproject", "engineering and construction"}}
    WAVE_COLOR = {"AI": "#1f77b4", "rockets_spacecraft": "#ff7f0e", "nuclear": "#2ca02c",
                  "quantum": "#9467bd", "robotics": "#8c564b", "geo-defense": "#d62728", "geo-drones": "#e377c2",
                  "geo-tankers": "#17becf", "geo-reconstruction": "#bcbd22", "aging_demographics": "#7f7f7f"}

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
    # Every configured keyword is a row (all four geopolitical subwaves + aging now have keywords, so the
    # old hard-coded "NO KEYWORDS" red rows are gone). Counts reflect the CURRENT corpus, which predates
    # the tanker/reconstruction/aging keywords, so those rows read low until the next GKG re-ingest.
    kw_rows = [(f"[{_kw_group(kw, wave)}] {kw}", kw_cnt.get(kw, 0), WAVE_COLOR.get(_kw_group(kw, wave), GREY))
               for kw, wave in KW_WAVE.items()]
    kw_rows.sort(key=lambda r: r[1])                     # ascending -> highest at top (h-bars)
    n_kw_zero = sum(1 for _, c, _ in kw_rows if c == 0)

    # ---- figure (7 rows) ---- (pool-size-per-window plot dropped: it was flat at the 100-article
    # cap for all 53 windows; that one fact is now a stat card instead.)
    titles = (   # figA subplot titles (plots 1-6); composition (7) + source/keyword (8-9) are their own figures
        "1. Unique articles per month",
        "2. Unique articles per week",
        "3. Unique articles per day",
        "4. Unique articles by day of week",
        "5. Unique articles by wave",
    )
    # Two subplot figures so the lede-source COMPOSITION (plot 6, a separate figure with its own legend)
    # can sit right after the article-count plots. figA = plots 1-5; figB = plots 7-8 (source + keyword).
    figA = make_subplots(rows=5, cols=1, vertical_spacing=0.05, subplot_titles=titles[:5],
                         row_heights=[1, 1, 1, 1, 1])
    figA.add_trace(go.Bar(x=mo_keys, y=[mon_g[m] for m in mo_keys], marker_color=BLUE, name="GKG"), row=1, col=1)
    figA.add_trace(go.Bar(x=wk_keys, y=[wk_g[w] for w in wk_keys], marker_color=BLUE, name="GKG"), row=2, col=1)
    figA.add_trace(go.Scatter(x=day_x, y=day_yg, mode="lines", line={"color": BLUE, "width": 1}, name="GKG"), row=3, col=1)
    figA.add_trace(go.Bar(x=DOW, y=[dow_g.get(d, 0) for d in DOW], marker_color=BLUE, name="GKG"), row=4, col=1)
    figA.add_trace(go.Bar(x=[v for _, v in wv][::-1], y=[k for k, _ in wv][::-1],
                          orientation="h", marker_color=GREEN), row=5, col=1)
    for r in (1, 2, 3, 4):
        figA.update_yaxes(title_text="articles", row=r, col=1)
    figA.update_xaxes(title_text="unique articles", row=5, col=1)
    figA.update_layout(template="seaborn", height=1300, barmode="group", showlegend=False,
                       margin={"t": 40, "l": 95}, hovermode="closest")
    chartA_html = figA.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    # figB = plots 8-9 (source utilization + keywords): many rows each, log x, 0 -> 0.5, wide left margin.
    figB = make_subplots(rows=2, cols=1, vertical_spacing=0.06,
                         subplot_titles=("7. Source utilization", "8. Articles per search keyword"),
                         row_heights=[3.6, 2.4])
    figB.add_trace(go.Bar(x=[c if c > 0 else 0.5 for _, c, _ in src_rows], y=[s for s, _, _ in src_rows],
                          orientation="h", marker_color=[col for _, _, col in src_rows],
                          customdata=[c for _, c, _ in src_rows],
                          hovertemplate="%{y}: %{customdata} articles<extra></extra>"), row=1, col=1)
    figB.add_trace(go.Bar(x=[c if c > 0 else 0.5 for _, c, _ in kw_rows], y=[k for k, _, _ in kw_rows],
                          orientation="h", marker_color=[col for _, _, col in kw_rows],
                          customdata=[c for _, c, _ in kw_rows],
                          hovertemplate="%{y}: %{customdata} articles<extra></extra>"), row=2, col=1)
    figB.update_xaxes(title_text="unique articles (log; 0 plotted at 0.5)", type="log", row=1, col=1)
    figB.update_yaxes(dtick=1, tickfont={"size": 9}, row=1, col=1)   # force EVERY source label
    figB.update_xaxes(title_text="unique articles (log; 0 plotted at 0.5)", type="log", row=2, col=1)
    figB.update_yaxes(dtick=1, tickfont={"size": 9}, row=2, col=1)   # force EVERY keyword label
    figB.update_layout(template="seaborn", height=int(400 * 6.5), barmode="group", showlegend=False,
                       margin={"t": 40, "l": 200}, hovermode="closest")
    chartB_html = figB.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})

    # ---- separate figure: lede-source COMPOSITION per pool (COUNTS, stacked): how many of each rebalance
    # pool's articles carried a clean Wayback lede vs a biased live-fallback lede vs title-only (no lede). A
    # standalone figure (not a subplot) so it can carry its own legend. ----
    comp_x = [d for d, _, _ in _bw]
    comp_wb = [ls.get("wayback", 0) for _, ls, _ in _bw]
    comp_lv = [ls.get("live", 0) for _, ls, _ in _bw]
    comp_ti = [max(n - ls.get("wayback", 0) - ls.get("live", 0), 0) for _, ls, n in _bw]
    _cfig = go.Figure()
    _cfig.add_trace(go.Bar(x=comp_x, y=comp_wb, name="clean Wayback", marker_color=GREEN))
    _cfig.add_trace(go.Bar(x=comp_x, y=comp_lv, name="biased live", marker_color=ORANGE))
    _cfig.add_trace(go.Bar(x=comp_x, y=comp_ti, name="titles-only", marker_color=GREY))
    _cfig.update_layout(template="seaborn", height=380, barmode="stack",
                        margin={"t": 10, "l": 60, "r": 20, "b": 40}, yaxis_title="articles per pool",
                        legend={"orientation": "h", "y": 1.12, "x": 0})
    comp_html = ('<h2 style="margin:1.8em 0 0.2em;">6. Lede source composition</h2>'
                 '<p style="color:#555;max-width:820px;margin:0 0 .4em;">How many of each biweekly pool&#39;s '
                 'articles the curator read with a clean, look-ahead-safe <b>Wayback</b> lede, a look-ahead-biased '
                 '<b>live-fallback</b> lede, or <b>title only</b> (no lede). The clean-Wayback share is what a '
                 'look-ahead-honest backtest relies on.</p>'
                 + _cfig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))

    # Link to the full keyword config just below the keyword plot (plot 8), so a reader can see the
    # RELATIVE link (docs/ -> repo root), deliberately not an absolute github.com blob URL: the config is
    # gitignored, so it exists only in a working tree. Relative means the link resolves whenever the page is
    # opened from a checkout that has the file -- the author's, or anyone who ran `cp examples/* .`. It 404s
    # on the published Pages copy, which is the accepted tradeoff for keeping the terms out of the repo.
    cfg_link = ('<p style="color:#555;max-width:820px;margin:.2em 0 0;">The full per-wave search-term '
                'lists behind plot 8 live in <a href="../retrieval_config.json"><code>retrieval_config.json'
                '</code></a> (<code>wave_keywords</code>), which is gitignored, so this link resolves only '
                'when you are viewing a local copy of the dashboard.</p>')

    # ---- 9. Articles per author (bylines from run_rel/_authors.json) ----
    # NATIVE capture attempts a byline for EVERY pooled article (build_article_pool stores a["author"]) -> a
    # pool-wide rate is meaningful. An OLD run built before that only has _authors.json backfilled for the
    # curator's CITED evidence URLs -> report a raw count, not a misleading "% of all pooled articles".
    author_html = ""
    _authored_n = 0
    _native_authors = any(
        isinstance(x, dict) and "author" in x
        for _pf in (ROOT / run_rel).glob("*-pool.json")
        for x in json.loads(_pf.read_text()).get("articles", []))   # pool JSON is a dict; iterate its articles
    _af_path = ROOT / run_rel / "_authors.json"
    if _af_path.exists():
        _authors_raw = json.loads(_af_path.read_text())   # {url: byline}
        # Drop source/wire/site-brand names masquerading as authors (Reuters, Breaking Defense,
        # Market BusinessInsider, ...) via the shared gkg_pool filter, keyed on the pool's own source
        # domains so new brands are caught without hand-listing them.
        _src_domains = set(src_c)
        _authors = {u: a for u, a in _authors_raw.items() if not g.is_source_name(a, _src_domains)}
        _authored_n = len(_authors)
        _top = Counter(_authors.values()).most_common(20)[::-1]   # ascending -> largest bar on top
        _afig = go.Figure(go.Bar(x=[c for _, c in _top], y=[a[:48] for a, _ in _top],
                                 orientation="h", marker_color="#f59e0b"))
        _afig.update_layout(template="seaborn", height=560, margin={"t": 10, "l": 250, "r": 20, "b": 40},
                            xaxis_title="unique articles", showlegend=False)
        _cap = (f'<b>{100 * _authored_n // max(n_uniq, 1)}%</b> of the {n_uniq:,} pooled articles '
                f'({_authored_n:,}) have a known author.' if _native_authors else
                f'This backtest predates native author capture, so bylines were only backfilled for the '
                f'curator&#39;s cited evidence: <b>{_authored_n:,} known authors</b> (not the full pool). '
                f'Re-run the backtest to capture bylines pool-wide.')
        author_html = (
            '<h2 style="margin:1.8em 0 0.2em;">9. Articles per author</h2>'
            f'<p style="color:#555;max-width:820px;">{_cap}</p>'
            + _afig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))

    # ---- stat cards ----
    def card(v, label):
        return (f'<div style="display:inline-block;margin:0 1.6em 0.6em 0">'
                f'<b style="font-size:1.5em;color:#0b7285">{v}</b><br>'
                f'<span style="font-size:.8em;color:#555">{label}</span></div>')
    n_full = sum(1 for n in pool_n if n >= 100)
    cards = (card(f"{corpus_total:,}", "articles gathered (full corpus)")
             + card(len(pools), "pools / rebalances")
             + card(f"{n_uniq:,}", "unique articles shown to curator")
             + (card(f"{100*_authored_n//max(n_uniq,1)}%", "pooled articles with a byline (plot 9)")
                if _native_authors else
                card(f"{_authored_n:,}", "cited-evidence authors (plot 9; not pool-wide)"))
             + card(f"{avg_clean:.0f}%", "avg CLEAN Wayback lede / window (plot 6, green)")
             + card(f"{avg_total:.0f}%", "avg CLEAN+LIVE coverage / window (plot 6, orange)")
             + card(f"{n_rec_zero}/{len(rec_rows)}", "configured desks GKG surfaced 0x (plot 7)")
             + card("$0", "BigQuery cost (free tier)"))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    # Title date range: read the same snapshots.csv the Curator Backtest uses, so all three DBs show an
    # identical "start to end". Fall back to the pool as_of span if the replay output isn't present.
    _snap = ROOT / run_rel / "_backtest" / "snapshots.csv"
    if _snap.exists():
        _sd = sorted({ln.split(",", 1)[0] for ln in _snap.read_text().splitlines()[1:] if ln})
        _start, _end = (_sd[0], _sd[-1]) if _sd else (as_of[0], as_of[-1])
    else:
        _start, _end = as_of[0], as_of[-1]
    # Parameter settings: only the user-set investor_profile.md knobs relevant to RETRIEVAL (mirrors the
    # Curator Backtest's table; the optimizer knobs live there + in the Sweeps DB).
    _fm = _pf.load_financial_model(str(ROOT / "investor_profile.md"))
    _bt = _pf.load_backtest_config(str(ROOT / "investor_profile.md"))   # max_articles lives here now
    def _prow(label, value):   # one parameter per row, matching the Curator Backtest's table
        return (f"<tr><td style='padding:5px 14px 5px 0;color:#555;white-space:nowrap;'>{label}</td>"
                f"<td style='padding:5px 0;font-weight:600;'>{value}</td></tr>")
    _params = (
        '<h2 style="margin:1.6em 0 0.3em;">Parameter settings</h2>'
        '<p style="color:#555;max-width:820px;margin:0 0 0.6em;font-size:13px;">The user-set '
        '<code>investor_profile.md</code> knobs relevant to news retrieval (the optimizer knobs are in the '
        'Curator Backtest + Sweeps DBs).</p>'
        "<table style='border-collapse:collapse;font-size:14px;margin-bottom:1.2em;'><tbody>"
        + _prow("Backtest window", f"{_start} &rarr; {_end}")
        + _prow("Rebalance cadence", f"{_fm['rebalance_period']}")
        + _prow("News lookback", f"{int(_fm['news_lookback_days'])} days")
        + _prow("Max articles / pool", f"{int(_bt['max_articles'])}")
        + "</tbody></table>")

    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Retriever Backtest (RBT)</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5}h1,h2{color:#111}'
        '.built{position:absolute;top:8px;right:16px;font-size:12px;color:#888}</style></head><body>'
        f'<div class="built">dashboard built {ts}</div>'
        + dash_nav.render("retrieval_pwr.html", built=False) +
        f'<h1>Retriever Backtest (RBT)</h1>'
        f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{_start} to {_end}</p>'
        f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{len(pools)} rebalances, each reading '
        f'a trailing 21-day news window</p>'
        '<p style="color:#555">Judges the <b>news gathering</b>, not portfolio gains: completeness of the '
        'historical pull and whether the calendar has gaps, upstream of the curator and free of '
        'PageRank-mooning. The pool the curator actually receives is the <b>ranked top-100</b> '
        '(salience + authority + per-wave weighting) — the shape that resembles live WebSearch results, '
        'not a raw dump. Each article carries a <b>lede</b>: first a look-ahead-<b>clean</b> Wayback '
        'snapshot (≤ as_of); where Wayback has no capture, a <b>live-fallback</b> fetches today’s '
        'page (LOOK-AHEAD-BIASED, tagged separately). Plot 6 tracks both: the clean join-rate and the '
        f'clean+live coverage. {len(pools)} rebalances over 21-day windows.</p>'
        f'<p style="color:#555">Raw data (inspect any file): <a href="../{run_rel}/"><code>{run_rel}/</code></a> — '
        f'per-window <code>&lt;date&gt;-pool.json</code> (ranked article list; <code>lede</code> = clean '
        f'Wayback join, <code>lede_live</code> = biased live-fallback, <code>lede_sources</code> = the '
        f'clean/live/none split), plus <code>_corpus/</code> (full gathering, one file per day).</p>'
        + f'<div style="margin:1em 0 1.5em">{cards}</div>' + _params + chartA_html + comp_html + chartB_html + cfg_link + author_html + '</body></html>'
    )
    out.write_text(dash_nav.stamp(page))
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
