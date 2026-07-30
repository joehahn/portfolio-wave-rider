---
# STARTER news-source lists. Copy to the repo root and edit:
#     cp examples/news_sources.md news_sources.md
#
# Machine-readable SOURCE lists (substring-matched against each article's source domain), read by
# scripts/gkg_pool.py. Two tiers plus a prose tier below:
#   source_block: content-farm / low-signal domains dropped from the pool entirely. These are the
#     press-release mills that republish the same wire copy under many domains; without a block list
#     they crowd out real reporting.
#   source_major: broad wire services and major outlets, the ranker's MID authority tier (weight 1.5).
#     A story's salience = how many RECOGNIZED outlets carried it, which scopes salience to credible
#     coverage so a viral-but-obscure story does not dominate.
#   The SPECIALTY desks (niche, wave-specific) are the ranker's TOP tier (weight 2.0) and live in the
#     prose section below, one line each. A domain in BOTH lists is treated as MAJOR (major wins).
#
# Expect to extend all three. The block list in particular grows as you notice junk in the pool.
source_block:
  - marketbeat.com
  - tickerreport.com
  - 247wallst.com
  - insidermonkey.com
  - stockstory.org
source_major:
  - reuters.com
  - apnews.com
  - bloomberg.com
  - wsj.com
  - ft.com
  - cnbc.com
---

# Specialty news sources

The top authority tier. One line per source: the domain, then why you trust it. Group them under the
waves you named in `investor_profile.md`, and add a group whenever you add a wave. These are the desks
that cover a beat closely enough to report a development before the wires pick it up, which is exactly
where a wave is cheapest to enter.

## AI

- **SemiAnalysis** — https://semianalysis.com — deep silicon and data-center supply-chain reporting.
- **Stratechery** — https://stratechery.com — strategy analysis on the platform companies.

## Robotics

- **The Robot Report** — https://therobotreport.com — industry coverage of commercial robotics.

## Quantum

- **Quantum Computing Report** — https://quantumcomputingreport.com — tracks hardware milestones and funding.

## General markets

- **Reuters** — https://reuters.com — broad wire coverage, well archived.
