#!/usr/bin/env bash
# Direct Ethernet cable between the two Pis, fixed addresses, no router needed, works offline.
#   on the BOT Pi:    bash deploy/link_setup.sh bot
#   on the BRAIN Pi:  bash deploy/link_setup.sh brain
# Wi-Fi keeps working for internet / laptop / iPad; this link has no gateway, so it never steals traffic.
set -euo pipefail
case "${1:-}" in
  bot)   IP=10.0.0.2 ;;
  brain) IP=10.0.0.1 ;;
  *) echo "usage: $0 bot|brain"; exit 1 ;;
esac
sudo nmcli connection delete robotlink 2>/dev/null || true
sudo nmcli connection add type ethernet ifname eth0 con-name robotlink \
  ipv4.method manual ipv4.addresses "$IP/24" ipv4.never-default yes ipv6.method disabled \
  connection.autoconnect yes connection.autoconnect-priority 50
sudo nmcli connection up robotlink || echo "(cable not plugged in yet - it will come up when it is)"
echo "This Pi is $IP on the robot link. Other Pi: $([ "$IP" = 10.0.0.2 ] && echo 10.0.0.1 || echo 10.0.0.2)"
