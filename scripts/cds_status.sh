#!/usr/bin/env bash
# One line per ENGRAM service on star-pi: OK or DOWN, plus the addresses the iPad and laptop need.
#   bash ~/engram_hub/scripts/cds_status.sh
set -uo pipefail
HUB=${ENGRAM_HUB:-$HOME/engram_hub}
LOGS=$HOME/cds-logs
code() { curl -sk -m 3 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }
field() { curl -sk -m 3 "$1" 2>/dev/null | grep -o "\"$2\":[^,}]*" | head -1 | cut -d: -f2- | tr -d '"'; }   # one JSON value
row() { # name, url, what it is
  local c; c=$(code "$2")
  case $c in 2*) s="OK  " ;; *) s="DOWN" ;; esac
  printf '  %s  %-10s %-38s %s\n' "$s" "$1" "$2" "$3"
}
# Brian's server binds 0.0.0.0 (IPv4) by default, or :: (IPv6 only): show the address that answers
ROBOT= rdesc="Brian's robot server (hardware + safety)"
for u in http://127.0.0.1:8200 'http://[::1]:8200'; do case $(code "$u/robot/state") in 2*) ROBOT=$u; break ;; esac; done
[ -n "$ROBOT" ] || rdesc="$rdesc; nothing on [::1]:8200 either"
# what the brain and the console really use, asked from them (not from what cds_up.sh meant to start)
brain=$(field http://127.0.0.1:8001/health robot)
case $brain in
  mock) brain="mock robot" ;;
  brian8200) brain="real robot"; [ "$(field http://127.0.0.1:8001/robot/state connected)" = true ] || brain="real robot, NOT reaching it" ;;
  '') brain="?" ;;
esac
view=$(field http://127.0.0.1:8000/status robot_url)
case $view in mock) view=SIMULATOR ;; '') view="?" ;; *) view="LIVE $view" ;; esac

echo "ENGRAM on $(hostname) at $(date '+%H:%M:%S')"
row brain   http://127.0.0.1:8001/health        "plans + memory ($brain)"
row console https://127.0.0.1:8443/             "Verna's console (iPad); robot view: $view"
row cert    http://127.0.0.1:8000/ca.crt        "certificate download for the iPad"
row robot   "${ROBOT:-http://127.0.0.1:8200}/robot/state" "$rdesc"
row model   http://127.0.0.1:8080/health        "Qwen3 1.7B (optional)"
row cortex  http://127.0.0.1:8010/health        "memory inspector sidecar (optional)"
if [ -n "$ROBOT" ]; then
  case $(field "$ROBOT/robot/state" estop_latched) in
    true) echo "  robot stop latch: on (it refuses to move until re-armed in the console)" ;;
    false) echo "  robot stop latch: off" ;;
  esac
fi
echo
H=$(hostname -s 2>/dev/null || hostname)
echo "  iPad console:      https://$H.local:8443"   # in the certificate; also works on an IPv6-only hotspot
echo "  iPad certificate:  http://$H.local:8000/ca.crt"
for ip in $(hostname -I 2>/dev/null); do
  case $ip in *:*) continue ;; esac   # the numbers are IPv4; IPv6 devices use the .local name above
  echo "  iPad console:      https://$ip:8443"
  echo "  iPad certificate:  http://$ip:8000/ca.crt"
done
# Safari refuses an https address the certificate does not name (a new network gives the Pi a new address)
CRT=$HUB/console/certs/server.crt
if [ -f "$CRT" ]; then
  san=$(openssl x509 -in "$CRT" -noout -text 2>/dev/null | grep -A1 'Subject Alternative Name' | tail -1)
  miss=
  for n in "$H.local" $(hostname -I 2>/dev/null); do
    case $n in *:*) continue ;; esac
    case "$san," in *"DNS:$n,"*|*"IP Address:$n,"*) ;; *) miss="$miss $n" ;; esac
  done
  [ -z "$miss" ] || echo "  WARNING: the certificate does not name$miss (Safari refuses those); cds_up.sh renews it"
fi
echo "  logs: $LOGS/"
