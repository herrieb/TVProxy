# Media playlist (`tv-proxy-media`)

A small, separate service that serves the local recordings and other video files
(mp4, avi, mkv, ...) and merges them into a viewer's normal short playlist, so one
URL (`https://tv.berrie.uk/tv/<short_code>`) has live TV and recordings.

## How a request flows

```
TV -> Cloudflare -> nginx :8080
   /tv/<code>          -> tv-proxy-media :8090  (live part from the DB, only the viewer's
                                                 channels, + recordings/videos appended)
   /tv/<code>/<s>.m3u8 -> tv-proxy :8081        (streams, unchanged)
   /media/<token>/...  -> tv-proxy-media :8090  (recording/video files, Range/seek)
```

Only viewers in `MEDIA_MERGE_VIEWERS` (now: `twan`) get the merged playlist; for
everyone else `/tv/<code>` is proxied to tv-proxy unchanged. tv-proxy itself loads all
~86k channels per playlist request (20-80 s on the Pi 2, TV apps time out); the merged
route reads only the viewer's channels and formats them with tv-proxy's
`generate_user_m3u`, so it answers in well under a second.

* Code: `app/media.py`, tests: `tests/test_media.py`
* Unit: `tv-proxy-media.service` (`python -m app.media`, user `tvproxy`; DB opened read-only)
* Config: `/etc/tv-proxy-media.env` (0640 root:tvproxy), see the module docstring for all options
* Listens on `0.0.0.0:8090`; only loopback (nginx), RFC1918 and Tailscale clients are accepted.
* nginx snippet: `deploy/nginx-media.conf`, inserted into the site by `deploy/publish-media.sh`.

## URLs

```
http://<pi-lan-ip>:8090/<MEDIA_TOKEN>/playlist.m3u        playlist, rebuilt on every request
http://<pi-lan-ip>:8090/<MEDIA_TOKEN>/files/<source>/<path> file, supports Range (seeking)
http://<pi-lan-ip>:8090/health
```

"Refreshing" is just reloading the playlist URL in the player: new recordings and new
files in the video folder show up immediately, deleted/uploaded ones disappear.

## Sources

| key | group in playlist | folder |
|-----|-------------------|--------|
| `recordings` | Opnames | `/var/lib/tv-proxy/recordings` (recorder output; parts grouped as "deel 1/N") |
| `videos` | Video's (+ subfolder) | `/var/lib/tv-proxy/media` (copy your own mp4/avi here, user `twan` can write) |

Files under 1 MiB and hidden files are skipped. Note: once the recorder uploads a recording
to OneDrive it deletes the local file, so it disappears from the playlist.

## Install / update

```
sudo bash deploy/install-media.sh     # first install (service, env, media folder)
sudo bash deploy/publish-media.sh     # code update + nginx + merge settings (idempotent)
```

Rotate the token: edit `MEDIA_TOKEN` in `/etc/tv-proxy-media.env`, `sudo systemctl restart tv-proxy-media`,
and update the playlist URL on the TV.

## Home server copies ("Opnames (thuis)")

srv01 (at home, Tailscale 100.92.103.112) gets a nightly copy of finished recordings
(`/usr/local/bin/tv-recordings-sync`, timer 04:00-08:00 Europe/Istanbul, read-only rsync
key restricted with `rrsync -ro`) in `/mnt/data/tv/rec/`. The same `app/media.py` runs there
as `tv-media.service` (`deploy/tv-media.service`, `/etc/tv-media.env` with its own token,
`MEDIA_SOURCES=thuis:Opnames (thuis):/mnt/data/tv/rec`, `MEDIA_PUBLIC_URL=lan` so links use
srv01's current LAN address). The Pi appends that playlist to the merged playlist via
`MEDIA_REMOTE_PLAYLISTS` (over Tailscale; skipped when srv01 is unreachable), so the TV plays
those copies over the home network. `MEDIA_TIMEZONE=Europe/Istanbul` on both: recorder file
names are UTC, titles are shown in Turkish time.
