# News sources

Curated list of news sources the `news-researcher` subagent consults
first when researching a ticker. Grouped by the technology waves named
in `investor_profile.md`.

**How this is used.** For each ticker, the news-researcher picks the
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
- **Anthropic blog** — https://www.anthropic.com/news — primary source for Claude models and research.
- **OpenAI blog** — https://openai.com/news/ — primary source for GPT models and research.

## Robotics

Humanoids, industrial automation, autonomy.

- **IEEE Spectrum — Robotics** — https://spectrum.ieee.org/robotics — long-running, technical.
- **The Robot Report** — https://www.therobotreport.com — industry news, funding rounds, product launches.
- **Robotics Business Review** — https://www.roboticsbusinessreview.com — business and market coverage.

## Rockets & spacecraft

Launch, satellites, space-economy.

- **Ars Technica — Space** — https://arstechnica.com/space/ — Eric Berger's reporting is the benchmark.
- **SpaceNews** — https://spacenews.com — trade publication; deep on launch, policy, and contracts.
- **Payload** — https://payloadspace.com — newsletter-style; strong on space-economy deals.
- **NASASpaceflight** — https://www.nasaspaceflight.com — launch-operations coverage.

## Nuclear fusion

Pre-commercial; expect slow cadence and peer-reviewed results.

- **Fusion Industry Association** — https://www.fusionindustryassociation.org — trade-body briefings and state-of-industry reports.
- **Nature — Fusion** — https://www.nature.com/subjects/nuclear-fusion-and-fission — peer-reviewed milestones.
- **World Nuclear News** — https://www.world-nuclear-news.org — includes fusion alongside fission news.

## Quantum computing

Also pre-commercial but with rapid research-cadence.

- **Quantum Computing Report** — https://quantumcomputingreport.com — industry news and vendor tracker.
- **Nature — Quantum Information** — https://www.nature.com/npjqi/ — peer-reviewed results.
- **IBM Quantum blog** — https://www.ibm.com/quantum/blog — primary source for IBM's roadmap.
- **Google Quantum AI** — https://quantumai.google — primary source for Google's quantum research.

## Synthetic biology

Gene editing, engineered cells, mRNA platforms, cellular agriculture, longevity research.

- **Endpoints News** — https://endpts.com — biotech business news; deals, trials, FDA actions.
- **STAT News** — https://www.statnews.com — biotech and health reporting; strong on biotech IPOs and clinical readouts.
- **BioPharma Dive** — https://www.biopharmadive.com — pharma and biotech industry news.
- **Nature Biotechnology** — https://www.nature.com/nbt/ — peer-reviewed research and reviews.
- **SynBioBeta** — https://www.synbiobeta.com — synthetic biology industry community and conference coverage.

## General markets

For tickers that don't map cleanly to a single wave, or for macro context.

- **Bloomberg** — https://www.bloomberg.com — breaking news and markets.
- **Reuters** — https://www.reuters.com — wire-service reliability.
- **Financial Times** — https://www.ft.com — paywalled but strong on markets and macro.
- **Wall Street Journal** — https://www.wsj.com — paywalled; US-centric markets and corporate news.
- **SEC EDGAR** — https://www.sec.gov/edgar — primary source for 10-Ks, 10-Qs, 8-Ks, proxy filings.
- **Yahoo Finance — ticker news** — https://finance.yahoo.com — ticker-scoped recent headlines aggregator.
- **Zero Hedge** — https://www.zerohedge.com — contrarian macro/markets blog; often early on stories wires are slow on, but framing is editorial and signal-to-noise is mixed — treat as a cross-check, not a primary source.
