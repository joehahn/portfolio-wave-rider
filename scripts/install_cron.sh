#!/usr/bin/env bash
# One-shot cron installer for portfolio-wave-rider. Works on macOS and Linux.
#
# Installs TWO entries, preserving any other crontab lines you have:
#   1. Daily (7-day)     18:30  scripts/cron_pull.sh      -> forward news pull into the frozen corpus
#   2. Weekday (Mon-Fri) 16:30  scripts/cron_snapshot.sh -> price snapshot + dashboard + review (if due)
# The pull runs in the evening (after the US close + after-hours) to capture the full day's news; the
# weekday review reads a trailing news window, so it does not depend on the same day's pull. Idempotent:
# re-running only adds whichever entry is missing.
#
# cron only fires while the machine is awake; missed runs do not auto-replay. A missed news pull cannot
# be cleanly backfilled (re-querying WebSearch about a past day reintroduces hindsight), so a persistently-
# asleep laptop leaves gaps in the corpus. Backfill a missed price snapshot with `snapshot --date YYYY-MM-DD`.
#
# To uninstall: run `crontab -e` and delete the two lines ending in cron_pull.sh / cron_snapshot.sh.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PULL="$PROJ/scripts/cron_pull.sh"
SNAP="$PROJ/scripts/cron_snapshot.sh"
PULL_LINE=$'# PWR: Daily (7-day) forward news pull into the corpus, 18:30 local\n'"30 18 * * *  $PULL"
SNAP_LINE=$'# PWR: Weekday price snapshot + dashboard + review (if due), Mon-Fri 16:30 local\n'"30 16 * * 1-5  $SNAP"

command -v crontab >/dev/null 2>&1 || { echo "error: crontab not found. Install it via your package manager." >&2; exit 1; }
for s in "$PULL" "$SNAP"; do
  [[ -x "$s" ]] || { echo "error: $s is not executable. Run 'chmod +x $s' first." >&2; exit 1; }
done

CUR="$(crontab -l 2>/dev/null || true)"
NEW="$CUR"
changed=0
add_line() {  # add crontab line "$2" unless a line already references script path "$1"
  if ! grep -Fq "$1" <<<"$NEW"; then
    NEW="${NEW:+$NEW$'\n'}$2"
    changed=1
  fi
}
add_line "$PULL" "$PULL_LINE"
add_line "$SNAP" "$SNAP_LINE"

if [[ $changed -eq 0 ]]; then
  echo "Already installed. Current PWR crontab lines:"
  crontab -l | grep -F "$PROJ/scripts/"
  exit 0
fi

printf '%s\n' "$NEW" | crontab -
echo "Installed / updated. PWR crontab lines now:"
crontab -l | grep -F "$PROJ/scripts/"
echo
echo "Verify all entries with: crontab -l"
echo "Uninstall: run 'crontab -e' and delete the cron_pull.sh / cron_snapshot.sh lines."
