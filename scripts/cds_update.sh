#!/usr/bin/env bash
# Switch star-pi's checkout (~/engram_hub) to Carter's integrated build: github.com/mrrCarter/VAYLT_Hackathon, branch main.
# Run ON the Pi:   bash ~/engram_hub/scripts/cds_update.sh
# First time (the old checkout does not have this file yet), fetch it and run that copy:
#   cd ~/engram_hub && git fetch https://github.com/mrrCarter/VAYLT_Hackathon.git main && git show FETCH_HEAD:scripts/cds_update.sh > /tmp/cds_update.sh && bash /tmp/cds_update.sh
# Nothing is lost: the commit the Pi was on is kept as branch pre-cds-<stamp>, and uncommitted work on the Pi is saved
# to a branch pi-local-<stamp> AND a patch file in ~/cds-backup/<stamp>/ first.
# Ignored files (.venv, console/.venv, console/models, console/certs, engram.db, ~/engram_robot) are never touched.
# Undo: the last line it prints says exactly how to go back (also kept in ~/cds-backup/<stamp>/UNDO.txt).
set -euo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
REPO_URL=https://github.com/mrrCarter/VAYLT_Hackathon.git
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=$HOME/cds-backup/$STAMP
cd "$HUB"

echo "now: $HUB on $(git symbolic-ref -q --short HEAD || echo 'a detached HEAD') at $(git rev-parse --short HEAD)"

# 1. fetch Carter's main first: if the network fails here, nothing on the Pi has changed (not even .git/config:
#    no remote is added, the fetch writes only the ref carter/main)
git fetch "$REPO_URL" "+refs/heads/main:refs/remotes/carter/main"

# 2. keep what was checked out, and anything changed on the Pi (tracked edits, new untracked files)
mkdir -p "$BACKUP"
git branch "pre-cds-$STAMP" HEAD   # the commit the Pi was on; checkout -B below may move a local main
undo="git checkout pre-cds-$STAMP"
git status --porcelain > "$BACKUP/status.txt"
if [ -s "$BACKUP/status.txt" ]; then
  echo "local changes found; saving them (list: $BACKUP/status.txt)"
  git diff --binary HEAD > "$BACKUP/tracked-changes.patch" || true
  saved="branch pi-local-$STAMP, $BACKUP/tracked-changes.patch"
  if [ -n "$(git ls-files --others --exclude-standard)" ]; then   # only when there are untracked files to keep
    git ls-files --others --exclude-standard -z | tar --null -czf "$BACKUP/untracked-files.tgz" -T -
    saved="$saved, $BACKUP/untracked-files.tgz"
  fi
  git stash push --include-untracked -m "cds-update $STAMP" >/dev/null
  git branch "pi-local-$STAMP" "stash@{0}"   # the stash commit, as a branch that cannot expire
  undo="$undo && git stash apply --index pi-local-$STAMP"
  echo "saved: $saved"
else
  echo "no local changes"
fi
echo "cd $HUB && $undo" > "$BACKUP/UNDO.txt"

# 3. switch to Carter's main
git checkout -B main carter/main
echo "switched to main at $(git rev-parse --short HEAD)"

# 4. Python packages (needs internet; skipped with a warning when offline, the old ones usually still work)
if curl -fsI -m 5 -o /dev/null https://pypi.org/simple/pip/; then   # headers only; the full index is 48 MB
  [ -x .venv/bin/pip ] && .venv/bin/pip install -q -r requirements.txt || echo "WARNING: brain packages not updated"
  [ -x console/.venv/bin/pip ] && console/.venv/bin/pip install -q -r console/requirements.txt || echo "WARNING: console packages not updated"
else
  echo "WARNING: no internet; packages not updated (fine unless a service fails to start)"
fi

echo
echo "done. next: bash $HUB/scripts/cds_up.sh --restart   (--restart: services already running still run the old code)"
echo "to undo:  cd $HUB && $undo"
