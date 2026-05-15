#!/usr/bin/env bash
# One-shot cron installer for portfolio-wave-rider. Works on macOS and Linux.
#
# Appends a single line to the user's crontab pointing at
# scripts/cron_snapshot.sh (which in turn runs snapshot + dashboard daily
# Mon-Fri 16:30 local). Idempotent: re-running detects an existing entry
# and exits cleanly. Preserves any other entries already in your crontab.
#
# cron only fires while the machine is awake; missed runs do not auto-replay.
# Use `--date YYYY-MM-DD` on `snapshot` to backfill a missed day.
#
# To uninstall: run `crontab -e` and delete the line containing
# cron_snapshot.sh.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$PROJ/scripts/cron_snapshot.sh"
LINE="30 16 * * 1-5  $SCRIPT"

if [[ ! -x "$SCRIPT" ]]; then
  echo "error: $SCRIPT is not executable. Run 'chmod +x $SCRIPT' first." >&2
  exit 1
fi

if ! command -v crontab >/dev/null 2>&1; then
  echo "error: crontab not found. Install it via your package manager." >&2
  exit 1
fi

if crontab -l 2>/dev/null | grep -Fq "$SCRIPT"; then
  echo "Already installed. Current crontab line referencing the helper:"
  crontab -l | grep -F "$SCRIPT"
  exit 0
fi

(crontab -l 2>/dev/null; echo "$LINE") | crontab -
echo "Installed: $LINE"
echo
echo "Verify with: crontab -l"
echo "Uninstall: run 'crontab -e' and delete the line above."
