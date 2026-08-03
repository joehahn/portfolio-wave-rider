#!/usr/bin/env python3
"""Build docs/retrieval_forward.html -- the "Retriever Forwardtest" (RFT) dashboard.

RBS shows the bootstrap-era corpus (backtest tail spliced onto the forward pulls). RFT is narrower and
different in kind: it watches the health of the LIVE WebSearch feed that the forward curator (CFT) eats,
day by day. Deliberately NOT a re-plot of RBS's volume charts -- the questions here are:

  1. is the cron still pulling, and how much is genuinely new?
  2. how much of what it pulls is not an article at all (quote/ticker pages)?
  3. how much arrives with usable body text (a lede) rather than a bare headline?
  4. how often is an author captured?
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
from src import portfolio as _pf  # noqa: E402  news_lookback_days, so the window marker is profile-driven

BLUE, GREEN, ORANGE, RED, GREY = "#1f77b4", "#2ca02c", "#ff7f0e", "#e03131", "#adb5bd"
# Run dir -> display label for plot 5. Explicit, NOT derived by stripping the dir prefix: the dir stayed
# `forward-ft` through the FT -> CFT rename, so a derived label silently went stale on the chart.
CURATOR_RUNS = {"forward-ft": "CFT", "bootstrap-cbs": "CBS"}


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
    # distinct articles seen on a day (a story returned twice by two pulls counts ONCE here). This is the
    # unit a reader expects, and `new_by_day` is a strict subset of it.
    _seen_ids_by_day = defaultdict(set)
    for _s in apps:
        _seen_ids_by_day[(_s.get("pulled_at") or "")[:10]].add(_s.get("article_id"))
    distinct_by_day = {d: len(v) for d, v in _seen_ids_by_day.items()}
    pulls_by_day = Counter((p_.get("pulled_at") or "")[:10] for p_ in pulls)
    nonart_by_day = Counter((a.get("first_pulled_at") or "")[:10] for a in arts if not a.get("is_article"))
    lede_by_day = Counter((a.get("first_pulled_at") or "")[:10] for a in arts if a["_lede"])
    auth_by_day = Counter((a.get("first_pulled_at") or "")[:10] for a in arts if (a.get("author") or "").strip())

    # ---- age at pull = first_pulled_at - published_date. A search hit is NOT necessarily fresh: the
    # engine returns whatever ranks, so this is the distribution of how stale the feed's material is.
    # Anything older than news_lookback_days is stored but then filtered out of every pool by read_slice.
    ages = []
    for a in arts:
        try:
            ages.append((date.fromisoformat((a.get("first_pulled_at") or "")[:10])
                         - date.fromisoformat((a.get("published_date") or "")[:10])).days)
        except Exception:  # noqa: BLE001 - one of the two dates is missing/unparseable
            pass
    _news_lb = int(_pf.load_financial_model().get("news_lookback_days") or 14)
    _cap = 24                       # everything this old or older collapses into one overflow bin
    # oldest on the LEFT, freshest on the right: the eye then travels toward "today", and the
    # news_lookback_days line reads as a cutoff with the unusable material behind it.
    age_bins = [f"{_cap}+"] + [str(i) for i in range(_cap - 1, -1, -1)]
    age_counts = ([sum(1 for x in ages if x >= _cap)]
                  + [sum(1 for x in ages if x == i) for i in range(_cap - 1, -1, -1)])
    _median_age = sorted(ages)[len(ages) // 2] if ages else None

    # ---- pools actually fed to curations (what read_slice returned, per run dir)
    pool_rows = []
    for run, label in CURATOR_RUNS.items():
        for f in sorted((ROOT / "data" / "curator_runs" / run).glob("2*-pool.json")):
            try:
                j = json.loads(f.read_text())
            except Exception:  # noqa: BLE001
                continue
            # Only pools THIS corpus fed. CBS re-curates every period back to its 2026-04-27 seed, but
            # those early pools were built by the backtest's gkg-wayback retriever -- plotting them here
            # implied the WebSearch corpus reached back to April, when it starts at the 07-22 handoff.
            if "websearch" not in str(j.get("source") or ""):
                continue
            pool_rows.append((label, j.get("as_of_date") or f.stem[:10], int(j.get("n_articles") or 0)))

    fig = make_subplots(
        rows=6, cols=1, vertical_spacing=0.065,
        # row 1 carries two units (articles as bars, search hits as a line), so it needs a real
        # secondary axis. Hand-numbering one collides with the subplot axes as rows are added.
        specs=[[{"secondary_y": True}]] + [[{"secondary_y": False}]] * 5,
        subplot_titles=(
            "1. Articles seen per day, and how many were new",
            "2. Non-articles ingested per day",
            "3. Body-text and author capture rate",
            "4. Articles per wave",
            "5. Pool size per curation",
            "6. Article age when pulled",
        ))

    # 1. new vs sightings
    # make_subplots put exactly one annotation per subplot title; anything added later (axis markers) must
    # NOT be caught by the restyling loop below, which would bold it and drag it to the left margin.
    _n_titles = len(fig.layout.annotations)

    # Bars are ARTICLES (distinct stories); the line is SIGHTINGS (search hits), a different unit, so it
    # gets its own axis. "new to corpus" is a strict subset of "distinct articles seen", drawn on top so
    # the containment is visible rather than implied.
    fig.add_trace(go.Bar(x=span, y=[distinct_by_day.get(d, 0) for d in span],
                         name="distinct articles seen", marker_color=GREY, legend="legend"), row=1, col=1)
    fig.add_trace(go.Bar(x=span, y=[new_by_day.get(d, 0) for d in span], name="new to corpus",
                         marker_color=BLUE, legend="legend"), row=1, col=1)
    # hits above the bars means the day ran MORE THAN ONE pull (a story is almost never returned twice
    # inside one pull -- wave queries overlap on ~1% of articles), so the pull count is what explains the
    # gap. Carried in the hover rather than as another series.
    fig.add_trace(go.Scatter(x=span, y=[sight_by_day.get(d, 0) for d in span], name="sightings (hits)",
                             mode="lines+markers", line={"color": ORANGE, "width": 1.6, "dash": "dot"},
                             marker={"size": 5}, legend="legend",
                             customdata=[pulls_by_day.get(d, 0) for d in span],
                             hovertemplate="%{x}: %{y} hits from %{customdata} pull(s)<extra></extra>"),
                  row=1, col=1, secondary_y=True)
    # 2. non-articles
    fig.add_trace(go.Bar(x=span, y=[nonart_by_day.get(d, 0) for d in span], name="non-articles",
                         marker_color=RED, showlegend=False), row=2, col=1)
    # 3. capture rates
    _rate = lambda num, d: (100.0 * num.get(d, 0) / new_by_day[d]) if new_by_day.get(d) else None  # noqa: E731
    fig.add_trace(go.Scatter(x=span, y=[_rate(lede_by_day, d) for d in span], name="% with body text",
                             mode="lines+markers", line={"color": GREEN}, legend="legend2"), row=3, col=1)
    fig.add_trace(go.Scatter(x=span, y=[_rate(auth_by_day, d) for d in span], name="% with author",
                             mode="lines+markers", line={"color": ORANGE}, legend="legend2"), row=3, col=1)
    # 4. waves
    wave_n = Counter(a.get("first_wave") or "?" for a in arts)
    _w = wave_n.most_common()
    fig.add_trace(go.Bar(x=[w for w, _ in _w], y=[n for _, n in _w], marker_color=BLUE,
                         showlegend=False), row=4, col=1)
    # 5. pools per curation. No max_articles cap line here: that knob truncates BACKTEST-retrieval pools,
    # and every pool plotted above is fed by the forward WebSearch corpus, which sets its own result count.
    for run, colour in (("CFT", BLUE), ("CBS", ORANGE)):
        rows = sorted((r for r in pool_rows if r[0] == run), key=lambda r: r[1])
        if rows:
            fig.add_trace(go.Scatter(x=[r[1] for r in rows], y=[r[2] for r in rows], name=f"{run} pool",
                                     mode="lines+markers", line={"color": colour},
                                     legend="legend3"), row=5, col=1)

    fig.update_layout(template="seaborn", height=1780, barmode="overlay",
                      margin={"t": 60, "l": 70, "r": 190, "b": 60})
    # One legend PER SUBPLOT, parked to the right of its own rows. A single shared legend listed every
    # trace in the figure next to chart 1, which read as though ledes/bylines/pools belonged there.
    # Each legend is anchored to the TOP of its subplot's y-domain, so it tracks the layout automatically.
    def _dom_top(row: int) -> float:
        """Top of a subplot's y-domain. NB row 1 owns a secondary axis, so the primary axis for row N is
        yaxis{N+1} from row 2 on -- read it off the layout rather than assuming yaxis{N}."""
        ax = fig.layout[f"yaxis{'' if row == 1 else row + 1}"]
        return float(ax.domain[1])
    _legend_style = {"orientation": "v", "x": 1.01, "xanchor": "left", "yanchor": "top",
                     "bgcolor": "rgba(255,255,255,0.85)", "font": {"size": 12}}
    fig.update_layout(legend={**_legend_style, "y": _dom_top(1)},
                      legend2={**_legend_style, "y": _dom_top(3)},
                      legend3={**_legend_style, "y": _dom_top(5)})
    fig.update_yaxes(title_text="articles", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="search hits", row=1, col=1, secondary_y=True,
                     showgrid=False, rangemode="tozero")
    fig.update_yaxes(title_text="count", row=2, col=1)
    fig.update_yaxes(title_text="% of the day's new articles", range=[0, 100], row=3, col=1)
    fig.update_yaxes(title_text="articles", row=4, col=1)
    fig.update_yaxes(title_text="articles in pool", row=5, col=1)
    fig.add_trace(go.Bar(x=age_bins, y=age_counts, marker_color=BLUE, showlegend=False,
                         hovertemplate="%{x} day(s) old: %{y} articles<extra></extra>"), row=6, col=1)
    # median line: the typical article, which the long evergreen tail pulls the MEAN far away from
    if _median_age is not None:
        _mlab = str(_median_age) if _median_age < _cap else f"{_cap}+"
        _mx = age_bins.index(_mlab)
        fig.add_vline(x=_mx, row=6, col=1, line={"dash": "dot", "color": GREEN, "width": 1.5})
        fig.add_annotation(x=_mx, y=0.72, yref="y domain", yanchor="top", xanchor="left",
                           text=f" median = {_median_age}d", showarrow=False,
                           font={"size": 11, "color": GREEN}, row=6, col=1)
    # the curator's window: everything to the right of this line is ingested but never read
    # categorical axis: position the cutoff by INDEX, between the ">lookback" and "lookback" categories
    _cut = age_bins.index(str(_news_lb)) - 0.5
    fig.add_vline(x=_cut, row=6, col=1, line={"dash": "dash", "color": RED, "width": 1.5})
    fig.add_annotation(x=_cut, y=0.92, yref="y domain", yanchor="top", xanchor="right",
                       text=f"older than news_lookback_days = {_news_lb} ", showarrow=False,
                       font={"size": 11, "color": RED}, row=6, col=1)
    # NB literal arrows, not HTML entities: plotly renders axis titles as SVG text and would print
    # "&larr;" verbatim.
    fig.update_xaxes(title_text="days between publication and pull   ← older · fresher →",
                     row=6, col=1)
    fig.update_yaxes(title_text="articles", row=6, col=1)
    for _st in fig.layout.annotations[:_n_titles]:
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
             + _card(f"{100 * n_auth / n_art:.0f}%", "with an author")
             + _card(f"{n_non}", "non-articles dropped", warn=n_non / n_art > 0.05)
             + _card(last_gap, "missed pull days", warn=bool(gaps))
             + _card(f"{sum(1 for d in span if pulls_by_day.get(d, 0) > 1)}", "days with >1 pull")
             + _card(f"{sorted(ages)[len(ages) // 2]}d" if ages else "n/a", "median age at pull")
             + _card(f"{100 * sum(1 for x in ages if x > _news_lb) / len(ages):.0f}%" if ages else "n/a",
                     f"older than the {_news_lb}d window",
                     warn=bool(ages) and sum(1 for x in ages if x > _news_lb) / len(ages) > 0.30)
             + "</div>")

    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Retriever Forwardtest (RFT)</title>'
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;'
        'margin:0 auto;padding:0 1.5em;color:#222;line-height:1.5;}h1,h2{color:#111;}</style></head><body>'
        '<h1>Retriever Forwardtest (RFT)</h1>'
        + dash_nav.render("retrieval_forward.html")
        + '<p style="color:#555;max-width:860px;">Health of the live WebSearch feed behind the Curator '
          'Forwardtest (CFT) &mdash; ingest quality, not market outcome. In chart&nbsp;1 the bars count '
          '<b>articles</b> and the dotted line counts <b>search hits</b> (a different unit, right-hand axis): '
          'one story returned by three pulls in a day is 3 hits but 1 article. <b>New to corpus</b> is a '
          'strict subset of <b>distinct articles seen</b> &mdash; the gap between them is stories the feed '
          'already had. Bars flat at zero means the daily cron stopped. <b>Non-articles</b> are quote and '
          'ticker pages: stored, tagged, never fed to a curator. Sister page: '
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
