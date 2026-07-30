"""
Blind rationale-soundness judge for the curator LLMs.

WHY: every backtest metric we have (return, IR, Sharpe, Calmar, t-stat) is in-sample /
look-ahead-leaky, so none of them can honestly rank which curator LLM *reasons* best. This
script scores the QUALITY of each curator's reasoning directly, independent of what the
market later did, so the score is leak-free.

HOW (blind): for every add/remove a curator made, an independent judge LLM (Opus 4.8 by
default) sees the profile thesis + that single decision — ticker, action, rationale, cited
news evidence — with the MODEL IDENTITY STRIPPED and the price/outcome NEVER shown. It rates
the decision on a 5-point rubric (on-thesis, evidence-supports, real-catalyst, disciplined,
valid-ticker) plus a holistic 1-5. Decisions are shuffled so the judge can't infer the
author. Opus is used because it is the strongest analytical model AND is not one of the
curators being judged (curators = sonnet-5 / kimi / deepseek), so there is no self-preference.

OUT: data/curator_runs/_judge_scores.json — mean soundness + per-criterion pass rates per
curator, which scripts/build_sweep_dashboard.py renders as section 5.

Usage:
  python scripts/judge_curations.py                 # full pass, default judge (Opus 4.8)
  python scripts/judge_curations.py --limit 6       # cheap smoke test (6 sampled decisions)
  python scripts/judge_curations.py --judge claude-fable-5
"""
import argparse
import glob
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parent.parent
import statistics  # noqa: E402
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))
from src import curator  # noqa: E402  (reused for provider-agnostic, reasoning-OFF judge calls)

# curators under judgement: (display label, run dir). Order is irrelevant — decisions are pooled + shuffled.
RUNS = [   # geosplit-config runs at the canonical mws16: same pools/dates/config, only the curator LLM varies
    ("claude-sonnet-5", "data/curator_runs/proto-sonnet"),
    ("moonshotai/kimi-k2.5", "data/curator_runs/proto-mws16"),
    ("deepseek/deepseek-v4-flash", "data/curator_runs/proto-deepseek"),
    ("claude-opus-4-8", "data/curator_runs/proto-opus"),
]
# JUDGE PANEL: three NON-FAMILY judges (disjoint from every candidate's vendor -> no self/in-family preference).
# Panel score = mean of the three; dispersion = their stdev. Opus is a CROSS-CHECK reference only (in-family with
# the sonnet/opus candidates, so it is reported alongside but NOT folded into the panel mean).
PANEL_JUDGES = ["openai/gpt-5.4", "google/gemini-3.1-pro-preview", "x-ai/grok-4.5"]
CROSSCHECK_JUDGE = "claude-opus-4-8"
ALL_JUDGES = PANEL_JUDGES + [CROSSCHECK_JUDGE]
JUDGE_PRICES = {   # $/M (in, out) for the approx cost report
    "openai/gpt-5.4": (2.5, 15.0), "google/gemini-3.1-pro-preview": (2.0, 12.0),
    "x-ai/grok-4.5": (2.0, 6.0), "claude-opus-4-8": (5.0, 25.0)}
JUDGE_DEFAULT = "claude-opus-4-8"
JUDGE_PRICE = {"in": 5.0, "out": 25.0}      # $/M tokens, Opus 4.8 estimate (only used to report an approx cost)
CRITERIA = ["on_thesis", "evidence_supports", "real_catalyst", "disciplined", "valid_ticker"]
OUT = ROOT / "data" / "curator_runs" / "_judge_scores.json"
CACHE = ROOT / "data" / "curator_runs" / "_judge_cache.json"   # per-decision verdicts; makes re-runs resume-only

# compact restatement of investor_profile.md so the judge knows the waves + rules WITHOUT any market outcome.
PROFILE = """The investor rides durable multi-year "waves" — entering on the quiet BUILDUP or early surge and
TRIMMING BEFORE THE CREST. Named waves: artificial intelligence (current, ride but trim); rockets & spacecraft;
robotics; quantum computing; nuclear (fission SMRs near-term, fusion long-term); geopolitical realignment
(defense primes & ETFs, tankers/freight, Middle-East reconstruction, drones/attritable autonomy); and
aging-population demographics (healthcare, eldercare/senior-housing REITs, automation). EXCLUSIONS: solar and
wind energy. Only real, investable US-listed equities/ETFs qualify (not private names, not company names used
as tickers, not delisted shells, not keyword false-matches like RKT=Rocket Mortgage for a space thesis)."""

