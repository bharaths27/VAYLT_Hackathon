#!/usr/bin/env bash
# One-time voice setup on star-pi. Needs internet (phone hotspot) for the downloads.
#   cd ~/engram_robot && bash voice/setup_voice.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "== packages"
sudo apt-get update
sudo apt-get install -y ffmpeg git cmake build-essential openssl
[ -d .venv ] || python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -q fastapi uvicorn python-multipart

echo "== whisper.cpp (offline speech-to-text)"
if [ ! -x ~/whisper.cpp/build/bin/whisper-cli ]; then
  [ -d ~/whisper.cpp ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
  (cd ~/whisper.cpp && cmake -B build && cmake --build build --config Release -j4)
fi
[ -f ~/whisper.cpp/models/ggml-tiny.en.bin ] || (cd ~/whisper.cpp && sh ./models/download-ggml-model.sh tiny.en)

echo "== HTTPS certificate (Safari only allows the microphone on https pages)"
mkdir -p voice/certs
if [ ! -f voice/certs/cert.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
    -keyout voice/certs/key.pem -out voice/certs/cert.pem \
    -subj "/CN=star-pi" -addext "subjectAltName=DNS:star-pi.local,DNS:star-pi,DNS:localhost"
  chmod 600 voice/certs/key.pem
fi

echo "== quick check"
~/whisper.cpp/build/bin/whisper-cli -m ~/whisper.cpp/models/ggml-tiny.en.bin \
  -f ~/whisper.cpp/samples/jfk.wav -t 2 -nt -np | head -2

echo "== service"
sudo cp voice/engram-voice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now engram-voice
sleep 2
systemctl --no-pager status engram-voice | head -5
echo
echo "Open on the iPad (same hotspot):  https://$(hostname -I | awk '{print $1}'):8444"
