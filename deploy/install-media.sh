#!/usr/bin/env bash
# Installs the LAN media playlist service. Run on the Pi:  sudo bash install-media.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/tv-proxy
ENV=/etc/tv-proxy-media.env
MEDIA_DIR=/var/lib/tv-proxy/media
PORT=8090
OWNER_USER="${SUDO_USER:-twan}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

put() {  # put SRC DEST: install as tvproxy unless already the same file
  [ "$(readlink -f "$1")" = "$(readlink -f "$2" 2>/dev/null || true)" ] && return 0
  install -o tvproxy -g tvproxy -m 0644 "$1" "$2"
}

echo "== code"
put "$HERE/app/media.py"        "$APP/app/media.py"
put "$HERE/tests/test_media.py" "$APP/tests/test_media.py"
install -d -o tvproxy -g tvproxy -m 0755 "$APP/deploy"
put "$HERE/deploy/tv-proxy-media.service" "$APP/deploy/tv-proxy-media.service"
put "$HERE/deploy/install-media.sh"      "$APP/deploy/install-media.sh"
put "$HERE/docs/MEDIA.md"              "$APP/docs/MEDIA.md"

echo "== video folder $MEDIA_DIR (writable by $OWNER_USER)"
install -d -o tvproxy -g tvproxy -m 2775 "$MEDIA_DIR"
setfacl -m "u:$OWNER_USER:rwx" -m "d:u:$OWNER_USER:rwx" -m "d:u:tvproxy:rwx" -m "d:g:tvproxy:r-x" "$MEDIA_DIR"

echo "== config $ENV"
if [ ! -f "$ENV" ]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  umask 027
  cat > "$ENV" <<CONF
MEDIA_TOKEN=$TOKEN
MEDIA_HOST=0.0.0.0
MEDIA_PORT=$PORT
MEDIA_SOURCES=recordings:Opnames:/var/lib/tv-proxy/recordings,videos:Video's:$MEDIA_DIR
# MEDIA_PUBLIC_URL=http://192.168.178.217:$PORT
CONF
  chown root:tvproxy "$ENV"; chmod 0640 "$ENV"
else
  echo "   exists, keeping it"
fi

echo "== firewall"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  SUBNET="$(ip -4 route show dev eth0 proto kernel scope link | awk '{print $1; exit}')"
  [ -n "$SUBNET" ] && ufw allow from "$SUBNET" to any port "$PORT" proto tcp comment 'tv-proxy-media LAN'
  ip link show tailscale0 >/dev/null 2>&1 && ufw allow in on tailscale0 to any port "$PORT" proto tcp comment 'tv-proxy-media tailscale'
fi

echo "== service"
install -m 0644 "$HERE/deploy/tv-proxy-media.service" /etc/systemd/system/tv-proxy-media.service
systemctl daemon-reload
systemctl enable tv-proxy-media
systemctl restart tv-proxy-media
sleep 3
systemctl --no-pager --lines=5 status tv-proxy-media || true

TOKEN="$(sed -n 's/^MEDIA_TOKEN=//p' "$ENV")"
LANIP="$(ip -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)"
echo
echo "Playlist URL for the TV:"
echo "   http://$LANIP:$PORT/$TOKEN/playlist.m3u"
echo "Put mp4/avi files in: $MEDIA_DIR"
