---
# Machine-readable SOURCE lists (substring-matched against each article's source domain), read by
# scripts/gkg_pool.py. This file is tracked (public), unlike investor_profile.md, so they ship with
# the repo.
#   source_block: content-farm / low-signal domains dropped from the GKG pool. APPLIED by the gather
#     (broad wave beats, minus these domains). (GHR calls this mill_block.)
#   source_major: broad wire services / major outlets — the ranker's MID authority tier (weight 1.5,
#     blue in the retrieval dashboard). A story's salience = how many RECOGNIZED outlets carried it
#     (major + specialty), which scopes salience to credible coverage so viral-but-obscure stories
#     don't dominate; the representative URL is preferred from a recognized outlet (well-archived).
# The SPECIALTY desks (niche, wave-specific — the ranker's TOP tier, weight 2.0, green in the dashboard)
# are the prose "Specialty news sources" section below: their single home + rationale, read by the live
# WebSearch curator and parsed by the ranker. A domain in BOTH lists is treated as MAJOR (major wins),
# so the broad wires stay 1.5 even though the prose also lists them for the live curator's convenience.
# Recognized = source_major (major) + specialty prose (minus any that are also major).
source_block:
  - marketbeat.com
  - tickerreport.com
  - defenseworld.net
  - americanbankingnews.com
  - dakotafinancialnews.com
  - wkrb13.com
  - modernreaders.com
  - thelincolnianonline.com
  - themarketsdaily.com
  - transcript-daily.com
  - etfdailynews.com
  - insidermonkey.com
  - 247wallst.com
  - stockstory.org
  - ts2.tech
  - pr-inside.com
  - financialnewsmedia.com
  # added 2026-07-20 from live-curator citation audit: listicle mills / algo-forecast farms / broker promo
  - fool.com
  - money.usnews.com
  - investorplace.com
  - stockinvest.us
  - tradingkey.com
  - top1markets.com
  - fxleaders.com
  - phemex.com
  - xtb.com
  - stocktwits.com
  - globalxetfs.com          # ETF-issuer self-PR
  - tech-insider.org
  - thedefensenews.com        # copycat of the real defensenews.com
  # added 2026-07-21 from curator-DB attribution review: PR-wire / syndication aggregators (tmcnet also
  # recycles URLs, which broke our Wayback join); low-signal republishers, cited adds were ~breakeven
  - tmcnet.com
  - bignewsnetwork.com
  # added 2026-07-22 from forward-corpus review: German financial portals republishing English wire / PR
  - ad-hoc-news.de
  - finanznachrichten.de
source_major:
  - reuters.com
  - apnews.com
  - bloomberg.com
  - cnbc.com
  - wsj.com
  - ft.com
  - nytimes.com
  - washingtonpost.com
  - marketwatch.com
  - barrons.com
  - forbes.com
  - businessinsider.com
  - fortune.com
  - investors.com
  - seekingalpha.com
  - benzinga.com
  - theverge.com
  - techcrunch.com
  - axios.com
  - cnn.com
  - bbc.com
  - bbc.co.uk
  - theguardian.com
  - npr.org
  # NOTE: yahoo.com was briefly promoted to major (2026-07-20) but that flooded the pool — as major it
  # jumped from ~2% to ~35% of articles (it's a huge wire-reposting aggregator), crowding out specialty
  # desks. Reverted to TAIL 2026-07-21: it still gathers + can be selected, just not privileged (1.0).
  # added 2026-07-20 from live-curator citation audit: reputable outlets/portals
  - morningstar.com
  - investing.com
  - nasdaq.com
  - coindesk.com
  - nbcnews.com
  - aljazeera.com
  # added 2026-07-21: DEMOTED from specialty (still listed in the General-markets prose below as a
  # live-curator hint) to major — its own note flags mixed signal-to-noise; a cross-check, not a 2.0 desk
  - zerohedge.com
---

# Specialty news sources

