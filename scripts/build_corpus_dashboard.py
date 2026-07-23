"""Forward news-corpus dashboard -> docs/corpus_pwr.html.

The live analog of the backtest's retrieval DB (retrieval_pwr.html): it monitors the frozen forward
corpus (data/forward_corpus/) that news_pull.sh fills each night. The day-one-useful panel is PULL
HISTORY + GAPS (did the cron actually fire every day? a missed pull can't be cleanly backfilled). The
coverage/quality panels grow more informative as the corpus accumulates.

Usage: python scripts/build_corpus_dashboard.py   (rebuild via cron later; wired by hand for now)
"""
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go

import dash_nav

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "forward_corpus"
OUT = ROOT / "docs" / "corpus_pwr.html"
LOOKBACK = 21   # news_lookback_days: the window a review actually reads


def _jsonl(name):
    p = CORPUS / name
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _fig(traces, title, ytitle, height=300):
    fig = go.Figure(traces)
    fig.update_layout(template="seaborn", height=height, margin={"t": 10, "l": 60, "r": 20, "b": 80},
                      yaxis_title=ytitle, showlegend=False)
    return fig


def _hfig(traces, xtitle, height=300, left=220):
    """Horizontal-bar figure (categories on y), mirroring retrieval_pwr's plots 5/7/8."""
    fig = go.Figure(traces)
    fig.update_layout(template="seaborn", height=height, margin={"t": 10, "l": left, "r": 20, "b": 40},
                      xaxis_title=xtitle, showlegend=False)
    return fig


# ---- provenance: which feed an article came from (the daily WebSearch pull vs the one-time GKG+Wayback
# seed). first_query on a backfill article starts "gkg wave:"; a WebSearch article starts "recent business…".
_PROVCOL = {"websearch": "#3b82f6", "backfill": "#f59e0b"}   # blue = WebSearch, amber = GKG+Wayback seed
_PROVLBL = {"websearch": "WebSearch (daily)", "backfill": "GKG+Wayback (seed)"}
_PROV_ORDER = ("websearch", "backfill")


def _prov(a) -> str:
    return "backfill" if (a.get("first_query") or "").startswith("gkg") else "websearch"


def _legfig(traces, ytitle, height=300):
    """Vertical stacked-bar figure WITH a provenance legend (plots 1-2)."""
    fig = go.Figure(traces)
    fig.update_layout(template="seaborn", height=height, margin={"t": 10, "l": 60, "r": 20, "b": 80},
                      yaxis_title=ytitle, barmode="stack",
                      legend={"orientation": "h", "y": 1.02, "x": 1, "xanchor": "right", "yanchor": "bottom"})
    return fig


def _leghfig(traces, xtitle, height=300, left=220):
    """Horizontal stacked-bar figure WITH a provenance legend (plots 3, 5)."""
    fig = go.Figure(traces)
    fig.update_layout(template="seaborn", height=height, margin={"t": 30, "l": left, "r": 20, "b": 40},
                      xaxis_title=xtitle, barmode="stack",
                      legend={"orientation": "h", "y": 1.02, "x": 1, "xanchor": "right", "yanchor": "bottom"})
    return fig


def _blocked_count() -> int:
    """How many source_block domains news_sources.md excludes from the forward pull (shown as a card)."""
    import re
    import yaml
    p = ROOT / "news_sources.md"
    if not p.exists():
        return 0
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", p.read_text(), re.DOTALL)
    return len((yaml.safe_load(m.group(1)) or {}).get("source_block") or []) if m else 0


