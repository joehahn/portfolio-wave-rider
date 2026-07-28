#!/usr/bin/env python3
"""Forward-test DB -> docs/forward_test.html.

The forward analog of the curator BACKTEST (docs/backtest_gkg_3yr_kimi.html): instead of replaying the
curator over 3 years of PAST news, it runs the LIVE curator over the FORWARD corpus (the one-time
GKG+Wayback seed + the daily WebSearch pulls) at each weekly rebalance in a trailing window, then replays
the resulting curations through the SAME mean-variance engine (portfolio.curator_backtest) and renders
with the SAME dashboard (portfolio.build_curator_dashboard).

Why this matters: the backtest is in-sample (the LLM may have memorized which 2022-2025 tickers later won).
This is out-of-sample -- July-2026-onward news the model could not have known when trained -- so it is the
only clean check on the curator's edge. It is thin now (~3 weekly rebalances over the 21-day seed) and
becomes meaningful only as months accrue.

Rolling window: the run window is the trailing WINDOW_DAYS ending today. As the daily cron adds WebSearch
days on the RIGHT, the oldest seed rebalances age off the LEFT, so the corpus visibly transitions from
GKG+Wayback seed to live WebSearch over time.

Idempotent + cron-ready: only dates without a saved curation JSON are (re)curated (each costs one kimi
call); everything downstream is a free math replay + render. Run:  python scripts/build_forward_test.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import corpus, curator, portfolio   # noqa: E402

RUN_DIR = ROOT / "data" / "curator_runs" / "forward-test"
OUT_DIR = ROOT / "data" / "forward_test"
DASH = ROOT / "docs" / "forward_test.html"


def _weekly_dates(inception: date, end: date) -> list[date]:
    """Weekly rebalance dates on a fixed grid anchored at `inception`, up to `end`. Anchored (not
    end - 7k) so the grid is STABLE across daily cron runs -> cached curations are reused and only a
    genuinely new week costs a curator call. GROWING window: dates accumulate from inception, so the
    forward-test performance builds a real track record rather than scrolling old rebalances off."""
    ds = []
    d = inception
    while d <= end:
        ds.append(d)
        d += timedelta(days=7)
    return ds


def _evolve(watchlist: list[str], decision: dict, max_size: int) -> list[str]:
    """Apply a curation to the running watchlist so the NEXT curate sees the right current state.
    (portfolio.curator_backtest does the authoritative replay; this only feeds the curate prompt.)"""
    wl = list(watchlist)
    for r in decision.get("removes") or []:
        t = str(r.get("ticker", "")).upper()
        if t in wl:
            wl.remove(t)
    for a in decision.get("adds") or []:
        t = str(a.get("ticker", "")).upper()
        if t and t not in wl and len(wl) < max_size:
            wl.append(t)
    return wl


def main() -> int:
    fw = portfolio.load_forward_config()
    fm = portfolio.load_financial_model()
    starter = fm["starter_watchlist"]
    initial_usd = fm["initial_investment_usd"]
    anchors = [t.upper() for t in fm.get("always_include", [])]
    max_size = int(fm["max_watchlist_size"])
    model = fw["curator_model"]
    news_lb = int(fw["news_lookback_days"])
    end = date.today()
    dates = _weekly_dates(date.fromisoformat(fw["inception_date"]), end)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    # _starter.json: the run config curator_backtest replays against (rolling trailing window).
    (RUN_DIR / "_starter.json").write_text(json.dumps({
        "starter_watchlist": starter,
        "as_of_dates": [d.isoformat() for d in dates],
        "start_date": dates[0].isoformat(),
        "end_date": end.isoformat(),
        "rebalance_period": fm["rebalance_period"],
        "initial_usd": initial_usd,
        "lookback_years": fm["optimizer_lookback_days"] / 365.0,
        "max_watchlist_size": max_size,
        "news_lookback_days": news_lb,
    }, indent=2))

    # Curate each rebalance over the forward corpus (idempotent: skip dates already curated).
    cli_a = curator.anthropic_client() if model.startswith("claude") else None
    watchlist = [t for t in starter if t not in anchors]
    n_curated = 0
    for d in dates:
        as_of = d.isoformat()
        cj = RUN_DIR / f"{as_of}-curation.json"
        pool = corpus.read_slice(as_of, news_lb)
        if cj.exists():
            decision = json.loads(cj.read_text())
        else:
            decision = curator.curate(
                curator.format_pool(pool), watchlist, as_of=as_of, model=model, anthropic_cli=cli_a,
                thesis=portfolio.load_wave_thesis(), exclusions=portfolio.load_exclusions(),
                max_size=max_size, anchors=anchors, cadence=fm["rebalance_period"],
                intro=curator.LIVE_INTRO, no_reasoning=True,
                log_path=RUN_DIR / f"{as_of}-curator.json", fail_dir=RUN_DIR / "_parse_fail")
            cj.write_text(json.dumps(decision, indent=2))
            n_curated += 1
            print(f"  curated {as_of}: {len(pool)} articles read, "
                  f"+{[a.get('ticker') for a in decision.get('adds') or []]} "
                  f"-{[r.get('ticker') for r in decision.get('removes') or []]}", file=sys.stderr)
        watchlist = _evolve(watchlist, decision, max_size)

    # Replay through the SAME engine as the backtest (free math), then render the SAME dashboard.
    res = portfolio.curator_backtest(
        runs_dir=str(RUN_DIR), out_dir=str(OUT_DIR), max_weight=fm["concentration_cap"],
        risk_aversion=fm["risk_aversion"], risk_free_rate=fm["risk_free_rate"], benchmarks=["SPY"],
        lookback_years_override=fm["optimizer_lookback_days"] / 365.0, always_include=anchors)
    # Just framing (NOT the parameter list -- those are in the Parameter-settings table above plot 1).
    config_note = ("FORWARD (out-of-sample) test on the live corpus: a curator replay on genuinely-"
                   "unknowable news. Thin over ~3 weeks; meaningful only as months accrue.")
    portfolio.build_curator_dashboard(
        backtest_dir=str(OUT_DIR), runs_dir=str(RUN_DIR), out_path=str(DASH),
        benchmarks=["SPY"], config_note=config_note)

    # Relabel the shared (backtest-flavored) renderer output for the forward context. The renderer itself
    # stays backtest-only -- NO forward/backtest switch inside it; all forward-specific wording lives HERE,
    # in the dedicated forward builder. Portfolio value already runs from the July-1 inception, and the
    # rightmost date advances to `end` on each refresh. (As the forward DB diverges -- seed-vs-WebSearch
    # provenance on the gains-per-source/keyword panels, etc. -- this grows into a full dedicated renderer.)
    html = DASH.read_text()
    for _old, _new in (
            ("<h1>Curator Backtest ", "<h1>Curator Forwardtest "),
            ("<title>Curator Backtest</title>", "<title>Curator Forwardtest</title>"),
            ("Backtest window", "Forward-test window"),
            # pool browser is a backtest-only artifact -> drop its intro-paragraph link on the forward page.
            ("; browse them in the <a href=\"pool_browser.html\">pool browser</a>", "")):
        html = html.replace(_old, _new)   # targeted (h1 tag / full title) so the nav-link labels stay intact
    DASH.write_text(html)

    print(f"\nforward test: {len(dates)} weekly rebalances {dates[0]}..{dates[-1]} "
          f"({n_curated} newly curated)  |  realized {res['realized_return']*100:+.1f}% "
          f"vs SPY {res['benchmark_returns']['SPY']*100:+.1f}%  |  final {res['final_watchlist']}")
    print(f"wrote {DASH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
