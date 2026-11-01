#!/usr/bin/env bash
# Turn the notice images (drawn on a PC by deploy/make_cards.py) into the
# 10-minute still videos the TV gets instead of a channel:
#   recording-in-progress.png -> /var/lib/tv-proxy/recording-in-progress.ts
#   stream-in-use.png         -> /var/lib/tv-proxy/stream-in-use.ts
# Run with sudo; ~1 min per clip on a Pi 2 (runs at lowest CPU priority).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
for name in recording-in-progress stream-in-use; do
  OUT=/var/lib/tv-proxy/$name.ts
  TMP="$(mktemp --suffix=.ts)"
  nice -n 19 ffmpeg -hide_banner -loglevel error -y \
    -loop 1 -framerate 1 -i "$HERE/$name.png" \
    -f lavfi -i anullsrc=r=48000:cl=stereo \
    -vf scale=1280:720,format=yuv420p -t 600 \
    -c:v libx264 -preset ultrafast -tune stillimage -g 2 \
    -c:a aac -b:a 32k -f mpegts "$TMP"
  install -o tvproxy -g tvproxy -m 0644 "$TMP" "$OUT"
  rm -f "$TMP"
  ls -la "$OUT"
done