JUDGE_SYSTEM = f"""You are an impartial equity-research reviewer grading the SOUNDNESS OF REASONING behind a
single portfolio watchlist decision. You do NOT know the ticker's subsequent price and must NOT guess it —
judge only whether the stated reasoning and cited evidence justify the decision AT THE TIME it was made.

The investor's mandate:
{PROFILE}

You will receive ONE decision: an ADD or REMOVE of a ticker, its rationale, and the news the curator cited.
Grade it on five criteria, each 1 (pass) or 0 (fail):
- on_thesis: the ticker maps to a named wave (and is not an excluded solar/wind name).
- evidence_supports: the cited news actually substantiates the rationale (not vague, stale, or mismatched).
- real_catalyst: a concrete, material catalyst (milestone/contract/deal/structural shift), not PR-cycle noise or pure momentum.
- disciplined: consistent with "enter on buildup, trim before crest"; a REMOVE is well-justified, not churn.
- valid_ticker: a real, investable US-listed equity or ETF (not a company name, private firm, delisted shell, or keyword false-match).
Then give an overall holistic score 1-5 (5 = exemplary reasoning, 1 = unsound/careless).

Respond with ONLY a JSON object, no prose:
{{"on_thesis":0|1,"evidence_supports":0|1,"real_catalyst":0|1,"disciplined":0|1,"valid_ticker":0|1,"overall":1-5,"reason":"one short sentence"}}"""


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return ""


def _anthropic():
    import anthropic
    k = _env("ANTHROPIC_API_KEY")
    if not k:
        raise SystemExit("ANTHROPIC_API_KEY empty in .env")
    return anthropic.Anthropic(api_key=k)


def collect():
    """Pool every add/remove across all curators into one flat, model-tagged list."""
    out = []
    for label, d in RUNS:
        for f in sorted(glob.glob(str(ROOT / d / "2*-curation.json"))):
            try:
                c = json.loads(Path(f).read_text())
            except Exception:  # noqa: BLE001
                continue
            date = Path(f).name[:10]
            for a in c.get("adds", []):
                out.append({"model": label, "date": date, "action": "add", "ticker": a.get("ticker", ""),
                            "rationale": a.get("rationale", ""), "evidence": a.get("news_evidence", [])})
            for r in c.get("removes", []):
                out.append({"model": label, "date": date, "action": "remove", "ticker": r.get("ticker", ""),
                            "rationale": r.get("rationale", ""), "evidence": r.get("news_evidence", [])})
    return out


def _decision_text(dec):
    """The BLIND payload shown to the judge: no model identity, no outcome."""
    ev = "\n".join(f"  - [{e.get('date','?')}] {e.get('summary','')} ({e.get('source','')})"
                   for e in dec["evidence"]) or "  (none cited)"
    return (f"ACTION: {dec['action'].upper()} {dec['ticker']}\n"
            f"RATIONALE: {dec['rationale']}\n"
            f"CITED NEWS:\n{ev}")


def _parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if any(k not in j for k in CRITERIA):
        return None
    if "overall" not in j:   # judge occasionally omits the holistic score -> derive it from the 5 criteria (0/5->1, 5/5->5)
        j["overall"] = round(1 + 4 * sum(1 for k in CRITERIA if j.get(k)) / len(CRITERIA))
    return j


def judge_one(judge_model, dec, cli):
    """One blind judgement by `judge_model` (provider-agnostic via curator._llm_complete, reasoning OFF so
    the short verdict isn't truncated by a thinking pass). 5 tries, backoff, parse-retry.
    Returns (verdict_dict, tokens_in, tokens_out)."""
    last = None
    for t in range(5):
        try:
            txt, ti, to = curator._llm_complete(judge_model, JUDGE_SYSTEM, _decision_text(dec),
                                                2000, cli, no_reasoning=True)   # headroom: gemini/grok force reasoning
            v = _parse(txt)
            if v is not None:
                return v, ti, to
            last = (None, ti, to)   # parsed empty -> retry
        except Exception as e:  # noqa: BLE001  (overload / rate-limit / transient)
            last = (None, 0, 0)
            print(f"  ! {judge_model} error on {dec['model']} {dec['date']} {dec['ticker']}: {str(e)[:80]}")
            time.sleep(min(30, 3 * 2 ** t))
    return last or (None, 0, 0)


