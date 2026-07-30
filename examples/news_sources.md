---
# STARTER news-source lists. Copy to the repo root and edit:
#     cp examples/news_sources.md news_sources.md
#
# A PROTOTYPE, not a source of truth. The few entries below exist to show the format and to let a
# fresh clone run end to end. Building the real lists is the work: which mills pollute your pool, and
# which desks are worth an authority bump, are things you learn by watching the retrieval dashboard.
#
# Machine-readable SOURCE lists (substring-matched against each article's source domain), read by
# scripts/gkg_pool.py. Two tiers plus a prose tier below:
#   source_block: content-farm / low-signal domains dropped from the pool entirely. These are the
#     press-release mills that republish the same wire copy under many domains; without a block list
#     they crowd out real reporting. Add a domain here whenever you see it clutter the pool.
#   source_major: broad wire services and major outlets, the ranker's MID authority tier (weight 1.5).
#     A story's salience = how many RECOGNIZED outlets carried it, which scopes salience to credible
#     coverage so a viral-but-obscure story does not dominate.
#   The SPECIALTY desks (niche, wave-specific) are the ranker's TOP tier (weight 2.0) and live in the
#     prose section below, one line each. A domain in BOTH lists is treated as MAJOR (major wins).
source_block:
  - marketbeat.com
  - 247wallst.com
source_major:
  - reuters.com
  - apnews.com
  - bloomberg.com
---

# Specialty news sources

The top authority tier, and the one worth the most effort. One line per source: the domain, then why
you trust it. Group them under the waves you named in `investor_profile.md`, and add a group whenever
you add a wave.

These are the desks that cover a beat closely enough to report a development before the wires pick it
up, which is exactly where a wave is cheapest to enter. Finding them is a research task of its own:
follow the reporters your wave's practitioners actually cite, then check whether the retrieval
dashboard shows the domain surfacing articles at all before you rely on it.

The single generic example below is a placeholder. Replace it.

## General markets

- **Reuters** — https://reuters.com — broad wire coverage, well archived, useful as a baseline.