Curated list of **specialty** news desks the `watchlist-curator` subagent
consults first when researching tickers each rebalance period. These are the
niche, wave-specific desks that carry the early/deep coverage — the top
authority tier (the retrieval dashboard colors them **green**, weight 2.0). The
broad **major wires** (Reuters, AP, Bloomberg, CNBC, WSJ, FT…) are the separate
`source_major` front-matter list above (the mid tier, **blue**, weight 1.5) —
consult them freely; any wire that also appears below is treated as major, not
specialty. Grouped by the technology waves named in `investor_profile.md`, in
the profile's order: the current wave (AI) first, then the next waves in
nearest-term-impact order, with general markets last as a catch-all.

**How this is used.** For each ticker the curator is considering (an
add candidate, a current holding, or a potential remove), it picks the
most relevant wave bucket (or `general_markets`) and tries the curated
sources there first via `WebSearch` scoped to their domains. If the
curated search returns nothing material for that ticker in the lookback
window, the agent falls back to open `WebSearch`.

**Maintenance.** This list is a preferred list, not an exclusive one.
Add sources when you find useful ones; remove sources that go dark,
paywall heavily, or drift off-topic. Edit freely — no code depends on
the exact URLs.

---

## AI

For LLM / ML platform / semiconductor coverage.

- **Stratechery** — https://stratechery.com — Ben Thompson on tech strategy; slow but high-signal on AI platform economics.
- **The Information** — https://www.theinformation.com — paywalled original reporting on AI labs, deals, and leadership.
- **SemiAnalysis** — https://www.semianalysis.com — Dylan Patel on semiconductors, data-center economics, AI compute supply.
- **MIT Technology Review** — https://www.technologyreview.com — AI section is editorially strong, less churn than news wires.
- **Ars Technica — AI** — https://arstechnica.com/ai/ — technical but accessible.
- **Gary Marcus** — https://garymarcus.substack.com — *skeptic.* Neuroscientist and prominent LLM-hype critic; deflationary takes on AGI timelines and AI-capex returns. Useful contrarian foil to the cheerleader outlets above.

## Rockets & spacecraft

Launch, satellites, space-economy.

- **Ars Technica — Space** — https://arstechnica.com/space/ — Eric Berger's reporting is the benchmark.
- **SpaceNews** — https://spacenews.com — trade publication; deep on launch, policy, and contracts.
- **Payload** — https://payloadspace.com — newsletter-style; strong on space-economy deals.
- **NASASpaceflight** — https://www.nasaspaceflight.com — launch-operations coverage.

## Robotics

Humanoids, industrial automation, autonomy.

- **IEEE Spectrum — Robotics** — https://spectrum.ieee.org/robotics — long-running, technical.
- **The Robot Report** — https://www.therobotreport.com — industry news, funding rounds, product launches.
- **Robotics Business Review** — https://www.roboticsbusinessreview.com — business and market coverage.

## Engineered biology

Gene editing, engineered cells, mRNA platforms, cellular agriculture, longevity research. (Sometimes called synthetic biology.)

- **Endpoints News** — https://endpts.com — biotech business news; deals, trials, FDA actions.
- **STAT News** — https://www.statnews.com — biotech and health reporting; strong on biotech IPOs and clinical readouts.
- **BioPharma Dive** — https://www.biopharmadive.com — pharma and biotech industry news.
- **Nature Biotechnology** — https://www.nature.com/nbt/ — peer-reviewed research and reviews.
- **SynBioBeta** — https://www.synbiobeta.com — synthetic biology industry community and conference coverage.
- **In the Pipeline (Derek Lowe)** — https://www.science.org/blogs/pipeline — *skeptic.* Pharma medicinal chemist with two decades of rigorous, skeptical biotech analysis. The best skeptic outlet on this wave; flags clinical-trial design issues and over-hyped mechanisms.

## Quantum computing

Pre-commercial but with rapid research-cadence.

