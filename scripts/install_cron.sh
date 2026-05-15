#!/usr/bin/env bash
# One-shot cron installer for portfolio-wave-rider. Works on macOS and Linux.
#
# Appends a single line to the user's crontab pointing at
# scripts/cron_snapshot.sh (which in turn runs snapshot + dashboard daily
# Mon-Fri 16:30 local). Idempotent: re-running detects an existing entry
# and exits cleanly. Preserves any other entries already in your crontab.
#
# Note on macOS: cron is a legacy compatibility layer that launchd manages
# under the label com.vix.cron. If `sudo launchctl list | grep cron`
# returns nothing on your Mac, the daemon is not loaded and your entry
# won't fire. Load it once with:
#
#     sudo launchctl load -w /System/Library/LaunchDaemons/com.vix.cron.plist
#
# Modern macOS also asks you to grant Full Disk Access to /usr/sbin/cron
# in System Settings > Privacy & Security if jobs touch protected paths.
# Linux has the cron daemon running by default; no extra step needed.
#
# Caveat: standard cron is strictly time-based — if the machine is asleep
# at 16:30, the job is missed (no replay). For laptops with frequent sleep
# this means occasional missed snapshots. Use `--date YYYY-MM-DD` on
# `snapshot` to backfill if needed.
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
