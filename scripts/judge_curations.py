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

# curators under judgement: (display label, run dir). Order is irrelevant — decisions are pooled + shuffled.
RUNS = [
    ("claude-sonnet-5", "data/curator_runs/gkg-2yr-weekly"),
    ("moonshotai/kimi-k2.5", "data/curator_runs/gkg-3yr-kimi"),
    ("deepseek/deepseek-v4-flash", "data/curator_runs/gkg-3yr-deepseek"),
]
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


def judge_one(cli, judge_model, dec):
    """One blind judgement (5 tries, exponential backoff on API/overload errors, parse-retry).
    Returns (verdict_dict, tokens_in, tokens_out)."""
    last = None
    for t in range(5):
        try:
            r = cli.messages.create(model=judge_model, max_tokens=400, system=JUDGE_SYSTEM,
                                    messages=[{"role": "user", "content": _decision_text(dec)}])
            txt = "".join(getattr(b, "text", "") for b in r.content).strip()
            v = _parse(txt)
            if v is not None:
                return v, r.usage.input_tokens, r.usage.output_tokens
            last = (None, r.usage.input_tokens, r.usage.output_tokens)   # parsed empty -> retry
        except Exception as e:  # noqa: BLE001  (overload / rate-limit / transient)
            last = (None, 0, 0)
            print(f"  ! judge error on {dec['model']} {dec['date']} {dec['ticker']}: {str(e)[:80]}")
            time.sleep(min(30, 3 * 2 ** t))
    return last or (None, 0, 0)


def _key(dec):
    return f"{dec['model']}|{dec['date']}|{dec['action']}|{dec['ticker']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default=JUDGE_DEFAULT, help="judge model id (Anthropic)")
    ap.add_argument("--limit", type=int, default=0, help="judge only N sampled decisions (smoke test)")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    decisions = collect()
    Random(42).shuffle(decisions)                        # blind ordering: author not inferable from position
    if a.limit:
        decisions = decisions[:a.limit]

    # resume: reuse cached verdicts (keyed by model|date|action|ticker), only call the API for the rest.
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    cache = cache.get(a.judge, {}) if isinstance(cache.get(a.judge), dict) else {}
    todo = [d for d in decisions if _key(d) not in cache]
    print(f"{len(decisions)} decisions; {len(decisions)-len(todo)} cached, judging {len(todo)} with "
          f"{a.judge} ({a.workers} workers)...")

    cli = _anthropic() if todo else None
    tin = tout = 0
    if todo:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(judge_one, cli, a.judge, d): d for d in todo}
            done = 0
            for fut, d in list(futs.items()):
                v, ti, to = fut.result()
                tin += ti
                tout += to
                if v is not None:
                    cache[_key(d)] = v
                done += 1
                if done % 15 == 0:
                    print(f"  {done}/{len(todo)} judged")
        allc = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        allc[a.judge] = cache
        CACHE.write_text(json.dumps(allc, indent=1))
    results = [cache.get(_key(d)) for d in decisions]

    # aggregate per curator
    models = {}
    for dec, v in zip(decisions, results):
        if v is None:
            continue
        m = models.setdefault(dec["model"], {"n": 0, "overall": [], "add": [], "rem": [],
                                             **{k: [] for k in CRITERIA}})
        m["n"] += 1
        ov = float(v["overall"])
        m["overall"].append(ov)
        (m["add"] if dec["action"] == "add" else m["rem"]).append(ov)
        for k in CRITERIA:
            m[k].append(1.0 if v.get(k) else 0.0)

    def _mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    summary = {}
    for label, m in models.items():
        summary[label] = {"n": m["n"], "mean_overall": _mean(m["overall"]),
                          "add_mean": _mean(m["add"]), "rem_mean": _mean(m["rem"]),
                          **{k: _mean(m[k]) for k in CRITERIA}}

    # a few example verdicts for the dashboard (lowest-scoring first — the interesting failures)
    ex_rows = sorted(
        [{"model": d["model"], "date": d["date"], "action": d["action"], "ticker": d["ticker"],
          "overall": v["overall"], "reason": v.get("reason", "")}
         for d, v in zip(decisions, results) if v is not None],
        key=lambda x: x["overall"])
    # full-batch cost estimate: scale this run's per-call token average to all judged decisions (stable
    # across resume runs, which only re-pay for the uncached tail).
    n_ok = sum(m["n"] for m in models.values())
    if todo:
        per = (tin * JUDGE_PRICE["in"] + tout * JUDGE_PRICE["out"]) / 1e6 / len(todo)
        cost = per * n_ok
    else:  # fully cached re-run: carry the last full-batch cost estimate forward instead of showing $0
        cost = json.loads(OUT.read_text()).get("cost_usd", 0.0) if OUT.exists() else 0.0

    payload = {"judge_model": a.judge, "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "n_decisions": sum(m["n"] for m in models.values()), "n_failed": results.count(None),
               "cost_usd": round(cost, 2), "tokens_in": tin, "tokens_out": tout,
               "criteria": CRITERIA, "models": summary, "examples": ex_rows[:12]}
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"\nwrote {OUT}  (cost ~${cost:.2f}, {results.count(None)} failed)")
    for label, s in sorted(summary.items(), key=lambda kv: -(kv[1]["mean_overall"] or 0)):
        print(f"  {label:32s} overall {s['mean_overall']}  (n={s['n']})  "
              + " ".join(f"{k[:4]}={s[k]}" for k in CRITERIA))


if __name__ == "__main__":
    main()
