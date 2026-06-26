"""Extract the WebSearch queries a curator agent actually ran, from its transcript.

Provenance for the curator's news searches: rather than trust the agent's
self-reported `search_terms`, read the real WebSearch tool calls out of its
transcript (the ground-truth tool-call log). The /review-portfolio and
/run-backtest skills call this after a curator agent returns, to populate
`search_terms` in `curator_latest.json` (live) or each `<date>-curation.json`
(backtest). Per-agent attribution works even when backtest agents run in
parallel, because each agent has its own transcript file.

Usage:
  python scripts/extract_search_terms.py <transcript>
      -> prints a JSON list of the queries, in run order

  python scripts/extract_search_terms.py <transcript> --into <curation.json>
      -> writes that list into the JSON's "search_terms". If no queries can be
         extracted (transcript missing/unparseable), it leaves any existing
         self-reported search_terms in place (graceful fallback).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def extract(transcript_path: str) -> list[str]:
    """Walk the transcript JSONL and collect every WebSearch tool-call query,
    in the order they appear. Robust to unknown nesting and malformed lines."""
    queries: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, dict):
            if o.get("name") == "WebSearch" and isinstance(o.get("input"), dict):
                q = o["input"].get("query")
                if isinstance(q, str) and q.strip():
                    queries.append(q)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    p = Path(transcript_path)
    if not p.exists():
        return queries
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            walk(json.loads(line))
        except Exception:  # noqa: BLE001 - skip any non-JSON / partial line
            continue
    return queries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", help="path to the agent transcript (.output JSONL)")
    ap.add_argument("--into", default=None,
                    help="curation JSON to set search_terms in (live or backtest)")
    args = ap.parse_args(argv)

    queries = extract(args.transcript)

    if not args.into:
        print(json.dumps(queries, indent=2))
        return 0

    target = Path(args.into)
    if not target.exists():
        print(f"target not found: {target}", file=sys.stderr)
        return 1
    d = json.loads(target.read_text())
    if queries:
        d["search_terms"] = queries
        target.write_text(json.dumps(d, indent=2))
        print(f"{target.name}: wrote {len(queries)} search_terms")
    else:
        kept = len(d.get("search_terms") or [])
        print(f"{target.name}: no queries extracted; kept {kept} self-reported",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
