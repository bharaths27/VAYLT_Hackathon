#!/usr/bin/env bash
# Make the Pi its own Wi-Fi network (no internet behind it). iPad joins ENGRAM-LAB,
# console is at https://10.42.0.1:8443. This is the cleanest offline proof for judges.
# The password belongs to this Pi: made on the first run into ~/.engram-lab-wifi (only you can read it) and
# reused after that, so an iPad that joined once keeps working. Anyone on ENGRAM-LAB can approve plans: never post it.
set -eu
PW_FILE=$HOME/.engram-lab-wifi
if [ ! -s "$PW_FILE" ]; then
  (umask 077; head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12 > "$PW_FILE")
fi
PW=$(cat "$PW_FILE")
sudo nmcli device wifi hotspot ssid ENGRAM-LAB password "$PW" ifname wlan0
nmcli -g IP4.ADDRESS device show wlan0
echo "Wi-Fi ENGRAM-LAB, password: $PW (kept in ~/.engram-lab-wifi; never post it)"
