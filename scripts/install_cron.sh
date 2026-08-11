#!/usr/bin/env bash
# One-shot cron installer for portfolio-wave-rider. Works on macOS and Linux.
#
# Installs THREE entries, preserving any other crontab lines you have:
#   1. Every day         18:30  scripts/news_pull.sh       -> news pull into the frozen corpus (incl. weekends) + RBS/RFT dashboards
#   2. Weekday (Mon-Fri) 16:30  scripts/price_snapshot.sh  -> price snapshot + CBS/CFT dashboards (CFT is docs/index.html)
#   3. Weekday (Mon-Fri) 19:00  scripts/review_curation.sh -> biweekly CBS + CFT curation + report (self-gated, --if-due)
# Only entry 3 calls the curator LLM; the dashboard refreshes in 1 and 2 are render-only.
# The pull runs in the evening (after the US close + after-hours) to capture the full day's news; the 19:00
# review runs after that pull so it curates on same-day news. Idempotent: re-running only adds whichever
# entry is missing.
#
# cron only fires while the machine is awake; missed runs do not auto-replay. A missed news pull cannot
# be cleanly backfilled (re-querying WebSearch about a past day reintroduces hindsight), so a persistently-
# asleep laptop leaves gaps in the corpus. Backfill a missed price snapshot with `snapshot --date YYYY-MM-DD`.
#
# To uninstall: run `crontab -e` and delete the lines ending in news_pull.sh / price_snapshot.sh / review_curation.sh.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PULL="$PROJ/scripts/news_pull.sh"
SNAP="$PROJ/scripts/price_snapshot.sh"
REVIEW="$PROJ/scripts/review_curation.sh"
PULL_LINE=$'# PWR: Forward news pull + RBS/RFT dashboard refreshes, every day (incl. weekends) 18:30 local\n'"30 18 * * *  $PULL"
SNAP_LINE=$'# PWR: Weekday price snapshot + CBS/CFT dashboard refreshes, Mon-Fri 16:30 local\n'"30 16 * * 1-5  $SNAP"
REVIEW_LINE=$'# PWR: Biweekly CBS + CFT curation + report (self-gated by rebalance_period), Mon-Fri 19:00 local\n'"0 19 * * 1-5  $REVIEW"

command -v crontab >/dev/null 2>&1 || { echo "error: crontab not found. Install it via your package manager." >&2; exit 1; }
for s in "$PULL" "$SNAP" "$REVIEW"; do
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
add_line "$REVIEW" "$REVIEW_LINE"

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
echo "Uninstall: run 'crontab -e' and delete the news_pull.sh / price_snapshot.sh / review_curation.sh lines."
