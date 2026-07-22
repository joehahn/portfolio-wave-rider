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

    # 1. Pull history + gaps (the monitoring panel)
    pull_day = Counter(p["pulled_at"][:10] for p in pulls)
    new_by_day = Counter()
    for p in pulls:
        new_by_day[p["pulled_at"][:10]] += p.get("n_new_articles", 0)
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
            if gaps else '<b style="color:#0a7a3a;">no missing days</b> since the corpus started')),
        _fig([go.Bar(x=[d for d in sorted(pull_day)], y=[new_by_day[d] for d in sorted(pull_day)],
                     marker_color="#3b82f6")], "", "new articles / day")))

    # 2. Articles by published date (histogram). Floored at PLOT2_MIN_DATE to hide the mis-dated
    # hub/evergreen outliers (2010-2024) so the real recent corpus is visible. Categorical x-axis.
    _min2 = "2026-07-01"
    dc = Counter(d for d in dated if d >= _min2)
    dk = sorted(dc)
    _n_hidden = sum(1 for d in dated if d < _min2)
    _f2 = _fig([go.Bar(x=dk, y=[dc[d] for d in dk], marker_color="#0ea5e9")], "", "articles")
    _f2.update_xaxes(type="category", tickangle=-45)
    figs.append((
        "2. Articles by published date",
        (f"published on or after {_min2} ({_n_hidden} older mis-dated hub/evergreen pages hidden). "
         f"The curator reads a trailing {LOOKBACK}-day slice of this."), _f2))

    # 3. Articles by wave — horizontal (mirrors retrieval_pwr plot 5)
    wave = Counter(a.get("first_wave", "?") for a in arts)
    wv = sorted(wave.items(), key=lambda kv: kv[1])            # ascending -> largest bar at top
    figs.append((
        "3. Articles by wave",
        "coverage by theme (the wave query whose result first surfaced each article)",
        _hfig([go.Bar(x=[v for _, v in wv], y=[k for k, _ in wv], orientation="h",
                      marker_color="#22c55e")], "articles")))

    # 4. Top sources — horizontal (mirrors retrieval_pwr plot 7)
    src = Counter(a.get("source_domain", "?") for a in arts).most_common(15)[::-1]
    figs.append((
        "4. Top sources",
        "domains contributing the most articles &mdash; the news_sources.md source_block list is excluded (see note)",
        _hfig([go.Bar(x=[c for _, c in src], y=[s for s, _ in src], orientation="h",
                      marker_color="#14b8a6")], "articles", height=400, left=200)))

    # 5. Articles per search term — horizontal (mirrors retrieval_pwr plot 8)
    _pfx = "recent business and stock-market news about "
    q = Counter((a.get("first_query") or "?").replace(_pfx, "") for a in arts)
    qi = sorted(q.items(), key=lambda kv: kv[1])
    figs.append((
        "5. Articles per search term",
        "the retriever's surfacing queries (one per wave, built from gkg_config.json keywords)",
        _hfig([go.Bar(x=[v for _, v in qi], y=[k[:58] for k, _ in qi], orientation="h",
                      marker_color="#a855f7")], "articles", height=340, left=330)))

    # 6. Articles per author — horizontal (byline attribution; raw material for gains-per-author later)
    _authors = [_corpus.clean_author(a.get("author"), a.get("publisher")) for a in arts]
    au = Counter(x for x in _authors if x)   # real bylines only (pseudo-authors dropped)
    ai = sorted(au.items(), key=lambda kv: kv[1])[-15:]   # top 15, largest bar at top
    figs.append((
        "6. Articles per author",
        "byline attribution (trafilatura). Partial by nature &mdash; wire services (Reuters/AP) omit authors "
        "and paywalled/JS pages block extraction. This is the raw material for a future gains-per-author "
        "view, once forward price outcomes accrue and ticker attribution is tightened",
        _hfig([go.Bar(x=[v for _, v in ai], y=[k[:50] for k, _ in ai], orientation="h",
                      marker_color="#f59e0b")], "articles", height=380, left=240)))

    charts = ""
    for i, (title, sub, fig) in enumerate(figs):
        charts += (f'<h2>{title}</h2><p style="color:#666;margin:.2em 0 .4em;">{sub}</p>'
                   + fig.to_html(full_html=False, include_plotlyjs="cdn" if i == 0 else False,
                                 config={"displayModeBar": False}))

    # ---- stat cards (top row, mirrors retrieval_pwr) ----
    def _card(v, label, color="#0b7285"):
        return (f'<div style="display:inline-block;margin:0 1.8em .8em 0;vertical-align:top">'
                f'<b style="font-size:1.6em;color:{color}">{v}</b><br>'
                f'<span style="font-size:.82em;color:#555">{label}</span></div>')
    domains = len(set(a.get("source_domain") for a in arts))
    n_app = len(_jsonl("appearances.jsonl"))
    with_author = sum(1 for x in _authors if x)   # real bylines (pseudo-authors already dropped)
    summary = ('<div style="margin:.8em 0 1.5em">'
               + _card(f"{n:,}", "articles in corpus (unique)")
               + _card(f"{n_app:,}", "sightings logged (appearances)")
               + _card(len(pulls), "pulls run")
               + _card(len(gaps), "missing days (gaps)", "#0a7a3a" if not gaps else "#b45309")
               + _card(f"{100*ok//n if n else 0}%", "full-text extracted")
               + _card(f"{100*with_author//n if n else 0}%", "with a byline (author)")
               + _card(in_window, f"read by a review now ({LOOKBACK}d window)")
               + _card(f"{len(wave)} / {domains}", "waves / source domains")
               + _card(_blocked_count(), "blocked domains (source_block)")
               + "</div>")

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>PWR — forward retriever</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1000px;margin:0 auto;
padding:0 1.5em 3em;color:#222;line-height:1.5}}h1,h2{{color:#111}}h2{{margin-top:1.6em}}a{{color:#2563eb}}</style>
</head><body>
{dash_nav.render("corpus_pwr.html")}
<h1>Forward news retriever &mdash; WebSearch corpus</h1>
<p style="color:#555;max-width:820px;">The frozen live-news archive (<code>data/forward_corpus/</code>) that
<code>news_pull.sh</code> fills each evening via WebSearch. Store-broad and deduped: each pull appends only
genuinely new articles. This is the forward analog of the backtest
<a href="retrieval_pwr.html">Retriever DB</a>; the curator reads a trailing {LOOKBACK}-day slice of it at each review.</p>
{summary}
<p style="color:#555;font-size:13px;margin-top:-.6em;">Raw data (local files):
<a href="../data/forward_corpus/articles.jsonl">articles.jsonl</a> &middot;
<a href="../data/forward_corpus/appearances.jsonl">appearances.jsonl</a> &middot;
<a href="../data/forward_corpus/pulls.jsonl">pulls.jsonl</a></p>
{charts}
<p style="color:#666;font-size:13px;max-width:820px;margin-top:1.5em;"><b>Note on sources:</b> the forward
pull runs Anthropic WebSearch on the wave keywords with the <code>news_sources.md</code>
<code>source_block</code> list passed as web_search <code>blocked_domains</code>, so the low-signal /
PR-mill domains the backtest also drops are excluded here too. It does <b>not</b> apply
<code>source_major</code> / specialty as an allow-list, because <code>allowed_domains</code> is a hard
whitelist that would gut recall (and the file itself calls those "a preferred list, not an exclusive one").</p>
</body></html>"""
    OUT.write_text(page)
    print(f"wrote {OUT}  ({n} articles, {len(pulls)} pulls, {len(gaps)} gap-days)")


if __name__ == "__main__":
    build()
