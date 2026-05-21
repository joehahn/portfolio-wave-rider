#!/bin/bash
# Run the three optimizer-parameter sweeps over the 5y curator runs dir.
# Each is a pure replay (no LLM calls), takes a few seconds, and renders
# its own docs/sweep_<param>.html.
set -e
cd "$(dirname "$0")/.."

for param in risk_aversion lookback concentration_cap; do
  echo "=== sweep: $param ==="
  .venv/bin/python scripts/sweep.py --param "$param"
done

echo "All three sweep pages written to docs/sweep_*.html"
