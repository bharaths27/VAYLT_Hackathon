#!/usr/bin/env bash
# One line per ENGRAM service on star-pi: OK or DOWN, plus the addresses the iPad and laptop need.
#   bash ~/engram_hub/scripts/cds_status.sh
set -uo pipefail
LOGS=$HOME/cds-logs
code() { curl -sk -m 3 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }
row() { # name, url, what it is
  local c; c=$(code "$2")
  case $c in 2*) s="OK  " ;; *) s="DOWN" ;; esac
  printf '  %s  %-10s %-38s %s\n' "$s" "$1" "$2" "$3"
}
echo "ENGRAM on $(hostname) at $(date '+%H:%M:%S')"
row brain   http://127.0.0.1:8001/health        "plans + memory ($(cat "$LOGS/brain.mode" 2>/dev/null || echo '?') robot)"
row console https://127.0.0.1:8443/             "Verna's console (iPad)"
row cert    http://127.0.0.1:8000/ca.crt        "certificate download for the iPad"
row robot   'http://[::1]:8200/robot/state'     "Brian's robot server (hardware + safety)"
row model   http://127.0.0.1:8080/health        "Qwen3 1.7B (optional)"
row cortex  http://127.0.0.1:8010/health        "memory inspector sidecar (optional)"
stop=$(curl -s -m 3 'http://[::1]:8200/robot/state' 2>/dev/null | grep -o '"estop[^,}]*' | head -1)
[ -n "$stop" ] && echo "  robot stop latch: $stop"
echo
for ip in $(hostname -I 2>/dev/null); do
  case $ip in *:*) continue ;; esac   # the iPad uses the IPv4 address
  echo "  iPad console:      https://$ip:8443"
  echo "  iPad certificate:  http://$ip:8000/ca.crt"
done
echo "  logs: $LOGS/"
