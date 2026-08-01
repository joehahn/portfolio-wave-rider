"""The shared curator: the ONE place the curator LLM is invoked, used by both the forward loop and
the backtest so a prompt or parsing change lands on both by construction.

- Reads the single system prompt (`.claude/agents/watchlist-curator.md`) and one user-prompt template.
- Routes to the SDK: `claude-*` -> Anthropic; `vendor/model` -> OpenRouter (raw requests).
- Parses the JSON decision (trailing-comma tolerant), retries transient API + parse errors, falls back
  to no_changes.

Retrieval is DECOUPLED: the caller passes `pool_text` (the forward corpus slice OR the backtest GKG
pool). Validation/apply is also the caller's job (`portfolio.apply_curator_decisions`). This module is
purely "news pool + watchlist + thesis -> validated-shape decision dict".
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = (ROOT / ".claude" / "agents" / "watchlist-curator.md").read_text()

# The wave thesis + exclusions are NOT hardcoded here on purpose: they MUST come from investor_profile.md
# (via portfolio.load_wave_thesis / load_exclusions), so editing the profile actually changes behavior.
# curate() takes them as REQUIRED args -- a caller that forgets errors loudly instead of silently using a
# stale hardcoded thesis (the historical bug this prevents). Anchors keep a default (safe-haven trio).
DEFAULT_ANCHORS = ["SPY", "AGG", "IAU"]

# Prompt intros: the backtest keeps its EXACT wording (byte-identical prompt -> parity); the forward
# path uses a live-framed intro (no as-of discipline, since forward has no future to suppress).
_BT_INTRO = ("Backtest, article-list mode (forward-resembling: a raw list of date-clean news ARTICLES with "
             "title + snippet, like live WebSearch results — you discover the tickers and filter the noise "
             "yourself).")
LIVE_INTRO = ("Live rebalance, article-list mode: a raw list of recent news ARTICLES with title + snippet. "
              "Discover US-listed wave tickers with real catalysts and filter the noise yourself.")


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return ""


def anthropic_client():
    import anthropic
    k = _env("ANTHROPIC_API_KEY")
    if not k:
        raise SystemExit("ANTHROPIC_API_KEY empty in .env")
    return anthropic.Anthropic(api_key=k)


def _llm_complete(model: str, system: str, user: str, max_tokens: int, anthropic_cli,
                  no_reasoning: bool = False):
    """One completion, provider-agnostic. `claude-*` -> Anthropic SDK; `vendor/model` -> OpenRouter's
    OpenAI-compatible endpoint (raw requests). Returns (text, tokens_in, tokens_out)."""
    if model.startswith("claude"):
        # claude-sonnet-5 / opus etc. run EXTENDED THINKING on by default, which (a) balloons output cost
        # (thinking tokens bill as output), (b) can overflow max_tokens and truncate the JSON, and (c) makes
        # the model unfairly stronger than the OpenRouter models we run with reasoning OFF. Honor no_reasoning
        # by explicitly disabling thinking so every curator LLM is compared on the same (no-reasoning) footing.
        kwargs = {"thinking": {"type": "disabled"}} if no_reasoning else {}
        r = anthropic_cli.messages.create(model=model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": user}], **kwargs)
        txt = "".join(getattr(b, "text", "") for b in r.content).strip()
        return txt, r.usage.input_tokens, r.usage.output_tokens
    import requests
    key = _env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY empty in .env")
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if no_reasoning:
        body["reasoning"] = {"enabled": False}   # try to skip the reasoning pass (speed/cost on reasoning models)

    def _post(b):
        return requests.post("https://openrouter.ai/api/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                             json=b, timeout=120)
    resp = _post(body)
    if resp.status_code == 400 and "reasoning" in body and "reasoning" in resp.text.lower():
        # some models (e.g. gemini-3-pro, grok-4.x) make reasoning MANDATORY -> disabling it 400s; let it reason.
        body.pop("reasoning")
        resp = _post(body)
    resp.raise_for_status()
    j = resp.json()
    txt = (j["choices"][0]["message"].get("content") or "").strip()
    u = j.get("usage", {})
    return txt, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def _repair_json(block: str) -> str:
    """Repair the two ways a model breaks JSON when a string value holds prose.

    1. A literal newline/tab inside a string (it wrote markdown bullets, not \\n escapes).
    2. An unescaped double quote inside a string (it quoted a headline).

    Walk the text tracking string state. A quote inside a string is treated as CLOSING only when the
    next non-space character is one of ,:}] or end-of-text; otherwise it is escaped as content. That
    heuristic is what a human reader does, and it is only ever applied as a fallback after a plain
    json.loads has already failed."""
    out, in_str, esc = [], False, False
    for i, ch in enumerate(block):
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\" and in_str:
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                continue
            nxt = next((c for c in block[i + 1:] if not c.isspace()), "")
            if nxt in (",", ":", "}", "]", ""):
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')          # a quote inside prose
            continue
        if in_str and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        out.append(ch)
    return "".join(out)


def _try_parse(txt: str) -> "dict | None":
    """Extract the curator's JSON object, tolerating a trailing-comma emission and raw newlines inside
    string values. Side-effect-free."""
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    block = m.group(0)
    _nocomma = re.sub(r",(\s*[}\]])", r"\1", block)
    for cand in (block, _nocomma, _repair_json(block), _repair_json(_nocomma)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def format_pool(articles: list[dict]) -> str:
    """Format article dicts (forward corpus read_slice OR the backtest GKG pool) into the news_pool
    text block the curator reads. Tolerant of both schemas (snippet/full_text/lede, date variants)."""
    out = []
    for a in articles:
        d = a.get("published_date") or a.get("date") or "?"
        title = (a.get("title") or "").strip()
        src = a.get("source_domain") or a.get("source") or ""
        body = (a.get("snippet") or a.get("full_text") or a.get("lede") or "").strip()[:400]
        url = a.get("url") or ""
        out.append(f"[{d}] {title} ({src})\n{body}\n{url}")
    return "\n\n".join(out) if out else "(no articles in the trailing window)"


def build_user_prompt(as_of: str, watchlist: list[str], thesis: str, exclusions: str, max_size: int,
                      anchors: list[str], pool_text: str, cadence: str, intro: str = _BT_INTRO,
                      retry_feedback: str = "") -> str:
    # Slot rule depends on how many managed slots are FREE. A blanket "any add needs a paired remove"
    # is only true at capacity; with free slots the curator may add outright (else it never grows a
    # sub-full watchlist toward max_size).
    free = max_size - len(watchlist)
    if free > 0:
        slot_rule = (f"{free} of the {max_size} managed slots are FREE: you may ADD up to {free} name(s) "
                     f"with NO paired remove; once full, a further add needs a paired remove.")
        action_rule = ("A free slot is NOT a mandate to add: add a name only when the pool shows a clearly rising "
                       "wave vehicle with a concrete fresh catalyst, or swap a weaker current holding for a "
                       "decisively stronger name; otherwise prefer no_changes and leave the slot open.")
    else:
        slot_rule = (f"The watchlist is FULL ({max_size}/{max_size} managed slots used). A bare ADD (with no "
                     f"paired REMOVE) is INVALID and will be REJECTED and wasted — the new name will NOT enter "
                     f"the watchlist. To bring in a new name you MUST remove one current holding in the SAME "
                     f"response (add + remove = a swap). If nothing is worth displacing a holding, emit no_changes.")
        action_rule = ("Because the watchlist is FULL: to add a stronger rising-wave vehicle you MUST pair it with "
                       "the REMOVE of your weakest-conviction current holding (add + remove together). NEVER emit "
                       "an add without a paired remove here — decide explicitly whether the new name beats your "
                       "weakest holding; if yes, swap them; if no, no_changes.")
    _retry = (f"RETRY — your previous proposal was partly REJECTED by the validator:\n{retry_feedback}\n"
              f"Revise so nothing is rejected: to keep a rejected ADD, pair it with a REMOVE of a "
              f"lower-conviction current holding (a swap); drop any add/remove you can't justify; else "
              f"emit no_changes. Re-emit the FULL corrected JSON.\n\n") if retry_feedback else ""
    from src.portfolio import _VALID_WAVE_BUCKETS   # valid wave_bucket vocabulary (lazy import; avoids cycle)
    _buckets = ", ".join(sorted(b for b in _VALID_WAVE_BUCKETS if b != "general_markets"))
    _opt_note = (
        "A BACKWARD-LOOKING mean-variance optimizer sets the dollar weights downstream — it weights each name "
        "by its recent trailing risk-adjusted return, so it buys winners AFTER they run and sells losers AFTER "
        "they fall; left alone it rides a once-rising name back down. Counteract that lag: keep the watchlist "
        "stocked with names in the EARLY buildup of a wave (fresh/accelerating catalysts, pre-run-up) so the "
        "optimizer always has a better-trending alternative to rotate into as older holdings plateau. Favor "
        "new/accelerating catalysts over already-crested or fully-re-rated names. When you DO add, prefer names "
        "spanning DIFFERENT waves so several concurrent early-and-mid waves are represented. But an EMPTY SLOT is "
        "better than a WEAK NAME: the watchlist is a quality-gated candidate menu, NOT a quota to fill. The "
        "optimizer funds only the few best-trending names and leaves the rest near 0, so padding the list with "
        "marginal tickers just adds noise and risks the short-lookback optimizer chasing a fluke. Add ONLY a name "
        "with a concrete, fresh catalyst you can cite; when in doubt, leave the slot open. Remove a name only when "
        "its thesis breaks or its wave has clearly crested, NOT merely because it matured (let the optimizer "
        "defund maturing names via its weighting; do not churn them off the watchlist yourself).")
    return f"""{_retry}{intro}
