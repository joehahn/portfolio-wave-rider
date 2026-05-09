"""Rebuild wave_history.csv from the 12 as-of-date news payloads.

Run from the repo root: ``.venv/bin/python scripts/rebuild_wave_history.py``.

Each payload at data/news_asof/YYYY-MM-DD-news.json carries one wave_stages
dict (wave -> {stage, rationale, evidence_tickers}) produced by a single
news-researcher subagent call run in strict historical replay mode — the
agent saw only headlines published on or before the as-of date and was
explicitly told to suppress training-data knowledge of post-date events.

This script aggregates the 12 monthly payloads into one CSV row per
(date, wave). seeded=False because each row reflects a real-time-discipline
classification, not post-hoc backfill. Replaces the prior seed_wave_history
flow whose rows were stamped with past dates but authored using post-date
events — what quant finance calls look-ahead bias.

If a payload omits a wave (e.g., an early-2025 month with no robotics
signal), we emit a 'neutral' row so the CSV has a stable 7-row shape per
date.
"""
from __future__ import annotations
import json
import csv
from pathlib import Path

ASOF_DIR = Path("data/news_asof")
OUT_PATH = Path("data/wave_history.csv")

WAVES = ["AI", "rockets_spacecraft", "robotics", "engineered_biology",
         "quantum", "nuclear_fusion", "general_markets"]

rows = []
for f in sorted(ASOF_DIR.glob("*-news.json")):
    payload = json.loads(f.read_text())
    date = payload["date"]
    stages = payload.get("wave_stages", {})
    for wave in WAVES:
        entry = stages.get(wave)
        if entry is None:
            stage = "neutral"
            rationale = "(wave not classified in this period)"
            ev = ""
        else:
            stage = entry["stage"]
            rationale = entry["rationale"]
            ev = "|".join(entry.get("evidence_tickers", []))
        rows.append({
            "date": date,
            "wave": wave,
            "stage": stage,
            "evidence_tickers": ev,
            "rationale": rationale,
            "seeded": False,
        })

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
with OUT_PATH.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["date", "wave", "stage",
                                        "evidence_tickers", "rationale",
                                        "seeded"])
    w.writeheader()
    w.writerows(rows)

print(f"wrote {len(rows)} rows to {OUT_PATH}")
print(f"dates: {len({r['date'] for r in rows})}, waves per date: {len(WAVES)}")
