#!/usr/bin/env bash
# Switch star-pi's checkout (~/engram_hub) to Carter's integrated build: github.com/mrrCarter/VAYLT_Hackathon, branch main.
# Run ON the Pi:   bash ~/engram_hub/scripts/cds_update.sh
# Nothing is lost: uncommitted work on the Pi is saved to a local branch AND a patch file in ~/cds-backup/ first.
# Ignored files (.venv, console/.venv, console/models, console/certs, engram.db, ~/engram_robot) are never touched.
# Undo: the last lines it prints say exactly how to go back to what was checked out before.
set -euo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
REPO_URL=https://github.com/mrrCarter/VAYLT_Hackathon.git
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=$HOME/cds-backup/$STAMP
cd "$HUB"

before_branch=$(git rev-parse --abbrev-ref HEAD)
before_commit=$(git rev-parse --short HEAD)
echo "now: $HUB on $before_branch at $before_commit"

# 1. keep anything changed on the Pi (tracked edits, new untracked files) before switching
mkdir -p "$BACKUP"
git status --porcelain > "$BACKUP/status.txt"
if [ -s "$BACKUP/status.txt" ]; then
  echo "local changes found; saving them (list: $BACKUP/status.txt)"
  git diff HEAD > "$BACKUP/tracked-changes.patch" || true
  git ls-files --others --exclude-standard -z | tar --null -czf "$BACKUP/untracked-files.tgz" -T -
  git stash push --include-untracked -m "cds-update $STAMP" >/dev/null
  git branch "pi-local-$STAMP" "stash@{0}"   # the stash commit, as a branch that cannot expire
  echo "saved: branch pi-local-$STAMP, $BACKUP/tracked-changes.patch, $BACKUP/untracked-files.tgz"
else
  echo "no local changes"
fi

# 2. fetch Carter's main and switch to it
git remote get-url carter >/dev/null 2>&1 || git remote add carter "$REPO_URL"
git fetch carter main
git checkout -B main carter/main
echo "switched to main at $(git rev-parse --short HEAD)"

# 3. Python packages (needs internet; skipped with a warning when offline, the old ones usually still work)
if curl -s -m 5 -o /dev/null https://pypi.org/simple/; then
  [ -x .venv/bin/pip ] && .venv/bin/pip install -q -r requirements.txt || echo "WARNING: brain packages not updated"
  [ -x console/.venv/bin/pip ] && console/.venv/bin/pip install -q -r console/requirements.txt || echo "WARNING: console packages not updated"
else
  echo "WARNING: no internet; packages not updated (fine unless a service fails to start)"
fi

echo
echo "done. next: bash $HUB/scripts/cds_up.sh"
echo "to undo:  cd $HUB && git checkout $before_branch   (your saved changes: git stash list / branch pi-local-$STAMP if any)"