- as_of_date: {as_of}
- current_watchlist: {watchlist}
- max_watchlist_size: {max_size} (managed slots; {anchors} are always_include anchors, off-limits, don't count). {slot_rule}
- rebalance_period: {cadence} (you are re-run every {cadence} — calibrate churn to this cadence; most {cadence} windows warrant no_changes, act only on a genuine catalyst)
- profile_wave_thesis: {thesis}
- wave_buckets (tag each add's wave_bucket with the single best-fitting label below; the 2026 geopolitical realignment is FOUR distinct waves — geo_defense, geo_drones, geo_tankers, geo_reconstruction — cover each as its OWN wave, not one 'geopolitical' lump): {_buckets}
- how_your_picks_are_used: {_opt_note}
- exclusions: {exclusions}

news_pool (read it, discover US-listed wave tickers with real catalysts, DISCARD the noise):
{pool_text}

{action_rule} Write rationale_overall as markdown bullets, ONE PER WAVE ('- **wave**: ...'), covering every wave the pool spoke to (including no-action ones), naming the specific tickers and facts and the noise you filtered, and closing with a '- **verdict**: ...' bullet. It is ONE JSON string: separate the bullets with \\n escapes, and use single quotes inside it -- a raw newline or a bare double quote breaks the payload. Emit ONLY the JSON object per your output schema."""


def curate(pool_text: str, watchlist: list[str], *, as_of: str, model: str, thesis: str, exclusions: str,
           anthropic_cli=None,
           max_size: int = 5, anchors: "list[str] | None" = None, cadence: str = "weekly",
           intro: str = _BT_INTRO, no_reasoning: bool = True,
           log_path: "Path | None" = None, fail_dir: "Path | None" = None, retry_feedback: str = "") -> dict:
    """Run the curator once. Returns the parsed decision dict, or a no_changes fallback after retries.
    Identical retry/parse logic for forward and backtest. `anthropic_cli` is required for claude-* models.
    `retry_feedback` (optional) prepends validator-rejection reasons so the model can re-propose a valid set."""
    anchors = anchors if anchors is not None else DEFAULT_ANCHORS
    user = build_user_prompt(as_of, watchlist, thesis, exclusions, max_size, anchors, pool_text, cadence, intro,
                             retry_feedback)
    txt = ""
    for attempt in range(2):
        ok, _uin, _uout = False, 0, 0
        for _t in range(6):
            try:
                txt, _uin, _uout = _llm_complete(model, SYSTEM_PROMPT, user, 8000, anthropic_cli, no_reasoning)
                ok = True
                break
            except Exception as _e:  # noqa: BLE001 - transient API blip; back off and retry
                _w = min(90, 5 * 2 ** _t)
                print(f"  API error {as_of} ({type(_e).__name__}): retry {_t + 1}/6 in {_w}s", file=sys.stderr)
                time.sleep(_w)
        if not ok:
            print(f"  API down for {as_of} after 6 retries -> no_changes", file=sys.stderr)
            break
        if log_path and attempt == 0:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps({"as_of": as_of, "model": model, "user": user, "response": txt,
                                            "usage": {"in": _uin, "out": _uout}}, indent=2))
        parsed = _try_parse(txt)
        if parsed is not None:
            return parsed
        print(f"  WARN {as_of}: unparseable curator JSON (attempt {attempt + 1}/2)", file=sys.stderr)
    if fail_dir:
        fail_dir.mkdir(parents=True, exist_ok=True)
        (fail_dir / f"{as_of}.txt").write_text(txt)
    return {"as_of_date": as_of, "adds": [], "removes": [], "no_changes": True}
