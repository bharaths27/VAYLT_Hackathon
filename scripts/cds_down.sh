#!/usr/bin/env bash
# Stop what cds_up.sh started on star-pi: brain (:8001), console (:8443 + :8000) and the Cortex sidecar (:8010).
# It does NOT stop Brian's robot server (:8200) or the model server (:8080); stop those separately if needed.
# On those four ports it stops only this hub's ENGRAM processes (one of our scripts in the command line, or started
# from or running in ~/engram_hub); anything else is named and left alone.
#   bash ~/engram_hub/scripts/cds_down.sh
set -uo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
HUB_DIR=$(cd "$HUB" 2>/dev/null && pwd -P) || HUB_DIR=$HUB   # the hub's real path
pids_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
ours() { # pid: ENGRAM from this hub: its command line names one of our scripts or starts in the hub, or it runs in the hub
  local a; a=$(ps -ww -o args= -p "$1" 2>/dev/null) || return 1
  case $a in *scripts/serve_dual.py*|*console/scripts/run.sh*|*cortex_sidecar*|"$HUB"/*|"$HUB_DIR"/*) return 0 ;; esac
  case $(readlink -f "/proc/$1/cwd" 2>/dev/null)/ in "$HUB_DIR"/*) return 0 ;; esac
  return 1
}
[ -f "$HUB/scripts/cortex_sidecar.sh" ] && sh "$HUB/scripts/cortex_sidecar.sh" stop 2>/dev/null
pids=
for port in 8001 8000 8443 8010; do
  for p in $(pids_on $port); do
    if ours "$p"; then
      kill "$p" 2>/dev/null && echo "stopped pid $p on :$port" && pids="$pids $p"
    elif a=$(ps -ww -o args= -p "$p" 2>/dev/null); then   # (gone already: nothing to say)
      echo "port $port is held by pid $p ($a), which these scripts did not start; stop it yourself if it is an old ENGRAM copy, then run again"
    fi
  done
done
for p in $pids; do   # a console streaming video to the iPad does not exit on TERM: force it after 5 s
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
  kill -9 "$p" 2>/dev/null && echo "pid $p did not exit in 5 s; killed"
done
echo "stopped. The robot server and the model server were left as they were."
