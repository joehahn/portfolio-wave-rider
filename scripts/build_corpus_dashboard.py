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


def build():
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

    # 2. Per-wave coverage
    wave = Counter(a.get("first_wave", "?") for a in arts)
    figs.append((
        "2. Coverage by wave", "unique articles whose first sighting was under each wave query",
        _fig([go.Bar(x=list(wave.keys()), y=list(wave.values()), marker_color="#8b5cf6")], "", "articles")))

    # 3. Top sources
    src = Counter(a.get("source_domain", "?") for a in arts).most_common(15)
    figs.append((
        "3. Top sources", "domains contributing the most articles",
        _fig([go.Bar(x=[s for s, _ in src], y=[c for _, c in src], marker_color="#10b981")], "", "articles",
             height=320)))

    charts = ""
    for i, (title, sub, fig) in enumerate(figs):
        charts += (f'<h2>{title}</h2><p style="color:#666;margin:.2em 0 .4em;">{sub}</p>'
                   + fig.to_html(full_html=False, include_plotlyjs="cdn" if i == 0 else False,
                                 config={"displayModeBar": False}))

    rng = f"{dated[0]} to {dated[-1]}" if dated else "n/a"
    summary = (
        '<table style="font-size:14px;border-collapse:collapse;margin:.5em 0 1.5em;">'
        f'<tr><td style="padding:2px 16px 2px 0;color:#666;">articles</td><td><b>{n}</b> '
        f'({len(dated)} dated, {undated} undated)</td></tr>'
        f'<tr><td style="padding:2px 16px 2px 0;color:#666;">published-date span</td><td>{rng}</td></tr>'
        f'<tr><td style="padding:2px 16px 2px 0;color:#666;">full-text extracted</td><td>{100*ok//n if n else 0}% '
        f'&middot; names a ticker {100*tick//n if n else 0}%</td></tr>'
        f'<tr><td style="padding:2px 16px 2px 0;color:#666;">waves &middot; sources</td>'
        f'<td>{len(wave)} waves &middot; {len(set(a.get("source_domain") for a in arts))} domains</td></tr>'
        f'<tr><td style="padding:2px 16px 2px 0;color:#666;">a review today would read</td>'
        f'<td><b>{in_window}</b> articles (trailing {LOOKBACK}d window, {win_lo} to {today})</td></tr>'
        '</table>')

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>PWR — news corpus</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1000px;margin:0 auto;
padding:0 1.5em 3em;color:#222;line-height:1.5}}h1,h2{{color:#111}}h2{{margin-top:1.6em}}a{{color:#2563eb}}</style>
</head><body>
{dash_nav.render("corpus_pwr.html")}
<h1>Forward news corpus</h1>
<p style="color:#555;max-width:820px;">The frozen live-news archive (<code>data/forward_corpus/</code>) that
<code>news_pull.sh</code> fills each evening via WebSearch. Store-broad and deduped: each pull appends only
genuinely new articles. This is the forward analog of the backtest
<a href="retrieval_pwr.html">Retriever DB</a>; the curator reads a trailing {LOOKBACK}-day slice of it at each review.</p>
{summary}
{charts}
</body></html>"""
    OUT.write_text(page)
    print(f"wrote {OUT}  ({n} articles, {len(pulls)} pulls, {len(gaps)} gap-days)")


if __name__ == "__main__":
    build()
