#!/usr/bin/env bash
# Publish tv-proxy-media through nginx (https://tv.berrie.uk/media/ and the
# merged /tv/<code> playlists). Idempotent. Run with sudo.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SITE=/etc/nginx/sites-available/tv-proxy
ENV=/etc/tv-proxy-media.env
APP=/opt/tv-proxy

for f in app/media.py tests/test_media.py deploy/nginx-media.conf deploy/publish-media.sh \
         deploy/install-media.sh deploy/tv-proxy-media.service deploy/grant_channels.py docs/MEDIA.md \
         deploy/make-busy-clip.sh; do
  [ "$(readlink -f "$HERE/$f")" = "$(readlink -f "$APP/$f")" ] || install -o tvproxy -g tvproxy -m 0644 "$HERE/$f" "$APP/$f"
done

setenv() {  # setenv KEY VALUE
  if grep -q "^$1=" "$ENV"; then sed -i "s#^$1=.*#$1=$2#" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}
setenv MEDIA_PUBLIC_URL https://tv.berrie.uk/media
setenv MEDIA_MERGE_VIEWERS twan
setenv MEDIA_TS_TEST nl-npo-1-fhd,tr-bein-sports-1
setenv MEDIA_TS_URLS 1
setenv MEDIA_RELAY_BUFFER_MB 64
setenv MEDIA_RELAY_PREBUFFER 5

# notice clips: rebuild when an image changed (or a clip is missing)
REBUILD=0
for img in recording-in-progress stream-in-use; do
  if ! cmp -s "$HERE/deploy/$img.png" "$APP/deploy/$img.png" || [ ! -f "/var/lib/tv-proxy/$img.ts" ]; then
    REBUILD=1
  fi
  install -o tvproxy -g tvproxy -m 0644 "$HERE/deploy/$img.png" "$APP/deploy/$img.png"
done
install -o tvproxy -g tvproxy -m 0644 "$HERE/deploy/make_cards.py" "$APP/deploy/make_cards.py"
# channel/category icons (made on a PC by deploy/make_icons.py)
install -d -o tvproxy -g tvproxy -m 0755 /var/lib/tv-proxy/icons "$APP/deploy/icons"
for f in "$HERE"/deploy/icons/*.png; do
  install -o tvproxy -g tvproxy -m 0644 "$f" /var/lib/tv-proxy/icons/
  install -o tvproxy -g tvproxy -m 0644 "$f" "$APP/deploy/icons/"
done
for f in deploy/make_icons.py app/logos.py; do
  install -o tvproxy -g tvproxy -m 0644 "$HERE/$f" "$APP/$f"
done
[ "$REBUILD" = 0 ] || bash "$APP/deploy/make-busy-clip.sh"

echo "== tests"
(cd "$APP" && sudo -u tvproxy env HOME=/tmp venv/bin/python -m pytest -p no:cacheprovider tests/test_media.py 2>&1 | tail -1)

cp -a "$SITE" "$SITE.bak-$(date +%Y%m%d-%H%M%S)"
python3 - "$SITE" "$HERE/deploy/nginx-media.conf" <<'PY'
import re, sys
site, block = sys.argv[1], open(sys.argv[2]).read()
s = open(site).read()
s = re.sub(r"    # tv-proxy-media \(BEGIN\).*?# tv-proxy-media \(END\)\n\n?", "", s, flags=re.S)
anchor = "    # Pass everything through to the Python application on 8081."
assert anchor in s, "anchor not found in nginx site"
open(site, "w").write(s.replace(anchor, block + anchor, 1))
PY
install -m 0644 "$HERE/deploy/tv-proxy-media.service" /etc/systemd/system/tv-proxy-media.service
systemctl daemon-reload
nginx -t
systemctl restart tv-proxy-media
sleep 3
systemctl reload nginx
systemctl is-active tv-proxy-media nginx | paste -sd' '
