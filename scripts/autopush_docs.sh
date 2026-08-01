#!/bin/bash
# Stop-hook auto-push for doc and dashboard changes.
# Scope: README.md and any *.md at repo root, docs/*.html, and the curation reports
# (data/reports/*-curation.md, which the dashboards link on GitHub).
# Skips .claude/, src/, scripts/, data/, etc.
# Silent if nothing matches. Exits non-zero only on git failure.

set -e
cd "$(dirname "$0")/.."

shopt -s nullglob

# Stage matching paths. Skip gitignored files (e.g., investor_profile.md).
# git add is a no-op on unchanged files.
staged=0
for f in *.md docs/*.html data/reports/*-curation.md; do
  [ -e "$f" ] || continue
  git check-ignore -q -- "$f" && continue
  git add -- "$f"
  staged=1
done

[ "$staged" = "0" ] && exit 0

# Nothing to commit if all staged paths were already up to date.
if git diff --cached --quiet; then
  exit 0
fi

git commit -m "Auto-push doc/dashboard changes" >/dev/null
git push origin main >/dev/null 2>&1
