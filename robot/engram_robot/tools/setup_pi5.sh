#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS Bookworm (Pi 5). Run from the engram_robot folder.
set -euo pipefail
sudo apt-get update
sudo apt-get install -y python3-venv python3-picamera2 python3-lgpio python3-gpiozero \
    python3-opencv python3-numpy i2c-tools v4l-utils
sudo usermod -aG dialout,video,gpio,i2c "$USER"   # log out/in once afterwards

# --system-site-packages: picamera2/libcamera and lgpio only come from apt on the Pi 5.
# pip into the system Python is blocked on Bookworm ("externally-managed-environment").
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install fastapi uvicorn pyserial smbus2 pytest httpx
# LiDAR library only if it's an RPLidar:  pip install rplidar-roboticia

echo "Done. Next: . .venv/bin/activate && bash tools/r01_probe.sh | tee r01_probe.txt"
