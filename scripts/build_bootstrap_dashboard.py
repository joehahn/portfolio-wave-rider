#!/usr/bin/env python3
"""Build docs/retrieval_bootstrap.html — the RETRIEVER view of the BOOTSTRAP dataset: the last ~3 months
of the backtest (GKG-discovered, Wayback ledes) spliced onto the forward cron's daily WebSearch pulls.
It is the news-coverage bridge across the backtest->forward handoff (2026-07-22).

Two provenances, kept DISTINCT (unlike the backtest RBT, which only knows Wayback/live/none):
  - Wayback     : backtest-tail ledes, archived-at-the-time (look-ahead-safe by construction)
  - live-fallback: backtest-tail ledes fetched from today's page (look-ahead-BIASED; backtest only)
  - WebSearch   : forward ledes from the daily cron (look-ahead-safe -- the news IS current)
Wayback + WebSearch are both clean; live-fallback is the only biased one and appears only in the tail.

Reads data/curator_runs/bootstrap-retriever/{*-pool.json,_authors.json} (assembled from canon14's last-3mo
pools + data/forward_corpus/articles.jsonl). Pure Plotly + seaborn to match the other PWR dashboards.
Usage: python scripts/build_bootstrap_dashboard.py [--run-dir ...] [--out docs/retrieval_bootstrap.html]
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dash_nav  # shared cross-page nav (Backtest | Forwardtest | Bootstrap groups)  # noqa: E402
BT_SRC = "gkg-wayback-articles"        # backtest pool `source` tag
FW_SRC = "forward-websearch"           # forward pool `source` tag


def _fig_html(fig):
    return fig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False})


def _load_pools(canon_dir: str, forward_corpus: str, since: str):
    """Assemble the bootstrap pools directly from source (no intermediate run-dir), so a re-run always
    reflects the latest cron pulls: (1) the backtest's own top-100 pools with as_of >= `since` (Wayback
    ledes, kept verbatim), and (2) the forward cron's articles grouped into per-pull-day pools with a
    `websearch` provenance. Returns (backtest_pools, forward_pools), each already in the pool-JSON shape."""
    bt = sorted((json.loads(f.read_text()) for f in (ROOT / canon_dir).glob("*-pool.json")),
                key=lambda p: p["as_of_date"])
    bt = [p for p in bt if p["as_of_date"] >= since]

    from collections import defaultdict
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
                   "lede_sources": dict(src), "articles": items})
    return bt, fw


def build(canon_dir: str, forward_corpus: str, since: str, out: Path) -> None:
    bt, fw = _load_pools(canon_dir, forward_corpus, since)
    pools = sorted(bt + fw, key=lambda p: p["as_of_date"])

    # ---- flatten to unique articles (dedupe by url; a url's first provenance wins) ----
    prov_of = {}          # url -> provenance bucket
    wave_of, src_of, auth_of = {}, {}, {}
    for p in pools:
        for a in p.get("articles", []):
            u = a.get("url")
            if not u or u in prov_of:
                continue
            ls = a.get("lede_source", "none")
            prov_of[u] = {"wayback": "Wayback", "live": "live-fallback",
                          "websearch": "WebSearch"}.get(ls, "none (no lede)")
            wave_of[u] = a.get("wave", "")
            src_of[u] = a.get("source", "")
            if a.get("author"):
                auth_of[u] = a["author"]
    n_uniq = len(prov_of)
    prov_counts = Counter(prov_of.values())
    covered = sum(v for k, v in prov_counts.items() if k != "none (no lede)")

    # ---- 1. articles per pool over time, colored by side (backtest Wayback vs forward WebSearch) ----
    f1 = go.Figure()
    f1.add_bar(x=[p["as_of_date"] for p in bt], y=[p["n_articles"] for p in bt],
               name="backtest tail (Wayback, biweekly)", marker_color="#1c7ed6")
    f1.add_bar(x=[p["as_of_date"] for p in fw], y=[p["n_articles"] for p in fw],
               name="forward cron (WebSearch, daily)", marker_color="#f59f00")
    f1.add_vline(x="2026-07-22", line={"dash": "dot", "color": "#888"})
    f1.update_layout(template="seaborn", height=380, barmode="group",
                     margin={"t": 20, "l": 50, "r": 20, "b": 60},
                     yaxis_title="articles in pool", legend={"orientation": "h", "y": 1.12},
                     xaxis={"title": "pool date (dotted line = backtest->forward handoff)"})

    # ---- 2. lede provenance mix across the whole bootstrap ----
    _order = ["Wayback", "WebSearch", "live-fallback", "none (no lede)"]
    _col = {"Wayback": "#2b8a3e", "WebSearch": "#f59f00", "live-fallback": "#c92a2a",
            "none (no lede)": "#adb5bd"}
    rows = [(k, prov_counts.get(k, 0)) for k in _order if prov_counts.get(k, 0)]
    f2 = go.Figure(go.Bar(x=[c for _, c in rows], y=[k for k, _ in rows], orientation="h",
                          marker_color=[_col[k] for k, _ in rows],
                          text=[f"{c} ({100*c//max(n_uniq,1)}%)" for _, c in rows], textposition="auto"))
    f2.update_layout(template="seaborn", height=260, margin={"t": 20, "l": 130, "r": 40, "b": 40},
                     xaxis_title="unique articles")

    # ---- 3. forward waves (per-article wave tag exists only on the forward side) ----
    fw_waves = Counter(w for u, w in wave_of.items() if w)
    wr = fw_waves.most_common()
    f3 = go.Figure(go.Bar(x=[c for _, c in wr], y=[w for w, _ in wr], orientation="h",
                          marker_color="#7048e8"))
    f3.update_layout(template="seaborn", height=300, margin={"t": 20, "l": 150, "r": 30, "b": 40},
                     xaxis_title="forward articles (wave tag is forward-side only)")

    # ---- 4. top source domains (both sides) ----
    sr = Counter(s for s in src_of.values() if s).most_common(15)[::-1]
    f4 = go.Figure(go.Bar(x=[c for _, c in sr], y=[s for s, _ in sr], orientation="h",
                          marker_color="#1098ad"))
    f4.update_layout(template="seaborn", height=480, margin={"t": 20, "l": 190, "r": 30, "b": 40},
                     xaxis_title="unique articles")

    # ---- 5. articles per author (top 20) ----
    ar = Counter(auth_of.values()).most_common(20)[::-1]
    f5 = go.Figure(go.Bar(x=[c for _, c in ar], y=[a[:46] for a, _ in ar], orientation="h",
                          marker_color="#e8590c"))
    f5.update_layout(template="seaborn", height=520, margin={"t": 20, "l": 250, "r": 30, "b": 40},
                     xaxis_title="unique articles")

    span = f'{pools[0]["as_of_date"]} → {pools[-1]["as_of_date"]}'

    def card(v, label):
        return (f'<div style="display:inline-block;margin:0 1.6em 0.6em 0">'
                f'<b style="font-size:1.5em;color:#0b7285">{v}</b><br>'
                f'<span style="font-size:.8em;color:#555">{label}</span></div>')
    cards = (card(f"{n_uniq:,}", "unique articles")
             + card(f"{len(bt)}+{len(fw)}", "backtest + forward pools")
             + card(span, "coverage span")
             + card(f"{100*covered//max(n_uniq,1)}%", "have a lede")
             + card(f"{100*len(auth_of)//max(n_uniq,1)}%", "have a byline"))

    html = f"""{dash_nav.render("retrieval_bootstrap.html")}
