#!/usr/bin/env python3
"""Build docs/retrieval_bootstrap.html — the "Retriever Bootstrap" dashboard: the news-coverage bridge
across the backtest->forward handoff (2026-07-22). Splices the backtest's last ~3 months of top-100 pools
(GKG discovery + Wayback ledes) onto the forward cron's daily WebSearch pulls (data/forward_corpus/).

STANDALONE (a deliberate fork of build_retrieval_dashboard.py, not a call into it) so the bootstrap view can
be customized freely without touching the backtest RBT. It recycles the RBT's plots / cards / param-table /
style, but frames provenance for TWO clean sources instead of one:
  - Wayback   : backtest-tail ledes, archived-at-the-time (look-ahead-safe)
  - WebSearch : forward ledes from the daily cron (look-ahead-safe -- the news IS current)
  - live-fallback: backtest-tail ledes fetched from today's page (look-ahead-BIASED; backtest only)
CLEAN = Wayback + WebSearch (both safe); the biased band is live-fallback only. Reads canon14's last-3mo
pools + the forward corpus directly, so a re-run always reflects the latest cron pulls.
Usage: python scripts/build_bootstrap_dashboard.py [--canon-dir ...] [--forward-corpus ...] [--since ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import dash_nav  # noqa: E402  shared cross-page nav (Backtest | Forwardtest | Bootstrap)
import gkg_pool as g  # noqa: E402  wave/domain-tier classifiers (same as the RBT)
from src import portfolio as _pf  # noqa: E402  load_financial_model -> the param-settings table

BLUE, GREEN, ORANGE, RED, GREY = "#1f77b4", "#2ca02c", "#ff7f0e", "#e03131", "#adb5bd"
GOLD = "#f59e0b"
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BT_SRC, FW_SRC = "gkg-wayback-articles", "forward-websearch"
CLEAN_LS = ("wayback", "websearch")            # look-ahead-safe lede provenances
HANDOFF = "2026-07-22"          # backtest -> forward news handoff (last backtest pool; WebSearch-only ~14d later)


def _iso(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except Exception:
        return None


def load_pools(canon_dir: str, forward_corpus: str, since: str):
    """(backtest_pools, forward_pools) in pool-JSON shape. Backtest = canon14's own top-100 pools with
    as_of >= `since` (Wayback ledes verbatim). Forward = the cron's articles grouped into per-pull-day
    pools tagged with a `websearch` provenance."""
    bt = sorted((json.loads(f.read_text()) for f in (ROOT / canon_dir).glob("*-pool.json")),
                key=lambda p: p["as_of_date"])
    bt = [p for p in bt if p["as_of_date"] >= since]
    byday = defaultdict(list)
    for line in (ROOT / forward_corpus / "articles.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        a = json.loads(line)
        day = (a.get("first_pulled_at") or "")[:10]
        if not day:
            continue
        lede = (a.get("snippet") or a.get("full_text") or "").strip()[:500]
        byday[day].append({"url": a.get("url", ""), "title": a.get("title", ""),
                           "date": (a.get("published_date") or "")[:10], "source": a.get("source_domain", ""),
                           "lede": lede, "lede_source": "websearch" if lede else "none",
                           "author": a.get("author", "") or "", "wave": a.get("first_wave", "")})
    fw = []
    for day, items in sorted(byday.items()):
        src = Counter(x["lede_source"] for x in items)
        fw.append({"as_of_date": day, "source": FW_SRC, "n_articles": len(items),
                   "hit_rate": round(src.get("websearch", 0) / max(len(items), 1), 2),
                   "lede_sources": dict(src), "articles": items})
    return bt, fw


def build(canon_dir: str, forward_corpus: str, since: str, out: Path) -> None:
    bt, fw = load_pools(canon_dir, forward_corpus, since)
    pools = sorted(bt + fw, key=lambda p: p["as_of_date"])

    def _rate(p, keys):
        ls, n = p.get("lede_sources", {}), max(p.get("n_articles", 1), 1)
        return sum(ls.get(k, 0) for k in keys) / n

    # ---- unique articles across all pools (dedupe by url); keep best provenance seen ----
    uniq = {}
    for p in pools:
        side = "web" if p.get("source") == FW_SRC else "gkg"   # gkg = GKG discovery + Wayback; web = WebSearch
        for a in p.get("articles", []):
            url = a.get("url")
            if not url:
                continue
            ls = a.get("lede_source", "none")
            clean, live = ls in CLEAN_LS, ls == "live"
            if url not in uniq:
                uniq[url] = {"date": a.get("date", ""), "source": a.get("source", ""),
                             "title": a.get("title", ""), "url": url, "author": a.get("author", ""),
                             "wave": a.get("wave", ""), "has_clean": clean, "has_live": live, "side": side}
            else:
                uniq[url]["has_clean"] |= clean
                uniq[url]["has_live"] |= live
    articles = list(uniq.values())
    n_uniq = len(articles)
    n_clean = sum(1 for a in articles if a["has_clean"])
    n_live = sum(1 for a in articles if a["has_live"] and not a["has_clean"])
    pct_clean = 100.0 * n_clean / n_uniq if n_uniq else 0.0
    pct_cov = 100.0 * (n_clean + n_live) / n_uniq if n_uniq else 0.0

    # ---- time buckets over unique articles (green overlay = has a CLEAN lede) ----
    mon_g, mon_w, wk_g, wk_w, day_g, day_w, dow_g, dow_w = (Counter() for _ in range(8))
    mon_gkg, mon_web, wk_gkg, wk_web, day_gkg, day_web = (Counter() for _ in range(6))   # plots 1-3, by side
    for a in articles:
        dt = _iso(a["date"])
        if dt is None:
            continue
        hw, mo = a["has_clean"], a["date"][:7]
        wk = (dt - timedelta(days=dt.weekday())).isoformat()
        di, wd = dt.isoformat(), DOW[dt.weekday()]
        _gk = a.get("side", "gkg") == "gkg"
        mon_g[mo] += 1; mon_w[mo] += hw; (mon_gkg if _gk else mon_web)[mo] += 1
        wk_g[wk] += 1; wk_w[wk] += hw; (wk_gkg if _gk else wk_web)[wk] += 1
        day_g[di] += 1; day_w[di] += hw; (day_gkg if _gk else day_web)[di] += 1
        dow_g[wd] += 1; dow_w[wd] += hw
    # Plots 1-3 span the full pool WINDOW (first backtest pool -> last forward pull), not just the dates
    # that happen to carry articles, so empty months/weeks/days show as zeros across the whole bridge.
    _span_lo, _span_hi = _iso(pools[0]["as_of_date"]), _iso(pools[-1]["as_of_date"])
    mo_keys = []
    _m = _span_lo.replace(day=1)
    while _m <= _span_hi:
        mo_keys.append(_m.isoformat()[:7])
        _m = (_m.replace(day=28) + timedelta(days=4)).replace(day=1)   # first of next month
    wk_keys = []
    _w = _span_lo - timedelta(days=_span_lo.weekday())                 # Monday of the start week
    while _w <= _span_hi:
        wk_keys.append(_w.isoformat()); _w += timedelta(days=7)
    day_x, day_yg = [], []
    _d = _span_lo
    while _d <= _span_hi:
        k = _d.isoformat(); day_x.append(k); day_yg.append(day_g.get(k, 0)); _d += timedelta(days=1)

    # ---- per-wave (unique). Forward carries a wave tag; backtest doesn't, so infer via gkg_pool. ----
    wave_c = Counter()
    for a in articles:
        w = a.get("wave") or (g._article_waves(f"{a['title']} {a['url']}") or ["general"])[0]
        wave_c[w or "general"] += 1
    wv = wave_c.most_common()

    # ---- source utilization: recognized desks (incl zero-contributors) + top other, tier-colored ----
    src_c = Counter(a["source"] for a in articles if a["source"])

    def _dom_count(dom):
        return sum(c for s, c in src_c.items() if g._domain_in(s, {dom}))
    rec_rows = [(d, _dom_count(d), GREEN if d in g.PREFERRED_DOMAINS else BLUE)
                for d in sorted(g.PREFERRED_DOMAINS | g.MAJOR_DOMAINS)]
    n_rec_zero = sum(1 for _, c, _ in rec_rows if c == 0)
    grey_rows = sorted(((s, c, GREY) for s, c in src_c.items()
                        if not g._domain_in(s, g.RECOGNIZED_DOMAINS)), key=lambda r: -r[1])[:12]
    src_rows = sorted(rec_rows + grey_rows, key=lambda r: r[1])

    # ---- articles per search keyword (retrieval_config wave_keywords), geopolitical split into subwaves ----
    KW_WAVE = g._KW_WAVE
    GEO_SUB = {"geo-defense": {"hypersonic", "missile", "fighter jet", "warship", "munition", "defense contract"},
               "geo-drones": {"loitering", "counter-drone", "drone swarm"},
               "geo-tankers": {"oil tanker", "crude tanker", "product tanker", "tanker rates", "vlcc",
                               "strait of hormuz", "tanker demand"},
               "geo-reconstruction": {"gaza reconstruction", "syria reconstruction", "lebanon reconstruction",
                                      "iran reconstruction", "middle east reconstruction", "reconstruction contract",
                                      "vision 2030", "megaproject", "engineering and construction"}}
    WAVE_COLOR = {"AI": "#1f77b4", "rockets_spacecraft": "#ff7f0e", "nuclear": "#2ca02c", "quantum": "#9467bd",
                  "robotics": "#8c564b", "geo-defense": "#d62728", "geo-drones": "#e377c2", "geo-tankers": "#17becf",
                  "geo-reconstruction": "#bcbd22", "aging_demographics": "#7f7f7f"}

    def _kw_group(kw, wave):
        return next((s for s, ks in GEO_SUB.items() if kw in ks), "geo-other") if wave == "geopolitical" else wave
    kw_cnt = Counter()
    for a in articles:
        text = f"{a['title']} {a['url']}".lower()
        for kw in KW_WAVE:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                kw_cnt[kw] += 1
    kw_rows = sorted(((f"[{_kw_group(kw, w)}] {kw}", kw_cnt.get(kw, 0), WAVE_COLOR.get(_kw_group(kw, w), GREY))
                      for kw, w in KW_WAVE.items()), key=lambda r: r[1])

    # ---- 8-row figure (mirrors the RBT) ----
    titles = ("1. Unique articles per month", "2. Unique articles per week", "3. Unique articles per day",
              "4. Unique articles by day of week", "5. Unique articles by wave",
              "6. Source utilization", "7. Articles per search keyword")
    fig = make_subplots(rows=7, cols=1, vertical_spacing=0.025, subplot_titles=titles,
                        row_heights=[1, 1, 1, 1, 1, 3.6, 2.4])
    # 1-3. volume over time, STACKED by provenance: GKG discovery + Wayback ledes (backtest, blue) vs
    # WebSearch (forward, gold). Legend shown once (row 1); the gold bars begin at the 2026-07-22 handoff.
    fig.add_trace(go.Bar(x=mo_keys, y=[mon_gkg[m] for m in mo_keys], marker_color=BLUE,
                         name="GKG + Wayback (backtest)", legendgroup="gkg"), row=1, col=1)
    fig.add_trace(go.Bar(x=mo_keys, y=[mon_web[m] for m in mo_keys], marker_color=GOLD,
                         name="WebSearch (forward)", legendgroup="web"), row=1, col=1)
    fig.add_trace(go.Bar(x=wk_keys, y=[wk_gkg[w] for w in wk_keys], marker_color=BLUE,
                         legendgroup="gkg", showlegend=False), row=2, col=1)
    fig.add_trace(go.Bar(x=wk_keys, y=[wk_web[w] for w in wk_keys], marker_color=GOLD,
                         legendgroup="web", showlegend=False), row=2, col=1)
    fig.add_trace(go.Bar(x=day_x, y=[day_gkg[d] for d in day_x], marker_color=BLUE,
                         legendgroup="gkg", showlegend=False), row=3, col=1)
    fig.add_trace(go.Bar(x=day_x, y=[day_web[d] for d in day_x], marker_color=GOLD,
                         legendgroup="web", showlegend=False), row=3, col=1)
    fig.add_trace(go.Bar(x=DOW, y=[dow_g.get(d, 0) for d in DOW], marker_color=BLUE, showlegend=False), row=4, col=1)
    fig.add_trace(go.Bar(x=[v for _, v in wv][::-1], y=[k for k, _ in wv][::-1], orientation="h",
                         marker_color=GREEN, showlegend=False), row=5, col=1)
    fig.add_trace(go.Bar(x=[c if c > 0 else 0.5 for _, c, _ in src_rows], y=[s for s, _, _ in src_rows],
                         orientation="h", marker_color=[col for _, _, col in src_rows], showlegend=False,
                         customdata=[c for _, c, _ in src_rows],
                         hovertemplate="%{y}: %{customdata} articles<extra></extra>"), row=6, col=1)
    fig.add_trace(go.Bar(x=[c if c > 0 else 0.5 for _, c, _ in kw_rows], y=[k for k, _, _ in kw_rows],
                         orientation="h", marker_color=[col for _, _, col in kw_rows], showlegend=False,
                         customdata=[c for _, c, _ in kw_rows],
                         hovertemplate="%{y}: %{customdata} articles<extra></extra>"), row=7, col=1)
    for r in (1, 2, 3, 4):
        fig.update_yaxes(title_text="articles", row=r, col=1)
    fig.update_xaxes(title_text="unique articles", row=5, col=1)
    fig.update_xaxes(title_text="unique articles (log; 0 plotted at 0.5)", type="log", row=6, col=1)
    fig.update_yaxes(dtick=1, tickfont={"size": 9}, row=6, col=1)
    fig.update_xaxes(title_text="unique articles (log; 0 plotted at 0.5)", type="log", row=7, col=1)
    fig.update_yaxes(dtick=1, tickfont={"size": 9}, row=7, col=1)
    fig.update_layout(template="seaborn", height=int(400 * 11.0), barmode="stack", showlegend=True,
                      legend={"orientation": "h", "y": 1.03, "x": 0.5, "xanchor": "center",
                              "yanchor": "bottom", "font": {"size": 12}},
                      margin={"t": 80, "l": 200}, hovermode="closest")
    # dashed vertical line at the backtest->WebSearch handoff on the daily plot (row 3): the news source
    # transitions here (GKG+Wayback before, WebSearch after), converging to WebSearch-only ~14d later.
    # (annotation omitted: plotly's add_vline annotation trips on a category x-axis; the card + the blue->gold
    #  provenance shift already label the handoff. The dashed line marks the exact day.)
    fig.add_vline(x=HANDOFF, row=3, col=1, line={"dash": "dash", "color": "#c92a2a", "width": 1.5})
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False})

    # link the STARTER template: the author's own retrieval_config.json is not tracked, so blob/main/ 404s.
    cfg_link = ('<p style="color:#555;max-width:820px;margin:.2em 0 0;">Plot 7 is driven by the per-wave '
                'search terms in <code>retrieval_config.json</code>; a starter version of that file is at '
                '<a href="https://github.com/joehahn/portfolio-wave-rider/blob/main/examples/'
                'retrieval_config.json"><code>examples/retrieval_config.json</code></a> '
                '(<code>wave_keywords</code>). The forward cron queries the same waves.</p>')

    # ---- 9. articles per author ----
    auth = {a["url"]: a["author"] for a in articles if a.get("author")}
    _src_domains = set(src_c)
    authors = {u: a for u, a in auth.items() if not g.is_source_name(a, _src_domains)}
    _top = Counter(authors.values()).most_common(20)[::-1]
    _afig = go.Figure(go.Bar(x=[c for _, c in _top], y=[a[:48] for a, _ in _top], orientation="h",
                             marker_color=GOLD))
    _afig.update_layout(template="seaborn", height=560, margin={"t": 10, "l": 250, "r": 20, "b": 40},
                        xaxis_title="unique articles", showlegend=False)
    author_html = ('<h2 style="margin:1.8em 0 0.2em;">8. Articles per author</h2>'
                   f'<p style="color:#555;max-width:820px;"><b>{100*len(authors)//max(n_uniq,1)}%</b> of the '
                   f'{n_uniq:,} unique articles ({len(authors):,}) carry a byline (native capture on both sides: '
                   f'Wayback/live extraction on the backtest tail, the WebSearch pull on the forward side).</p>'
                   + _afig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}))

    # ---- cards ----
    def card(v, label):
        return (f'<div style="display:inline-block;margin:0 1.6em 0.6em 0">'
                f'<b style="font-size:1.5em;color:#0b7285">{v}</b><br>'
                f'<span style="font-size:.8em;color:#555">{label}</span></div>')
    cards = (card(HANDOFF, "backtest → WebSearch handoff (news source shift)")
             + card(f"{n_uniq:,}", "unique articles (deduped)")
             + card(f"{len(bt)} + {len(fw)}", "backtest + forward pools")
             + card(f"{pools[0]['as_of_date']} → {pools[-1]['as_of_date']}", "coverage span")
             + card(f"{pct_clean:.0f}%", "CLEAN lede: Wayback + WebSearch (look-ahead-safe)")
             + card(f"{pct_cov:.0f}%", "clean + live-fallback coverage")
             + card(f"{100*len(authors)//max(n_uniq,1)}%", "unique articles with a byline (plot 8)")
             + card(f"{n_rec_zero}/{len(rec_rows)}", "configured desks surfaced 0x (plot 6)"))

    # ---- parameter table (RETRIEVAL knobs, mirrors the RBT) ----
    _fm = _pf.load_financial_model(str(ROOT / "investor_profile.md"))
    _bt = _pf.load_backtest_config(str(ROOT / "investor_profile.md"))

    def _prow(label, value):
        return (f"<tr><td style='padding:5px 14px 5px 0;color:#555;white-space:nowrap;'>{label}</td>"
                f"<td style='padding:5px 0;font-weight:600;'>{value}</td></tr>")
    _params = ('<h2 style="margin:1.6em 0 0.3em;">Parameter settings</h2>'
               '<p style="color:#555;max-width:820px;margin:0 0 0.6em;font-size:13px;">The user-set '
               '<code>investor_profile.md</code> knobs relevant to news retrieval.</p>'
               "<table style='border-collapse:collapse;font-size:14px;margin-bottom:1.2em;'><tbody>"
               + _prow("Backtest-tail cadence", f"{_fm['rebalance_period']} (top-100 pools, Wayback ledes)")
               + _prow("Forward cadence", "daily cron (WebSearch pulls)")
               + _prow("News lookback (backtest)", f"{int(_fm['news_lookback_days'])} days")
               + _prow("Max articles / backtest pool", f"{int(_bt['max_articles'])}")
               + "</tbody></table>")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    span = f'{pools[0]["as_of_date"]} to {pools[-1]["as_of_date"]}'
    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Retriever Bootstrap (RBS)</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'max-width:1180px;margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5}h1,h2{color:#111}'
        '.built{position:absolute;top:8px;right:16px;font-size:12px;color:#888}</style></head><body>'
        f'<div class="built">dashboard built {ts}</div>'
        + dash_nav.render("retrieval_bootstrap.html", built=False) +
        f'<h1>Retriever Bootstrap (RBS)</h1>'
        f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{span}</p>'
        f'<p style="color:#666;margin:-.4em 0 .7em;font-size:14px;">{len(bt)} backtest-tail pools (biweekly) '
        f'+ {len(fw)} forward cron pulls (daily)</p>'
        '<p style="color:#555">The news-coverage <b>bridge</b> across the backtest&rarr;forward handoff '
        '(2026-07-22): the backtest&#39;s last ~3 months of ranked <b>top-100</b> pools (GKG discovery + '
        'look-ahead-clean <b>Wayback</b> ledes) spliced onto the forward cron&#39;s daily <b>WebSearch</b> '
        'pulls. Both Wayback and WebSearch ledes are look-ahead-safe (WebSearch because the forward news is '
        'genuinely current); the backtest-only <b>live-fallback</b> is the single look-ahead-biased provenance.</p>'
        f'<div style="margin:1em 0 1.5em">{cards}</div>' + _params + chart_html + cfg_link + author_html
        + '</body></html>')
    out.write_text(page)
    print(f"wrote {out}  ({n_uniq} unique | {len(bt)} backtest + {len(fw)} forward pools | "
          f"clean {pct_clean:.0f}% + live {100.0*n_live/max(n_uniq,1):.0f}% = {pct_cov:.0f}% covered)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the PWR Retriever Bootstrap dashboard.")
    ap.add_argument("--canon-dir", default="data/curator_runs/gkg-3yr-geosplit")   # current-thesis backtest pools
    ap.add_argument("--forward-corpus", default="data/forward_corpus")
    ap.add_argument("--since", default="2026-04-22", help="include backtest pools with as_of >= this date")
    ap.add_argument("--out", default=str(ROOT / "docs" / "retrieval_bootstrap.html"))
    a = ap.parse_args()
    build(a.canon_dir, a.forward_corpus, a.since, Path(a.out))