- **Quantum Computing Report** — https://quantumcomputingreport.com — industry news and vendor tracker.
- **Nature — Quantum Information** — https://www.nature.com/npjqi/ — peer-reviewed results.
- **Shtetl-Optimized (Scott Aaronson)** — https://scottaaronson.blog — *skeptic.* UT Austin computer scientist; the most prominent and rigorous public quantum-hype skeptic. Calls out vendor claims point-by-point and distinguishes peer-reviewed milestones from press-release theater.

## Nuclear

Covers fission (uranium, utilities, small modular reactors, the AI-data-center electricity narrative) and fusion (still pre-commercial; expect slow cadence and peer-reviewed results).

- **World Nuclear News** — https://www.world-nuclear-news.org — fission and fusion industry news, regulatory filings, PPAs.
- **Fusion Industry Association** — https://www.fusionindustryassociation.org — trade-body briefings and state-of-industry reports.
- **Nature — Fusion** — https://www.nature.com/subjects/nuclear-fusion-and-fission — peer-reviewed milestones.
- **IEA** — https://www.iea.org — International Energy Agency; authoritative primary source on electricity demand/supply, feeding the nuclear/SMR AI-data-center-power narrative.

## Defense / rearmament

Military procurement, defense budgets, the primes, and defense ETFs. Covers the geopolitical "defense and rearmament" expression in `investor_profile.md`: US budget growth, NATO and European rearmament, and Gulf procurement (where the investable exposure is the US and EU exporters, not the buyer states).

- **Breaking Defense** — https://breakingdefense.com — US procurement, budget, and program reporting.
- **Defense News** — https://www.defensenews.com — global defense-industry trade press; contracts and budgets.
- **Janes** — https://www.janes.com — defense-intelligence reference on programs, orders, and order-of-battle.
- **War on the Rocks** — https://warontherocks.com — strategy and policy analysis; useful for reading where budgets are heading.
- **Reuters — Aerospace & Defense** — https://www.reuters.com/business/aerospace-defense/ — wire-service coverage of primes' earnings and contract awards.
- **Project On Government Oversight (POGO)** — https://www.pogo.org — *skeptic.* Defense-spending watchdog; rigorous on cost overruns, failed programs, and procurement waste. The credibility-weighted foil to the trade press above, useful for spotting when a re-rated prime is priced for execution it may not deliver.

## General markets

For tickers that don't map cleanly to a single wave, or for macro context.

- **Bloomberg** — https://www.bloomberg.com — breaking news and markets.
- **Reuters** — https://www.reuters.com — wire-service reliability.
- **Financial Times** — https://www.ft.com — paywalled but strong on markets and macro.
- **Wall Street Journal** — https://www.wsj.com — paywalled; US-centric markets and corporate news.
- **SEC EDGAR** — https://www.sec.gov/edgar — primary source for 10-Ks, 10-Qs, 8-Ks, proxy filings.
- **Yahoo Finance — ticker news** — https://finance.yahoo.com — ticker-scoped recent headlines aggregator.
- **FT Alphaville** — https://www.ft.com/alphaville — *skeptic.* Rigorous, skeptical markets commentary; Bryce Elder's columns are especially good for accounting-irregularity and valuation-skepticism framing. The credibility-weighted contrarian foil to the wire-service primary sources above.
- **Zero Hedge** — https://www.zerohedge.com — contrarian macro/markets blog; often early on stories wires are slow on, but framing is editorial and signal-to-noise is mixed — treat as a cross-check, not a primary source.
- **ETF Trends** — https://www.etftrends.com — ETF-industry desk; flows, launches, and thematic-ETF coverage (useful for the wave-ETF vehicles).
- **Freight Perspectives** — https://www.freightperspectives.com — shipping/freight trade desk; rates and capacity for the tankers/shipping (geopolitical) wave.
- **McKnight's Senior Living** — https://www.mcknightsseniorliving.com — senior-housing/eldercare trade desk for the aging-population-demographics wave.
