"""Shared cross-page nav for the PWR dashboards -- the SINGLE source of truth, rendered by EVERY
dashboard (the scripts/ builders import this module directly; src/portfolio.py's build_dashboard and
build_curator_dashboard import it via portfolio._nav). One README link plus two grouped families:

  - Backtest    : the historical GKG + Wayback replay -- Retriever -> Curator -> parameter Sweeps.
  - Forwardtest : the live out-of-sample test -- Retriever (WebSearch corpus) -> Curator.

Adding a dashboard = one line in the relevant group; it appears in every page's nav. (pool_browser is
intentionally absent: it's reachable only from the Curator Backtest DB's intro paragraph. The Forwardtest
dashboard IS index.html, i.e. the site landing page -- the old real-holdings page it replaced is retired.)
"""
from datetime import datetime

README = ("https://github.com/joehahn/portfolio-wave-rider/blob/main/README.md", "README")
BACKTEST = [
    ("retrieval_pwr.html", "Retriever"),
    ("backtest_gkg_3yr_kimi.html", "Curator"),
    ("sweep_pwr.html", "Sweeps"),
]
FORWARDTEST = [
    ("index.html", "Dashboard"),
]
BOOTSTRAP = [
    ("retrieval_bootstrap.html", "Retriever"),
    ("curator_bootstrap.html", "Curator"),
]


def _link(href, name, current):
    return f"<b>{name}</b>" if href == current else f'<a href="{href}">{name}</a>'


def _group(pages, current):
    return " &middot; ".join(_link(h, n, current) for h, n in pages)


def render(current: str = "", built: bool = True) -> str:
    """An HTML <nav>: README, then the Backtest and Forwardtest groups. The page whose bare filename
    matches `current` is bold, not a link (so a reader sees which page they're on)."""
    ts = (f'<span style="float:right;color:#aaa;font-weight:normal;">built '
          f'{datetime.now().strftime("%Y-%m-%d %H:%M")}</span>') if built else ""
    return (
        '<nav style="font-size:13px;color:#555;margin:0 0 1.2em;padding-bottom:.5em;'
        'border-bottom:1px solid #eee;line-height:1.9;">'
        f'{ts}'
        f'<div>{_link(*README, current)}</div>'
        f'<div><span style="color:#999;">Backtest</span> &nbsp;&nbsp;{_group(BACKTEST, current)}</div>'
        f'<div><span style="color:#999;">Bootstrap</span> &nbsp;&nbsp;{_group(BOOTSTRAP, current)}</div>'
        f'<div><span style="color:#999;">Forwardtest</span> &nbsp;&nbsp;{_group(FORWARDTEST, current)}</div>'
        '</nav>')
