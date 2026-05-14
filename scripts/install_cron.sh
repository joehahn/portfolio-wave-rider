#!/usr/bin/env bash
# One-shot cron installer for portfolio-wave-rider.
#
# Computes the absolute path to scripts/cron_snapshot.sh, then appends a single
# cron line that fires it Mon-Fri 16:30 local. Idempotent: re-running is safe.
# Preserves any other entries already in your crontab (we read the existing
# crontab, check for our line, and only append if missing).
#
# To uninstall: run `crontab -e` and delete the line containing cron_snapshot.sh.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$PROJ/scripts/cron_snapshot.sh"
LINE="30 16 * * 1-5  $SCRIPT"

# Sanity: the helper has to exist and be executable.
if [[ ! -x "$SCRIPT" ]]; then
  echo "error: $SCRIPT is not executable. Run 'chmod +x $SCRIPT' first." >&2
  exit 1
fi

# Check whether our cron line is already installed.
if crontab -l 2>/dev/null | grep -Fq "$SCRIPT"; then
  echo "Already installed. Current crontab line referencing the helper:"
  crontab -l | grep -F "$SCRIPT"
  exit 0
fi

# Append our line to the existing crontab (preserves any other entries).
(crontab -l 2>/dev/null; echo "$LINE") | crontab -
echo "Installed: $LINE"
echo
echo "Verify with: crontab -l"
echo "Uninstall: run 'crontab -e' and delete the line above."