<h1 style="margin:0 0 .1em">PWR bootstrap retriever</h1>
<p style="color:#555;max-width:900px;margin:.2em 0 1em">The news-coverage bridge across the backtest&rarr;forward
handoff (2026-07-22): the last ~3 months of the backtest (GKG discovery + <b>Wayback</b> ledes, biweekly pools)
spliced onto the forward cron&#39;s daily <b>WebSearch</b> pulls. Wayback and WebSearch ledes are both
look-ahead-safe; the backtest-only <b>live-fallback</b> is the one biased provenance. Provenances are kept
distinct here (the backtest RBT collapses them).</p>
<div style="margin:0 0 1.2em">{cards}</div>
<h2 style="margin:1.4em 0 .2em">1. Article volume over time &mdash; backtest tail vs forward cron</h2>
<p style="color:#555;max-width:860px;margin:0 0 .4em">Biweekly backtest pools (top-100 ranked) then the daily
forward pulls; the 2026-07-23 spike is the cron&#39;s initial backfill, settling to ~9 new/day.</p>{_fig_html(f1)}
<h2 style="margin:1.6em 0 .2em">2. Lede provenance mix</h2>
<p style="color:#555;max-width:860px;margin:0 0 .4em">Every unique article&#39;s lede source. Wayback = backtest
tail (clean); WebSearch = forward (clean); live-fallback = backtest tail (look-ahead-biased).</p>{_fig_html(f2)}
<h2 style="margin:1.6em 0 .2em">3. Forward coverage by wave</h2>
<p style="color:#555;max-width:860px;margin:0 0 .4em">Per-article wave tags exist only on the forward side
(the cron records each article&#39;s discovering wave query).</p>{_fig_html(f3)}
<h2 style="margin:1.6em 0 .2em">4. Top source domains</h2>{_fig_html(f4)}
<h2 style="margin:1.6em 0 .2em">5. Articles per author</h2>{_fig_html(f5)}
"""
    out.write_text(html)
    print(f"wrote {out}  ({n_uniq} unique | {len(bt)} backtest + {len(fw)} forward pools | "
          f"prov {dict(prov_counts)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the PWR bootstrap retriever dashboard.")
    ap.add_argument("--canon-dir", default="data/curator_runs/gkg-3yr-canon14",
                    help="backtest run dir whose last-3mo pools form the bootstrap's historical tail")
    ap.add_argument("--forward-corpus", default="data/forward_corpus",
                    help="forward cron corpus dir (articles.jsonl)")
    ap.add_argument("--since", default="2026-04-22",
                    help="include backtest pools with as_of >= this date (the ~3-month tail)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "retrieval_bootstrap.html"))
    a = ap.parse_args()
    build(a.canon_dir, a.forward_corpus, a.since, Path(a.out))
