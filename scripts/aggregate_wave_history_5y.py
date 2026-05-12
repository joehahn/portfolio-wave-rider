"""Aggregate the 20 as-of-date news payloads into wave_history_5y.csv.

Reads each `data/news_asof_5y/<as_of_date>-news.json` (produced by the
rebuild skill's 20 parallel news-researcher Task calls) and emits a
single `data/wave_history_5y.csv` with columns matching the live
`data/wave_history.csv` schema:

    date, wave, stage, evidence_tickers, rationale, seeded

`seeded` is always False here (the rows are organic agent output, just
with strict as-of-date discipline). A 21st column `self_critique_downgrades`
records the count of model self-critique downgrades on that call, as
telemetry for whether the discipline was biting.

Writes to wave_history_5y.csv (NOT the live wave_history.csv) so the
1y backtest / live dashboard wave history are not disturbed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


IN_DIR = Path("data/news_asof_5y")
OUT_PATH = Path("data/wave_history_5y.csv")


def main() -> int:
    if not IN_DIR.is_dir():
        print(f"error: {IN_DIR} does not exist; run the rebuild skill first",
              file=sys.stderr)
        return 1

    rows: list[dict] = []
    for p in sorted(IN_DIR.glob("*-news.json")):
        try:
            payload = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"warning: skipping {p.name} ({e})", file=sys.stderr)
            continue

        as_of = payload.get("as_of_date") or payload.get("date")
        if not as_of:
            print(f"warning: skipping {p.name} (no as_of_date)", file=sys.stderr)
            continue

        downgrade_count = len(payload.get("self_critique_downgrades") or [])
        for wave, info in (payload.get("wave_stages") or {}).items():
            rows.append({
                "date": as_of,
                "wave": wave,
                "stage": info.get("stage", "neutral"),
                "evidence_tickers": ";".join(info.get("evidence_tickers") or []),
                "rationale": info.get("rationale", ""),
                "seeded": False,
                "self_critique_downgrades": downgrade_count,
            })

    if not rows:
        print("error: no rows aggregated; nothing to write", file=sys.stderr)
        return 1

    df = pd.DataFrame(rows).sort_values(["date", "wave"]).reset_index(drop=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_PATH, index=False)
    print(f"wrote {OUT_PATH} ({len(df)} rows across "
          f"{df['date'].nunique()} as-of dates)")

    # Telemetry: total downgrades caught across the rebuild.
    total_dg = df.groupby("date")["self_critique_downgrades"].first().sum()
    print(f"self-critique downgrades total: {total_dg} "
          f"(zero across all 20 calls would suggest the discipline is not biting)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