def _key(dec):
    return f"{dec['model']}|{dec['date']}|{dec['action']}|{dec['ticker']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="judge only N sampled decisions (smoke test)")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    decisions = collect()
    Random(42).shuffle(decisions)                        # blind ordering: author not inferable from position
    if a.limit:
        decisions = decisions[:a.limit]

    # Run every judge over every decision. Resume-friendly: cache is {judge: {dec_key: verdict}}; only the
    # uncached (judge, decision) pairs hit the API. curator._llm_complete routes claude-* -> Anthropic (cli),
    # everything else -> OpenRouter, both reasoning-OFF so the short verdict can't be truncated by a think pass.
    allc = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    cli = _anthropic()
    tin = {j: 0 for j in ALL_JUDGES}
    tout = {j: 0 for j in ALL_JUDGES}
    verdicts = {}
    for j in ALL_JUDGES:
        cache = allc.get(j, {}) if isinstance(allc.get(j), dict) else {}
        todo = [d for d in decisions if _key(d) not in cache]
        print(f"[{j}] {len(decisions)} decisions; {len(decisions) - len(todo)} cached, judging {len(todo)}...")
        if todo:
            with ThreadPoolExecutor(max_workers=a.workers) as ex:
                futs = {ex.submit(judge_one, j, d, cli): d for d in todo}
                for done, (fut, d) in enumerate(list(futs.items()), 1):
                    v, ti, to = fut.result()
                    tin[j] += ti
                    tout[j] += to
                    if v is not None:
                        cache[_key(d)] = v
                    if done % 20 == 0:
                        print(f"  [{j}] {done}/{len(todo)}")
            allc[j] = cache
            CACHE.write_text(json.dumps(allc, indent=1))
        verdicts[j] = cache

    _ov = lambda v: float(v["overall"]) if v else None   # noqa: E731

    def _mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    def _pstd(xs):
        return round(statistics.pstdev(xs), 3) if len(xs) > 1 else 0.0

    # panel overall + dispersion per decision = mean / stdev across the panel judges that returned a verdict
    pov, pdisp = {}, {}
    for d in decisions:
        k = _key(d)
        pj = [x for x in (_ov(verdicts[j].get(k)) for j in PANEL_JUDGES) if x is not None]
        if pj:
            pov[k], pdisp[k] = sum(pj) / len(pj), _pstd(pj)

    # per-candidate aggregation (panel mean + dispersion + per-judge + Opus cross-check + panel-averaged criteria)
    models = {}
    for d in decisions:
        k = _key(d)
        if k not in pov:
            continue
        m = models.setdefault(d["model"], {"n": 0, "panel": [], "disp": [], "add": [], "rem": [], "cross": [],
                                           "pj": {j: [] for j in ALL_JUDGES}, **{c: [] for c in CRITERIA}})
        m["n"] += 1
        m["panel"].append(pov[k])
        m["disp"].append(pdisp[k])
        (m["add"] if d["action"] == "add" else m["rem"]).append(pov[k])
        for j in ALL_JUDGES:
            x = _ov(verdicts[j].get(k))
            if x is not None:
                m["pj"][j].append(x)
        cx = _ov(verdicts[CROSSCHECK_JUDGE].get(k))
        if cx is not None:
            m["cross"].append(cx)
        for c in CRITERIA:                               # share of PANEL judges passing c, averaged over decisions
            passes = [1.0 if (verdicts[j].get(k) or {}).get(c) else 0.0
                      for j in PANEL_JUDGES if verdicts[j].get(k) is not None]
            if passes:
                m[c].append(sum(passes) / len(passes))

    summary = {}
    for label, m in models.items():
        summary[label] = {"n": m["n"], "mean_overall": _mean(m["panel"]), "dispersion": _mean(m["disp"]),
                          "add_mean": _mean(m["add"]), "rem_mean": _mean(m["rem"]),
                          "crosscheck_overall": _mean(m["cross"]),
                          "per_judge": {j: _mean(m["pj"][j]) for j in ALL_JUDGES},
                          **{c: _mean(m[c]) for c in CRITERIA}}

    panel_rank = [l for l, _ in sorted(summary.items(), key=lambda kv: -(kv[1]["mean_overall"] or -9))]
    opus_rank = [l for l, _ in sorted(summary.items(), key=lambda kv: -(kv[1]["crosscheck_overall"] or -9))]
    ex_rows = sorted(({"model": d["model"], "date": d["date"], "action": d["action"], "ticker": d["ticker"],
                       "overall": round(pov[_key(d)], 2)} for d in decisions if _key(d) in pov),
                     key=lambda x: x["overall"])[:12]
    cost = sum((tin[j] * JUDGE_PRICES.get(j, (0, 0))[0] + tout[j] * JUDGE_PRICES.get(j, (0, 0))[1]) / 1e6
               for j in ALL_JUDGES)

    payload = {"built": datetime.now().strftime("%Y-%m-%d %H:%M"), "panel_judges": PANEL_JUDGES,
               "crosscheck_judge": CROSSCHECK_JUDGE, "n_decisions": sum(m["n"] for m in models.values()),
               "cost_usd": round(cost, 2), "criteria": CRITERIA, "models": summary,
               "panel_rank": panel_rank, "crosscheck_rank": opus_rank, "examples": ex_rows}
    OUT.write_text(json.dumps(payload, indent=1))

    print(f"\nwrote {OUT}  (this-run cost ~${cost:.2f})")
    print("panel = mean of " + ", ".join(j.split("/")[-1] for j in PANEL_JUDGES) + "; ± = stdev across them:")
    for label in panel_rank:
        s = summary[label]
        pj = "  ".join(f"{j.split('/')[-1]}={s['per_judge'][j]}" for j in PANEL_JUDGES)
        print(f"  {label.split('/')[-1]:20s} panel {s['mean_overall']} ±{s['dispersion']}  (n={s['n']})  "
              f"opus_x={s['crosscheck_overall']}   [{pj}]")
    print(f"panel rank: {[l.split('/')[-1] for l in panel_rank]}")
    print(f"opus  rank: {[l.split('/')[-1] for l in opus_rank]}   (cross-check: do the neutral panel + Opus agree?)")


if __name__ == "__main__":
    main()
