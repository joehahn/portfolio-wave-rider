---
name: init-profile
description: Interview the user about their long-term investing goals, risk tolerance, constraints, and exclusions, then write investor_profile.md at the repo root. Run this once before invoking any other skill.
---

# /init-profile

Purpose: build the single source of truth that every other skill and
subagent in this project will consult — `investor_profile.md` at the
repo root.

## Before you start

1. Check whether `investor_profile.md` already exists. If it does, ask
   the user whether to overwrite it (`"Your existing profile will be
   replaced. Proceed?"`). If no, stop.
2. If `investor_profile.md` is absent or the user consented to overwrite,
   proceed.

## The interview

Ask the user, using `AskUserQuestion`, one cluster at a time. Keep it
short; don't interrogate. Default where you can so the user can press
through quickly.

Cluster 1 — **Horizon and goals**
- "What's your investing horizon in years?"
- "What are the 1-3 goals this portfolio funds? (retirement, college,
  home purchase, etc. — short prose is fine)"

Cluster 2 — **Risk and return**
- "Risk tolerance: conservative, moderate, or aggressive?"
- "What annualized return are you targeting? (typical range: 4%–10%)"
- "What's the worst peak-to-trough drawdown you could stomach without
  panic-selling?"

Cluster 3 — **Account and tax context**
- "Is this account taxable, tax-advantaged (IRA/401k), or mixed?"
- "Concentration cap for any single position (e.g. 25%)?"
- "Rebalance cadence: quarterly, semiannual, annual, or threshold-driven?"

Cluster 4 — **Exclusions and preferences**
- "Any sectors or themes to exclude (tobacco, defense, fossil fuels)?"
- "Any hard rules (no crypto, no options, no margin, US-only, etc.)?"
- "Rough asset-class targets (equities / bonds / cash)?"

Cluster 5 — **Strategy & beliefs** (freeform)
- "In 3-5 sentences, describe your investing philosophy or beliefs that
  should shape recommendations."

## Write the profile

Compose `investor_profile.md` with:

- YAML frontmatter: numeric + enum fields from clusters 1-4.
- `# Goals`: prose from cluster 1.
- `# Strategy & beliefs`: prose from cluster 5.
- `# Hard constraints`: machine-checkable rules from clusters 3-4.
- `# Soft preferences`: rule-of-thumb items from cluster 4.
- `# Notes for Claude`: copy the conflict-handling paragraph from the
  existing template so it survives the rewrite.

Use `Write` to create the file. After writing, show the user a summary
and ask: "Anything to adjust?"

## What you must NOT do

- Do not save the profile silently — always show the final content to
  the user before writing it.
- Do not fill in defaults for fields the user explicitly skipped — leave
  them out, and note in the output that they were skipped.
- Do not invoke any subagent. This skill only interviews and writes.
