#!/usr/bin/env bash
# Stop what cds_up.sh started on star-pi: brain (:8001), console (:8443 + :8000) and the Cortex sidecar.
# It does NOT stop Brian's robot server (:8200) or the model server (:8080); stop those separately if needed.
#   bash ~/engram_hub/scripts/cds_down.sh
set -uo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
pids_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
for port in 8001 8443 8000; do
  for p in $(pids_on $port); do kill "$p" 2>/dev/null && echo "stopped pid $p on :$port"; done
done
[ -f "$HUB/scripts/cortex_sidecar.sh" ] && sh "$HUB/scripts/cortex_sidecar.sh" stop 2>/dev/null
rm -f "$HOME/cds-logs/brain.mode"
echo "stopped. The robot server and the model server were left as they were."
