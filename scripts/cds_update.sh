#!/usr/bin/env bash
# Switch star-pi's checkout (~/engram_hub) to Carter's integrated build: github.com/mrrCarter/VAYLT_Hackathon,
# branch main, or the branch named on the command line (the demo runs from engram-assist).
# Run ON the Pi:   bash ~/engram_hub/scripts/cds_update.sh [branch]
# First time (the old checkout does not have this file yet), fetch it and run that copy:
#   cd ~/engram_hub && git fetch https://github.com/mrrCarter/VAYLT_Hackathon.git main && git show FETCH_HEAD:scripts/cds_update.sh > /tmp/cds_update.sh && bash /tmp/cds_update.sh
# or, for a branch:
#   cd ~/engram_hub && git fetch https://github.com/mrrCarter/VAYLT_Hackathon.git engram-assist && git show FETCH_HEAD:scripts/cds_update.sh > /tmp/cds_update.sh && bash /tmp/cds_update.sh engram-assist
# Nothing is lost: the commit the Pi was on is kept as branch pre-cds-<stamp>; uncommitted work on the Pi is saved to a
# branch pi-local-<stamp> AND a patch file in ~/cds-backup/<stamp>/ first; ignored files where the new branch has a
# file (checkout would replace them) are copied to ~/cds-backup/<stamp>/ignored-files.tgz.
# The venvs (.venv, console/.venv) get the branch's requirements; each one's package list is saved first
# (~/cds-backup/<stamp>/brain-packages.txt, console-packages.txt), and the undo runs packages-undo.sh from there:
# the old versions back, then whatever the update added removed.
# Other ignored files (console/models, console/certs, engram.db, ~/engram_robot) are never touched.
# Undo: the last line it prints says exactly how to go back (also kept in ~/cds-backup/<stamp>/UNDO.txt).
set -euo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
REPO_URL=https://github.com/mrrCarter/VAYLT_Hackathon.git
BRANCH=${1:-main}
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=$HOME/cds-backup/$STAMP
cd "$HUB"

# 0. the name goes into git commands: letters, digits and / - _ . only, no leading -, no .. (nothing is changed yet)
if [ $# -gt 1 ] || ! [[ $BRANCH =~ ^[A-Za-z0-9/_.-]+$ ]] || [[ $BRANCH == -* || $BRANCH == *..* ]] ||
   ! git check-ref-format --branch "$BRANCH" > /dev/null 2>&1; then
  echo "REFUSED: '$*' is not a branch name this script takes (letters, digits, / - _ . only). Nothing was changed."
  exit 2
fi

echo "now: $HUB on $(git symbolic-ref -q --short HEAD || echo 'a detached HEAD') at $(git rev-parse --short HEAD)"

# 1. fetch the branch first: if the network fails here, nothing on the Pi has changed (not even .git/config:
#    no remote is added, the fetch writes only the ref carter/<branch>)
git fetch "$REPO_URL" "+refs/heads/$BRANCH:refs/remotes/carter/$BRANCH"

# 2. keep what was checked out, the ignored files the branch would replace, and anything changed on the Pi
mkdir -p "$BACKUP"
git branch "pre-cds-$STAMP" HEAD   # the commit the Pi was on; checkout -B below may move a local branch
undo="git checkout pre-cds-$STAMP"
# Stash does not keep ignored files, and checkout silently replaces an ignored file where the branch has one (an old
# broad ignore such as models/ can hide local files there). Copy exactly those, so the undo can put them back.
git diff -z --no-renames --name-only --diff-filter=A HEAD "carter/$BRANCH" |
  while IFS= read -r -d '' f; do { [ -e "$f" ] && git check-ignore -q -- "$f" && printf '%s\0' "$f"; } || true; done \
  > "$BACKUP/ignored.list"
if [ -s "$BACKUP/ignored.list" ]; then
  tar --null -czf "$BACKUP/ignored-files.tgz" -T "$BACKUP/ignored.list"
  tr '\0' '\n' < "$BACKUP/ignored.list" > "$BACKUP/ignored-files.txt"
  undo="$undo && tar -xzf $BACKUP/ignored-files.tgz -C $HUB"
  echo "ignored files that $BRANCH replaces, copied first: $BACKUP/ignored-files.tgz (list: ignored-files.txt)"
fi
rm -f "$BACKUP/ignored.list"
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

# 3. switch to the branch
git checkout -B "$BRANCH" "carter/$BRANCH"
echo "switched to $BRANCH at $(git rev-parse --short HEAD)"

# 4. Python packages (needs internet; skipped with a warning when offline, the old ones usually still work).
#    Each venv's packages are listed first (pip freeze); the undo puts exactly that list back (packages-undo.sh).
packages() { # venv, requirements file, name
  local list=$BACKUP/$3-packages.txt fix=$BACKUP/packages-undo.sh
  [ -x "$1/bin/pip" ] || { echo "WARNING: no $1; $3 packages not updated"; return 0; }
  "$1/bin/pip" freeze > "$list" || { echo "WARNING: could not list the $3 packages; not updated"; return 0; }
  if [ ! -f "$fix" ]; then   # the undo's last step: the old versions back, then remove what the update added
    printf '#!/usr/bin/env bash\n# put the venvs back as they were before the update of %s\nset -e\ncd %q\n' "$STAMP" "$HUB" > "$fix"
    undo="$undo && bash $fix"
  fi
  printf '%q/bin/pip install -q -r %q\n%q/bin/pip freeze | grep -vxFf %q | sed "s/[ =@].*//" | xargs -r %q/bin/pip uninstall -q -y\n' \
    "$1" "$list" "$1" "$list" "$1" >> "$fix"
  echo "cd $HUB && $undo" > "$BACKUP/UNDO.txt"   # before installing, so the undo is right even if pip stops halfway
  "$1/bin/pip" install -q -r "$2" || echo "WARNING: $3 packages not updated"
}
if curl -fsI -m 5 -o /dev/null https://pypi.org/simple/pip/; then   # headers only; the full index is 48 MB
  packages .venv requirements.txt brain
  packages console/.venv console/requirements.txt console
else
  echo "WARNING: no internet; packages not updated (fine unless a service fails to start)"
fi

echo
echo "done: on $BRANCH. next: bash $HUB/scripts/cds_up.sh --restart   (--restart: services already running still run the old code)"
echo "to undo (back from $BRANCH):  cd $HUB && $undo"
