#!/usr/bin/env python3
"""Curator-MODEL sweep: re-curate the geosplit pools at the CANONICAL config for a given curator model,
writing to data/curator_runs/<out_name>/. Companion to sweep_mws_proto.py (which sweeps max_watchlist_size);
here the watchlist size is fixed at the profile's canonical value and the MODEL is the swept dimension. Every
other input (pools, dates, thesis, exclusions, anchors, no_reasoning) is held identical to proto-mws{N}, so
the only variable is the curator LLM.

Feeds scripts/judge_curations.py (blind Opus soundness judge, the PRIMARY ranking) and BTS section 13
(agreement / valid-JSON / cost, secondary).

Usage:
  python scripts/sweep_curator_model.py claude-sonnet-5 proto-sonnet
  python scripts/sweep_curator_model.py deepseek/deepseek-v4-flash proto-deepseek
  python scripts/sweep_curator_model.py claude-sonnet-5 proto-sonnet --limit 2   # cheap smoke test

claude-* models route through ANTHROPIC_API_KEY (curator.anthropic_client); everything else via
OPENROUTER_API_KEY. kimi's canonical run already exists as proto-mws16, so it is NOT re-fired here.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import curator, portfolio  # noqa: E402

CANON = ROOT / "data" / "curator_runs" / "gkg-3yr-geosplit"   # same geo-split titles pools proto-mws{N} uses


def _titles(arts):
    """Identical look-ahead-clean pool rendering as sweep_mws_proto._titles (archived Wayback lede else title;
    biased live ledes ignored) so the model sweep sees byte-identical prompts to the mws sweep."""
    lines = ["DATE-CLEAN NEWS ARTICLES (title + snippet). Discover the tickers; discard non-investable noise "
             "(war/weather events, private cos, foreign/OTC, keyword false-matches):"]
    for a in arts:
        snip = (a.get("lede") or a.get("title", ""))[:220]
        lines.append(f"\n[{a.get('date', '')} | {a.get('source', '')}] {a.get('title', '')[:90]}\n"
                     f"   {snip} ({a.get('url', '')})")
    return "\n".join(lines)


def main() -> int:
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    model, out_name = pos[0], pos[1]
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

    fm = portfolio.load_financial_model()
    anchors = [t.upper() for t in (fm.get("always_include") or [])]
    ms = int(fm["max_watchlist_size"])                     # canonical watchlist size (16), NOT swept here
    thesis = portfolio.load_wave_thesis()
    excl = portfolio.load_exclusions()
    starter = fm["starter_watchlist"]
    cadence = fm["rebalance_period"]
    cli = curator.anthropic_client() if model.startswith("claude") else None   # required for claude-* models

    st = json.loads((CANON / "_starter.json").read_text())
    dates = st["as_of_dates"][:limit] if limit else st["as_of_dates"]
    pools = {d: json.loads((CANON / f"{d}-pool.json").read_text()).get("articles", []) for d in dates}

    RUN = ROOT / "data" / "curator_runs" / out_name
    RUN.mkdir(parents=True, exist_ok=True)
    for f in list(RUN.glob("*-curation.json")):
        f.unlink()
    _st = dict(st)
    _st["max_watchlist_size"] = ms
    _st["curator_model"] = model
    (RUN / "_starter.json").write_text(json.dumps(_st, indent=2))
    hist = RUN / "_wf_history.csv"
    hist.write_text("date,action,ticker,wave_bucket,rationale,news_evidence_urls\n")
    hold = RUN / "_wf_holdings.csv"
    hold.write_text("ticker,shares\n" + "".join(f"{t},0\n" for t in starter + anchors))
    authors = {a["url"]: a["author"] for arts in pools.values() for a in arts
               if a.get("url") and (a.get("author") or "").strip()}
    (RUN / "_authors.json").write_text(json.dumps(authors, indent=1))

    for i, d in enumerate(dates):
        wl = portfolio.reconstruct_watchlist_at(d, starter, str(hist))
        ptext = _titles(pools[d])
        cur = curator.curate(ptext, wl, as_of=d, model=model, max_size=ms, anchors=anchors,
                             thesis=thesis, exclusions=excl, cadence=cadence, intro=curator.LIVE_INTRO,
                             no_reasoning=True, anthropic_cli=cli)
        _nret = 0
        for _ in range(2):        # reject-and-retry, same discipline as the other curation paths
            chk = portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
                  profile_path="investor_profile.md", listing_check=False, as_of_date=d,
                  max_watchlist_size=ms, dry_run=True)
            rej = chk.get("rejections") or []
            if not rej:
                break
            fb = "\n".join(f"- {x.get('ticker')} ({x.get('action')}): {x.get('reason')}" for x in rej)
            cur = curator.curate(ptext, wl, as_of=d, model=model, max_size=ms, anchors=anchors,
                                 thesis=thesis, exclusions=excl, cadence=cadence, intro=curator.LIVE_INTRO,
                                 no_reasoning=True, anthropic_cli=cli, retry_feedback=fb)
            _nret += 1
        cur["_retries"] = _nret
        cur["as_of_date"] = d
        (RUN / f"{d}-curation.json").write_text(json.dumps(cur, indent=2))
        portfolio.apply_curator_decisions(cur, holdings_path=str(hold), history_path=str(hist),
              profile_path="investor_profile.md", listing_check=False, as_of_date=d, max_watchlist_size=ms)
        if (i + 1) % 5 == 0:
            print(f"{out_name} [{model}]: {i + 1}/{len(dates)}", file=sys.stderr, flush=True)
    print(f"{out_name} [{model}]: DONE {len(dates)} dates -> {RUN}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
