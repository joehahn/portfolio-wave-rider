"""Chronological "post-date events" timeline for the 5y backfill.

Used by the rebuild skill to template the prompt's training-suppression
list per as-of date. For an as-of date D, the events relevant to the
prompt are those with date strictly greater than D — events that have
"not yet happened" from the model's stand-in-the-shoes-of-D perspective.

Filtered to events 2020 through 2026 since the 5y backfill window spans
2021-Q3 through 2026-Q2. Earlier events are common training-data
ground truth and don't need suppression for any in-window as-of date.
"""
from __future__ import annotations
from datetime import date


# Each entry: (event_date as YYYY-MM-DD, short description).
# Keep descriptions concrete and unambiguous so the model's
# suppression instructions are precise. Order chronologically.
POST_DATE_EVENTS: list[tuple[str, str]] = [
    # 2020 — pre-window background, suppressed only for very early as-of dates
    ("2020-03-11", "WHO declares COVID-19 a pandemic; March 2020 market crash"),
    ("2020-03-15", "Federal Reserve emergency QE and rate cut to zero"),
    ("2020-11-09", "Pfizer/BioNTech vaccine efficacy announcement; reflation trade begins"),
    # 2021
    ("2021-01-28", "GameStop short squeeze peak"),
    ("2021-04-14", "Coinbase direct listing; crypto enters mainstream institutional flows"),
    ("2021-08-25", "Rocket Lab (RKLB) lists via SPAC merger"),
    ("2021-11-10", "U.S. CPI hits 6.8%; inflation acknowledged as not-transitory"),
    ("2021-11-26", "Omicron variant emerges; equity drawdown into year-end"),
    # 2022
    ("2022-02-24", "Russia invades Ukraine; commodities spike, defense names rally"),
    ("2022-03-16", "Federal Reserve begins hiking cycle (first 25bp)"),
    ("2022-05-12", "Luna/Terra stablecoin collapse; crypto contagion"),
    ("2022-06-15", "Fed delivers first 75bp hike"),
    ("2022-09-23", "UK gilt crisis; LDI unwind"),
    ("2022-11-08", "FTX collapse begins"),
    ("2022-11-30", "ChatGPT launches; public AI inflection point"),
    # 2023
    ("2023-01-23", "Microsoft expands OpenAI partnership ($10B+ reported)"),
    ("2023-03-10", "Silicon Valley Bank fails; First Republic / Signature follow"),
    ("2023-03-14", "GPT-4 released"),
    ("2023-05-24", "Nvidia guides Q2 revenue at $11B; data-center surge begins"),
    ("2023-07-12", "Threads launches; Meta META rebrand context"),
    ("2023-11-17", "OpenAI board fires Altman; reinstated five days later"),
    # 2024
    ("2024-01-08", "Spot Bitcoin ETF approved by SEC"),
    ("2024-01-24", "NUKZ ETF (nuclear-energy thematic basket) launches"),
    ("2024-02-15", "OpenAI Sora video-generation demo"),
    ("2024-03-18", "Nvidia GB200 announcement; data-center capex narrative compounds"),
    ("2024-06-10", "Apple Intelligence announced at WWDC"),
    ("2024-08-05", "Yen carry-trade unwind; brief equity-vol spike"),
    ("2024-09-20", "Microsoft–Constellation Three Mile Island PPA"),
    ("2024-10-15", "AWS–Talen Susquehanna nuclear PPA"),
    ("2024-11-05", "Trump re-elected"),
    ("2024-12-15", "Nvidia Blackwell ships at scale"),
    # 2025
    ("2025-01-20", "DeepSeek R1 release; AI-capex efficiency questions, brief tech sell-off"),
    ("2025-02-03", "Tariff announcement; equity-vol spike"),
    ("2025-04-15", "Nuclear-utility tickers break out (NUKZ +30% in six weeks)"),
    ("2025-07-30", "Q2-25 hyperscaler capex prints; capex flagged as overbuilding risk"),
    ("2025-10-12", "Robotaxi commercial launch broadens"),
    ("2025-12-01", "Nvidia FY26 capex guidance"),
    # 2026 (through the project's current date 2026-05-12)
    ("2026-02-15", "AI agents commercial adoption inflection"),
    ("2026-04-30", "Latest as-of-date pilot rebuild news payload (1y backtest input)"),
]


def events_after(as_of_date: str) -> list[str]:
    """Return human-readable event bullets dated strictly after as_of_date.

    Output is a list of strings ready to be joined with newlines and
    inserted into the prompt's {post_date_events} placeholder. Each
    string is preformatted as a bullet ('  - description (YYYY-MM-DD)').
    """
    cutoff = date.fromisoformat(as_of_date)
    return [
        f"  - {desc} ({d})"
        for d, desc in POST_DATE_EVENTS
        if date.fromisoformat(d) > cutoff
    ]


# Quarterly as-of dates for the 5y backfill window. Quarter-end dates,
# the natural anchor for a quarterly-rebalance backtest. The earliest
# is 2021-09-30 (so the backtest start of 2021-10-01 has ~1.3y of
# price history available); the latest is the last quarter-end before
# the project's current date. Update if shifting the window.
ASOF_DATES_5Y: list[str] = [
    "2021-09-30", "2021-12-31",
    "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31",
    "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
    "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
    "2026-03-31", "2026-04-30",
]

# Monthly cadence: last business day of each month from 2021-09 through
# 2026-04 (56 dates). Used by the per-ticker per-month rebuild that
# tests the hypothesis that wave-level quarterly classifications were
# too coarse to capture news signal.
ASOF_DATES_5Y_MONTHLY: list[str] = [
    "2021-09-30", "2021-10-29", "2021-11-30", "2021-12-31",
    "2022-01-31", "2022-02-28", "2022-03-31", "2022-04-29",
    "2022-05-31", "2022-06-30", "2022-07-29", "2022-08-31",
    "2022-09-30", "2022-10-31", "2022-11-30", "2022-12-30",
    "2023-01-31", "2023-02-28", "2023-03-31", "2023-04-28",
    "2023-05-31", "2023-06-30", "2023-07-31", "2023-08-31",
    "2023-09-29", "2023-10-31", "2023-11-30", "2023-12-29",
    "2024-01-31", "2024-02-29", "2024-03-29", "2024-04-30",
    "2024-05-31", "2024-06-28", "2024-07-31", "2024-08-30",
    "2024-09-30", "2024-10-31", "2024-11-29", "2024-12-31",
    "2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30",
    "2025-05-30", "2025-06-30", "2025-07-31", "2025-08-29",
    "2025-09-30", "2025-10-31", "2025-11-28", "2025-12-31",
    "2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30",
]


if __name__ == "__main__":
    # Quick sanity dump: how many events suppress for each as-of date.
    for d in ASOF_DATES_5Y:
        n = len(events_after(d))
        print(f"{d}: {n:>2} events to suppress")