def build():
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from src import corpus as _corpus   # clean_author: drop PR-wire / site-brand / staff pseudo-authors
    arts, pulls = _jsonl("articles.jsonl"), _jsonl("pulls.jsonl")
    n = len(arts)
    dated = sorted(a["published_date"][:10] for a in arts if a.get("published_date"))
    undated = n - len(dated)
    ok = sum(1 for a in arts if a.get("extraction_ok"))
    tick = sum(1 for a in arts if a.get("tickers_mentioned"))
    today = date.today()
    win_lo = today - timedelta(days=LOOKBACK)
    in_window = sum(1 for d in dated if win_lo < date.fromisoformat(d) <= today)

    figs = []   # (title, subtitle, figure)

    # 1. Pull history + gaps (the monitoring panel), split by provenance (each pull is one feed)
    pull_day = Counter(p["pulled_at"][:10] for p in pulls)
    new_by_prov = {"websearch": Counter(), "backfill": Counter()}
    for p in pulls:
        pv = "backfill" if p["pull_id"].startswith("backfill-") else "websearch"
        new_by_prov[pv][p["pulled_at"][:10]] += p.get("n_new_articles", 0)
    pdays = sorted(pull_day)
    gaps = []
    if pdays:
        d0, d1 = date.fromisoformat(pdays[0]), today
        alld = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
        gaps = [d for d in alld if d not in pull_day]
    figs.append((
        "1. Pull history and gaps",
        (f"{len(pulls)} pulls across {len(pdays)} day(s). "
         + (f'<b style="color:#b45309;">{len(gaps)} missing day(s): {", ".join(gaps[:10])}'
            f'{"…" if len(gaps) > 10 else ""}</b> (a missed pull can\'t be cleanly backfilled)'
            if gaps else '<b style="color:#0a7a3a;">no missing days</b> since the corpus started')
         + ". Colored by feed: the one-time <b style=\"color:#f59e0b;\">GKG+Wayback seed</b> vs the daily "
           "<b style=\"color:#3b82f6;\">WebSearch</b> pulls."),
        _legfig([go.Bar(name=_PROVLBL[pv], x=pdays, y=[new_by_prov[pv][d] for d in pdays],
                        marker_color=_PROVCOL[pv]) for pv in _PROV_ORDER], "new articles / day")))

    # 2. Articles by published date (histogram). Floored at PLOT2_MIN_DATE to hide the mis-dated
    # hub/evergreen outliers (2010-2024) so the real recent corpus is visible. Categorical x-axis.
    _min2 = "2026-07-01"
    dcp = {"websearch": Counter(), "backfill": Counter()}
    for a in arts:
        d = (a.get("published_date") or "")[:10]
        if d and d >= _min2:
            dcp[_prov(a)][d] += 1
    dk = sorted(set(d for pv in dcp for d in dcp[pv]))
    _n_hidden = sum(1 for d in dated if d < _min2)
    _f2 = _legfig([go.Bar(name=_PROVLBL[pv], x=dk, y=[dcp[pv][d] for d in dk], marker_color=_PROVCOL[pv])
                   for pv in _PROV_ORDER], "articles")
    _f2.update_xaxes(type="category", tickangle=-45)
    figs.append((
        "2. Articles by published date",
        (f"published on or after {_min2} ({_n_hidden} older mis-dated hub/evergreen pages hidden), colored by "
         f"feed. The <b style=\"color:#f59e0b;\">GKG+Wayback seed</b> backfills the older days; the daily "
         f"<b style=\"color:#3b82f6;\">WebSearch</b> pulls add the recent edge. The curator reads a trailing "
         f"{LOOKBACK}-day slice of this."), _f2))

    # 3. Articles by wave — horizontal, split by provenance (mirrors retrieval_pwr plot 5)
    wave_p = {"websearch": Counter(), "backfill": Counter()}
    for a in arts:
        wave_p[_prov(a)][a.get("first_wave", "?")] += 1
    allw = sorted(set(w for pv in wave_p for w in wave_p[pv]),
                  key=lambda w: wave_p["websearch"][w] + wave_p["backfill"][w])   # ascending total -> top
    figs.append((
        "3. Articles by wave",
        "coverage by theme (the wave query whose result first surfaced each article), split by feed",
        _leghfig([go.Bar(name=_PROVLBL[pv], x=[wave_p[pv][w] for w in allw], y=allw, orientation="h",
                         marker_color=_PROVCOL[pv]) for pv in _PROV_ORDER], "articles")))

    # 4. Top sources — horizontal, split by feed (orange = GKG+Wayback seed contribution)
    _topsrc = [s for s, _ in Counter(a.get("source_domain", "?") for a in arts).most_common(15)]
    src_p = {"websearch": Counter(), "backfill": Counter()}
    for a in arts:
        d = a.get("source_domain", "?")
        if d in _topsrc:
            src_p[_prov(a)][d] += 1
    _sorder = sorted(_topsrc, key=lambda d: src_p["websearch"][d] + src_p["backfill"][d])   # ascending -> top
    figs.append((
        "4. Top sources (color = feed)",
        "top source domains by article count, split by feed: the <b style=\"color:#f59e0b;\">GKG+Wayback "
        "seed</b> contribution (authority-ranked, so it skews to recognized desks) vs the daily "
        "<b style=\"color:#3b82f6;\">WebSearch</b> pulls. source_block junk excluded upstream",
        _leghfig([go.Bar(name=_PROVLBL[pv], x=[src_p[pv][d] for d in _sorder], y=_sorder, orientation="h",
                         marker_color=_PROVCOL[pv]) for pv in _PROV_ORDER], "articles", height=420, left=200)))

    # 5. Articles per search term — horizontal, STACKED by wave (one bar per wave, orange+blue segments per
    # feed). The literal surfacing query lives in each segment's hover, so the wave-merged view stays legible
    # without a wall of keyword text on the axis.
    _pfx = "recent business and stock-market news about "
    wq = {"websearch": {}, "backfill": {}}   # feed -> {wave: {"n": count, "q": literal query text}}
    for a in arts:
        fq = a.get("first_query") or "?"
        pv = "backfill" if fq.startswith("gkg") else "websearch"
        wave = a.get("first_wave", "?")
        d = wq[pv].setdefault(wave, {"n": 0, "q": (fq if fq.startswith("gkg") else fq.replace(_pfx, ""))})
        d["n"] += 1
    allw = sorted(set(w for pv in wq for w in wq[pv]),
                  key=lambda w: sum(wq[pv].get(w, {}).get("n", 0) for pv in wq))   # ascending total -> top
    _f5 = _leghfig([go.Bar(name=_PROVLBL[pv], y=allw, orientation="h", marker_color=_PROVCOL[pv],
                           x=[wq[pv].get(w, {}).get("n", 0) for w in allw],
                           customdata=[wq[pv].get(w, {}).get("q", "(not queried)") for w in allw],
                           hovertemplate="%{y}: %{x} articles<br>query: %{customdata}<extra>"
                                         + _PROVLBL[pv] + "</extra>")
                    for pv in _PROV_ORDER], "articles", height=340, left=200)
    figs.append(("5. Articles per search term", "", _f5))

    # 6. Articles per author — horizontal, split by feed (both feeds now carry bylines; the seed's come from
    # the Wayback/live page metadata). Byline coverage is partial: wires (Reuters/AP) omit authors and
    # paywalled/JS pages block extraction. Raw material for a future gains-per-author view.
    _authors = [_corpus.clean_author(a.get("author"), a.get("publisher")) for a in arts]
    au_p = {"websearch": Counter(), "backfill": Counter()}
    for a in arts:
        au = _corpus.clean_author(a.get("author"), a.get("publisher"))
        if au:
            au_p[_prov(a)][au] += 1
    _atot = Counter()
    for pv in _PROV_ORDER:
        _atot.update(au_p[pv])
    _aorder = [k for k, _ in sorted(_atot.items(), key=lambda kv: kv[1])[-15:]]   # top 15, largest at top
    figs.append((
        "6. Articles per author", "",
        _leghfig([go.Bar(name=_PROVLBL[pv], x=[au_p[pv][k] for k in _aorder], y=[k[:50] for k in _aorder],
                         orientation="h", marker_color=_PROVCOL[pv]) for pv in _PROV_ORDER],
                 "articles", height=400, left=240)))

    charts = ""
    for i, (title, sub, fig) in enumerate(figs):
        subhtml = f'<p style="color:#666;margin:.2em 0 .4em;">{sub}</p>' if sub else ''
        charts += (f'<h2>{title}</h2>{subhtml}'
                   + fig.to_html(full_html=False, include_plotlyjs="cdn" if i == 0 else False,
                                 config={"displayModeBar": False}))

    # ---- stat cards (top row, mirrors retrieval_pwr) ----
    def _card(v, label, color="#0b7285"):
        return (f'<div style="display:inline-block;margin:0 1.8em .8em 0;vertical-align:top">'
                f'<b style="font-size:1.6em;color:{color}">{v}</b><br>'
                f'<span style="font-size:.82em;color:#555">{label}</span></div>')
    domains = len(set(a.get("source_domain") for a in arts))
    n_waves = len(set(a.get("first_wave", "?") for a in arts))
    n_app = len(_jsonl("appearances.jsonl"))
    with_author = sum(1 for x in _authors if x)   # real bylines (pseudo-authors already dropped)
    recog = sum(1 for a in arts if _corpus.source_tier(a.get("source_domain", "")) in ("specialty", "major"))
    n_bf = sum(1 for a in arts if _prov(a) == "backfill")
    n_ws = n - n_bf
    summary = ('<div style="margin:.8em 0 1.5em">'
               + _card(f"{n:,}", "articles in corpus (unique)")
               + _card(f"{n_bf} / {n_ws}", "GKG+Wayback seed / WebSearch daily", "#f59e0b")
               + _card(f"{n_app:,}", "sightings logged (appearances)")
               + _card(len(pulls), "pulls run")
               + _card(len(gaps), "missing days (gaps)", "#0a7a3a" if not gaps else "#b45309")
               + _card(f"{100*ok//n if n else 0}%", "full-text extracted")
               + _card(f"{100*with_author//n if n else 0}%", "with a byline (author)")
               + _card(f"{100*recog//n if n else 0}%", "recognized desks (specialty+major)")
               + _card(in_window, f"read by a review now ({LOOKBACK}d window)")
               + _card(f"{n_waves} / {domains}", "waves / source domains")
               + _card(_blocked_count(), "blocked domains (source_block)")
               + "</div>")

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Retriever Forwardtest</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1000px;margin:0 auto;
padding:0 1.5em 3em;color:#222;line-height:1.5}}h1,h2{{color:#111}}h2{{margin-top:1.6em}}a{{color:#2563eb}}</style>
</head><body>
{dash_nav.render("corpus_pwr.html")}
<h1>Retriever Forwardtest</h1>
<p style="color:#555;max-width:820px;">The frozen live-news archive (<code>data/forward_corpus/</code>). Two feeds
fill it: a daily <b>WebSearch</b> pull (<code>news_pull.sh</code> each evening) plus a one-time
<b>GKG+Wayback</b> cold-start seed &mdash; the very same BigQuery-GKG discovery + Wayback-lede (+ title-gated
live fallback) pipeline the <a href="retrieval_pwr.html">backtest Retriever DB</a> uses, pointed at the last
few weeks so the curator has a full trailing window on day one. Store-broad and deduped: each pull appends
only genuinely new articles. This is the forward analog of that backtest DB; the curator reads a trailing
{LOOKBACK}-day slice of it at each review.</p>
{summary}
<p style="color:#555;font-size:13px;margin-top:-.6em;">Raw data (local files):
<a href="../data/forward_corpus/articles.jsonl">articles.jsonl</a> &middot;
<a href="../data/forward_corpus/appearances.jsonl">appearances.jsonl</a> &middot;
<a href="../data/forward_corpus/pulls.jsonl">pulls.jsonl</a></p>
{charts}
</body></html>"""
    OUT.write_text(page)
    print(f"wrote {OUT}  ({n} articles, {len(pulls)} pulls, {len(gaps)} gap-days)")


if __name__ == "__main__":
    build()
