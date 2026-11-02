# srv01 (home server) setup

* `tv-recordings-sync` -> `/usr/local/bin/`, `tv-recordings-sync.{service,timer}` -> `/etc/systemd/system/`:
  nightly 04:00-08:00 (Europe/Istanbul) read-only rsync of finished recordings from the Pi over
  Tailscale into `/mnt/data/tv/rec/`. Copy only, never deletes. `--dry-run` / `--now` for manual use.
* SSH key `~twan/.ssh/tv_recordings_pull` on srv01; on the Pi in `~twan/.ssh/authorized_keys` as
  `from="<srv01 tailscale ip>",restrict,command="/usr/bin/rrsync -ro /var/lib/tv-proxy/recordings" <key>`.
* `../tv-media.service`: `app/media.py` serving `/mnt/data/tv/rec/` as "Opnames (thuis)" with
  `MEDIA_PUBLIC_URL=lan` (`/etc/tv-media.env`, own `MEDIA_TOKEN`).
