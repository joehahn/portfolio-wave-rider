#!/usr/bin/env python3
"""Build docs/retrieval_forward.html -- the "Retriever Forwardtest" (RFT) dashboard.

RBS shows the bootstrap-era corpus (backtest tail spliced onto the forward pulls). RFT is narrower and
different in kind: it watches the health of the LIVE WebSearch feed that the forward curator (CFT) eats,
day by day. Deliberately NOT a re-plot of RBS's volume charts -- the questions here are:

  1. is the cron still pulling, and how much is genuinely new?
  2. how much of what it pulls is not an article at all (quote/ticker pages)?
  3. how much arrives with usable body text (a lede) rather than a bare headline?
  4. how often is a byline captured?
  5. which waves are being fed, and which are starving?
  6. how big was the pool each curation actually read?

Reads data/forward_corpus/{articles,appearances,pulls}.jsonl plus the curator run dirs' pool files.
Render-only: no LLM call, no network. Refreshed by the daily news_pull.sh cron after the pull.
Usage: python scripts/build_forward_retrieval_dashboard.py [--corpus data/forward_corpus] [--out docs/...]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import dash_nav  # noqa: E402
from src import corpus as _corpus  # noqa: E402  is_article / clean_lede: the SAME predicates the curator sees

BLUE, GREEN, ORANGE, RED, GREY = "#1f77b4", "#2ca02c", "#ff7f0e", "#e03131", "#adb5bd"
CURATOR_RUNS = ("forward-ft", "bootstrap-cbs")   # run dirs whose pools are fed by this corpus


def _iso(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except Exception:
        return None


def _jsonl(p: Path):
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def load(corpus_dir: str):
    d = ROOT / corpus_dir
    arts = _jsonl(d / "articles.jsonl")
    apps = _jsonl(d / "appearances.jsonl")
    pulls = _jsonl(d / "pulls.jsonl")
    for a in arts:                       # tolerate pre-tag records
        a.setdefault("is_article", _corpus.is_article(a))
        a["_lede"] = _corpus.clean_lede(a.get("snippet") or a.get("full_text") or "")
        a["_day"] = (a.get("published_date") or a.get("first_pulled_at") or "")[:10]
    return arts, apps, pulls


def build(corpus_dir: str, out: Path) -> None:
    arts, apps, pulls = load(corpus_dir)
    if not arts:
        print("empty forward corpus; nothing to render", file=sys.stderr)
        return
    pull_days = sorted({(p.get("pulled_at") or "")[:10] for p in pulls if p.get("pulled_at")})
    d0, d1 = _iso(pull_days[0]), _iso(pull_days[-1])
    span = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]

    # ---- per-day aggregates, keyed on the PULL day (ingest health, not publication volume)
    new_by_day = Counter()
    for a in arts:
        k = (a.get("first_pulled_at") or "")[:10]
        if k:
            new_by_day[k] += 1
    sight_by_day = Counter((s.get("pulled_at") or "")[:10] for s in apps)
    nonart_by_day = Counter((a.get("first_pulled_at") or "")[:10] for a in arts if not a.get("is_article"))
    lede_by_day = Counter((a.get("first_pulled_at") or "")[:10] for a in arts if a["_lede"])
    auth_by_day = Counter((a.get("first_pulled_at") or "")[:10] for a in arts if (a.get("author") or "").strip())

    # ---- pools actually fed to curations (what read_slice returned, per run dir)
    pool_rows = []
    for run in CURATOR_RUNS:
        for f in sorted((ROOT / "data" / "curator_runs" / run).glob("2*-pool.json")):
            try:
                j = json.loads(f.read_text())
            except Exception:  # noqa: BLE001
                continue
            pool_rows.append((run.replace("forward-", "").replace("bootstrap-", "").upper(),
                              j.get("as_of_date") or f.stem[:10], int(j.get("n_articles") or 0)))

    fig = make_subplots(
        rows=5, cols=1, vertical_spacing=0.075,
        subplot_titles=(
            "1. Daily pull: new articles vs sightings — a sighting is one search hit (the same story is returned day after day); a new article is the first time a story enters the corpus. Flat at zero = the cron stopped",
            "2. Non-articles ingested (quote / ticker pages) — never reach the curator, but show query drift",
            "3. Body-text and byline capture rate — how much arrives as more than a headline",
            "4. Articles per wave — which waves the queries are actually feeding",
            "5. Pool size fed to each curation — what the curator read on the day it decided",
        ))

    # 1. new vs sightings
    fig.add_trace(go.Bar(x=span, y=[sight_by_day.get(d, 0) for d in span], name="sightings",
                         marker_color=GREY), row=1, col=1)
    fig.add_trace(go.Bar(x=span, y=[new_by_day.get(d, 0) for d in span], name="new articles",
                         marker_color=BLUE), row=1, col=1)
    # 2. non-articles
    fig.add_trace(go.Bar(x=span, y=[nonart_by_day.get(d, 0) for d in span], name="non-articles",
                         marker_color=RED, showlegend=False), row=2, col=1)
    # 3. capture rates
    _rate = lambda num, d: (100.0 * num.get(d, 0) / new_by_day[d]) if new_by_day.get(d) else None  # noqa: E731
    fig.add_trace(go.Scatter(x=span, y=[_rate(lede_by_day, d) for d in span], name="% with lede",
                             mode="lines+markers", line={"color": GREEN}), row=3, col=1)
    fig.add_trace(go.Scatter(x=span, y=[_rate(auth_by_day, d) for d in span], name="% with byline",
                             mode="lines+markers", line={"color": ORANGE}), row=3, col=1)
    # 4. waves
    wave_n = Counter(a.get("first_wave") or "?" for a in arts)
    _w = wave_n.most_common()
    fig.add_trace(go.Bar(x=[w for w, _ in _w], y=[n for _, n in _w], marker_color=BLUE,
                         showlegend=False), row=4, col=1)
    # 5. pools per curation
    for run, colour in (("FT", BLUE), ("CBS", ORANGE)):
        rows = sorted((r for r in pool_rows if r[0] == run), key=lambda r: r[1])
        if rows:
            fig.add_trace(go.Scatter(x=[r[1] for r in rows], y=[r[2] for r in rows], name=f"{run} pool",
                                     mode="lines+markers", line={"color": colour}), row=5, col=1)

    fig.update_layout(template="seaborn", height=1500, barmode="overlay",
                      margin={"t": 60, "l": 70, "r": 190, "b": 60},
                      # legend to the RIGHT of the plotting area: these are stacked time series and a
                      # horizontal legend on top crowds the first subplot's title.
                      legend={"orientation": "v", "x": 1.01, "xanchor": "left", "y": 1.0,
                              "yanchor": "top", "bgcolor": "rgba(255,255,255,0.85)"})
    fig.update_yaxes(title_text="count", row=1, col=1)
    fig.update_yaxes(title_text="count", row=2, col=1)
    fig.update_yaxes(title_text="% of the day's new articles", range=[0, 100], row=3, col=1)
    fig.update_yaxes(title_text="articles", row=4, col=1)
    fig.update_yaxes(title_text="articles in pool", row=5, col=1)
    for _st in fig.layout.annotations:
        _st.update(font={"size": 16, "color": "#111"}, x=0.0, xanchor="left", text=f"<b>{_st.text}</b>")

    # ---- summary cards
    n_art = len(arts)
    n_non = sum(1 for a in arts if not a.get("is_article"))
    n_lede = sum(1 for a in arts if a["_lede"])
    n_auth = sum(1 for a in arts if (a.get("author") or "").strip())
    gaps = [d for d in span if d not in pull_days]
    last_gap = f"{len(gaps)} day(s) with no pull" if gaps else "none"

    def _card(v, label, warn=False):
        return (f'<div style="display:inline-block;min-width:132px;margin:0 1.4em 0.8em 0">'
                f'<b style="font-size:1.45em;color:{RED if warn else "#0b7285"}">{v}</b><br>'
                f'<span style="font-size:.78em;color:#555">{label}</span></div>')

    cards = ('<h2 style="margin:1.2em 0 0.3em;">Summary</h2><div>'
             + _card(f"{n_art:,}", "articles in corpus")
             + _card(f"{len(pulls)}", "pulls logged")
             + _card(f"{span[0]} → {span[-1]}", "window")
             + _card(f"{100 * n_lede / n_art:.0f}%", "with body text", warn=n_lede / n_art < 0.6)
             + _card(f"{100 * n_auth / n_art:.0f}%", "with a byline")
             + _card(f"{n_non}", "non-articles dropped", warn=n_non / n_art > 0.05)
             + _card(last_gap, "missed pull days", warn=bool(gaps))
             + "</div>")

    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Retriever Forwardtest (RFT)</title>'
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;'
        'margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5;}h1,h2{color:#111;}</style></head><body>'
        '<h1>Retriever Forwardtest (RFT)</h1>'
        + dash_nav.render("retrieval_forward.html")
        + '<p style="color:#555;max-width:860px;">Health of the live WebSearch feed behind the Curator '
          'Forwardtest (CFT). Every chart is about <b>ingest quality</b>, not market outcome: whether the '
          'daily cron is running, how much of what it collects is really an article, and how much arrives '
          'with enough text for the curator to reason over. Sister page: '
          '<a href="retrieval_bootstrap.html">RBS</a> covers the bootstrap-era corpus.</p>'
        + cards
        + fig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})
        + '</body></html>')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out}  ({n_art} articles | {len(pulls)} pulls | {n_non} non-articles)", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the Retriever Forwardtest (RFT) dashboard.")
    ap.add_argument("--corpus", default="data/forward_corpus")
    ap.add_argument("--out", default="docs/retrieval_forward.html")
    a = ap.parse_args(argv)
    build(a.corpus, ROOT / a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
