#!/usr/bin/env bash
# Start the console. HTTPS on 8443 (needed for the iPad mic) + HTTP on 8000 (cert download + fallback).
# Both take IPv6 and IPv4 (scripts/serve_dual.py): phone hotspots often give laptops only IPv6.
# Point at teammates' services with env vars, e.g.:
#   ENGRAM_ROBOT_URL=http://127.0.0.1:8100 ENGRAM_MEMORY_URL=http://127.0.0.1:8200 ./scripts/run.sh
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] && source .venv/bin/activate
export ENGRAM_ROBOT_URL="${ENGRAM_ROBOT_URL:-mock}"
export ENGRAM_MEMORY_URL="${ENGRAM_MEMORY_URL:-mock}"
echo "robot=$ENGRAM_ROBOT_URL memory=$ENGRAM_MEMORY_URL camera=${ENGRAM_CAMERA:-robot} llm=${ENGRAM_LLM_URL:-off}"
IPS=$(hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null || echo localhost)
H=$(hostname -s 2>/dev/null || hostname)
addrs() { echo "$H.local"; for ip in $IPS; do case $ip in *:*) ;; *) echo "$ip" ;; esac; done; }   # IPv6 devices use the .local name
if [ -f certs/server.crt ]; then
  python3 scripts/serve_dual.py --port 8000 --log-level warning &
  HTTP_PID=$!
  trap "kill $HTTP_PID 2>/dev/null" EXIT
  for a in $(addrs); do echo "  iPad (mic works):  https://$a:8443   cert: http://$a:8000/ca.crt"; done
  python3 scripts/serve_dual.py --port 8443 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt --log-level warning
else
  for a in $(addrs); do echo "  iPad (typed only, run scripts/make_cert.sh for mic):  http://$a:8000"; done
  python3 scripts/serve_dual.py --port 8000 --log-level warning
fi
