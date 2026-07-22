"""Shared cross-page nav for the PWR dashboards.

The dashboard set is growing (a forward family + a backtest family), so instead of ad-hoc per-page
links, every dashboard renders THIS one grouped strip. Adding a new dashboard = one line in the
relevant group below, and it appears in every page's nav.

Two families:
  - FORWARD  : the live loop (real prices, WebSearch corpus) -> the truth going forward.
  - BACKTEST : the historical GKG + Wayback replay -> the (in-sample) evidence it was built on.
"""
from datetime import datetime

FORWARD = [
    ("index.html", "Live portfolio"),
    ("corpus_pwr.html", "News corpus"),
    # future forward pages slot in here, e.g.:
    # ("forward_curator.html", "Curator"),
    # ("forward_sweep.html", "Sweep"),
]
BACKTEST = [
    ("retrieval_pwr.html", "Retriever (GKG+Wayback)"),
    ("backtest_gkg_3yr_kimi.html", "Curator"),
    ("sweep_pwr.html", "Sweep + LLM judge"),
    ("pool_browser.html", "Pool browser"),
]


def _links(pages, current):
    out = []
    for href, name in pages:
        out.append(f"<b>{name}</b>" if href == current else f'<a href="{href}">{name}</a>')
    return " &middot; ".join(out)


def render(current: str, built: bool = True) -> str:
    """An HTML <nav> with the Forward and Backtest groups; the current page is bold, not a link."""
    ts = (f'<span style="float:right;color:#aaa;font-weight:normal;">built '
          f'{datetime.now().strftime("%Y-%m-%d %H:%M")}</span>') if built else ""
    return (
        '<nav style="font-size:13px;color:#555;margin:0 0 1.2em;padding-bottom:.5em;'
        'border-bottom:1px solid #eee;">'
        f'{ts}'
        f'<span style="color:#999;">FORWARD</span> &nbsp;{_links(FORWARD, current)}'
        '&nbsp;&nbsp;&nbsp;<span style="color:#ccc;">|</span>&nbsp;&nbsp;&nbsp;'
        f'<span style="color:#999;">BACKTEST</span> &nbsp;{_links(BACKTEST, current)}'
        '</nav>')
