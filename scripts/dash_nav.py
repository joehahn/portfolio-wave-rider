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
    ("retrieval_forward.html", "Retriever"),
    ("index.html", "Curator"),
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


SITE = "https://jmh-datasciences.com"
REPO = "https://github.com/joehahn/portfolio-wave-rider"


def footer() -> str:
    """The shared page footer: who built this, where to find them, how it may be reused, and the
    not-investment-advice line. ONE source of truth, same as the nav above it.

    It exists because `docs/` is CC BY 4.0. These pages are meant to be screenshotted into other
    people's slides, so the attribution condition has to ride ON the artifact; someone lifting a
    chart never opens the README where the license actually lives.

    The disclaimer scopes its claim to BACKTESTED figures deliberately. A blanket "every number here
    is a hindsight upper bound" would be false on the forwardtest pages, which are the one clean
    out-of-sample read in this project, and this footer renders on those pages too."""
    a = 'style="color:inherit;text-decoration:underline;"'
    return (
        '<footer data-pwr-footer style="font-size:12px;line-height:1.75;color:var(--text2,#777);'
        'border-top:1px solid var(--line,#e2e2e2);margin-top:40px;padding:14px 0 24px;">'
        f'Built by <a href="{SITE}" {a}>Joseph M. Hahn, Ph.D. &mdash; JMH DataSciences</a>'
        f' &middot; <a href="{REPO}" {a}>portfolio-wave-rider on GitHub</a><br>'
        f'This page is <a href="{REPO}/blob/main/LICENSE-docs.md" {a}>CC BY 4.0</a> &mdash; reuse it, '
        f'including commercially, with attribution to Joseph M. Hahn, {SITE}<br>'
        '<b>Not investment advice.</b> Research output. Backtested figures are hindsight upper '
        'bounds, not realized return.'
        '</footer>')


def stamp(html: str) -> str:
    """Insert `footer()` just before </body>. IDEMPOTENT: a page already carrying the footer comes
    back untouched, so re-stamping a built page, or a builder that routes through here twice, cannot
    duplicate it. Every dashboard writer passes its output through this on the way to disk."""
    if "data-pwr-footer" in html:
        return html
    if "</body>" not in html:
        return html + footer()
    return html.replace("</body>", footer() + "</body>", 1)
