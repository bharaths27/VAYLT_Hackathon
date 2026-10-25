#!/usr/bin/env bash
# Stop what cds_up.sh started on star-pi: brain (:8001), console (:8443 + :8000) and the Cortex sidecar (:8010).
# It does NOT stop Brian's robot server (:8200) or the model server (:8080); stop those separately if needed.
#   bash ~/engram_hub/scripts/cds_down.sh
set -uo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
pids_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
[ -f "$HUB/scripts/cortex_sidecar.sh" ] && sh "$HUB/scripts/cortex_sidecar.sh" stop 2>/dev/null
pids=
for port in 8001 8000 8443 8010; do
  for p in $(pids_on $port); do kill "$p" 2>/dev/null && echo "stopped pid $p on :$port" && pids="$pids $p"; done
done
for p in $pids; do   # a console streaming video to the iPad does not exit on TERM: force it after 5 s
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
  kill -9 "$p" 2>/dev/null && echo "pid $p did not exit in 5 s; killed"
done
echo "stopped. The robot server and the model server were left as they were."
