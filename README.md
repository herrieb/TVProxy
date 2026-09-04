# TVProxy IPTV Proxy

Small, secure IPTV/HLS reverse-proxy running on a Raspberry Pi inside the EU.
It exists so a small number of authorized viewers outside the EU can watch
our own TV station through a Pi located inside the EU.

* No transcoding, no FFmpeg, no video conversion.
* The proxy only relays the existing HLS stream.

## Architecture

```
Internet
   |
   v
Cloudflare HTTPS (public)
   |
   v
Cloudflare Tunnel (outbound from the Pi)
   |
   v
cloudflared  -> 127.0.0.1:8080 (nginx)
                        |
                        v
              127.0.0.1:8081 (tv-proxy)
                        |
                        v
              upstream IPTV/HLS origin
```

The Pi makes **only outbound** connections. No inbound ports are opened on
the home router.

## Directory layout

| Path | Purpose |
|------|---------|
| `/opt/tv-proxy/app/`             | Python application source |
| `/opt/tv-proxy/app/main.py`      | aiohttp application entry point |
| `/opt/tv-proxy/app/__init__.py`  | Package marker |
| `/opt/tv-proxy/venv/`            | Python virtual environment |
| `/opt/tv-proxy/requirements.txt` | Python dependencies |
| `/opt/tv-proxy/.gitignore`       | VCS exclusions |
| `/var/lib/tv-proxy/mappings.db`  | SQLite database of channel mappings (0750 root:tvproxy, file 0640) |
| `/etc/tv-proxy.env`              | Environment configuration (0640, root:tvproxy) |
| `/etc/systemd/system/tv-proxy.service` | systemd service unit |
| `/etc/nginx/sites-available/tv-proxy`  | nginx site |
| `/etc/nginx/sites-enabled/tv-proxy`    | nginx site enabled |
| `/usr/local/bin/cloudflared`     | Cloudflare Tunnel client |

## Ports

| Port | Bound to      | Owner   | Purpose |
|------|--------------|---------|---------|
| 8080 | 127.0.0.1    | nginx   | Local Cloudflare-facing front-end |
| 8081 | 127.0.0.1    | tvproxy | IPTV proxy application |
| 80/443 | not bound   | -       | Cloudflare terminates TLS upstream |

Neither 8080 nor 8081 is publicly listening. Router port-forwarding is
not required and must not be added.

## systemd services

* `tv-proxy.service` — runs `/opt/tv-proxy/venv/bin/python -m app.main`
  as user `tvproxy`, loads `/etc/tv-proxy.env`, restarts on failure.
* `nginx.service` — local front-end on 127.0.0.1:8080.
* `cloudflared.service` — installed once a tunnel token is provided;
  manages the Cloudflare Tunnel connection.

## Configuration

Edit `/etc/tv-proxy.env`:

```
ACCESS_TOKEN=<long-random-string>     # required, no default
UPSTREAM_URL=https://.../channel.m3u8 # optional legacy single-upstream
UPSTREAM_ALLOWED_HOST=hostname-of-upstream # SSRF allowlist for legacy
UPSTREAM_CONNECT_TIMEOUT=5
UPSTREAM_READ_TIMEOUT=30
ADMIN_USER=<admin-username>
ADMIN_PASSWORD_HASH=<bcrypt-hash>      # bcrypt of ADMIN_PASSWORD
```

If `UPSTREAM_URL` is empty, protected routes fall through to the
mappings database, which is managed via the web UI.

If `ADMIN_USER` or `ADMIN_PASSWORD_HASH` is missing, all `/admin/*`
routes return HTTP 401.

## URL format for viewers

Public:

```
https://tv.berrie.uk/live/<slug>.m3u8?token=<ACCESS_TOKEN>
```

Where `<slug>` is the slug configured for the channel in the admin UI
(default: `channel` for the legacy single-upstream config).

Every subsequent request (segments, child playlists, keys, init maps)
is rewritten by the proxy to include the token.

## Admin UI

URL: `https://tv.berrie.uk/admin/`

Authentication: HTTP Basic with `ADMIN_USER` and the bcrypt-hashed
`ADMIN_PASSWORD` from `/etc/tv-proxy.env`.  Sessions are stateless —
each request re-authenticates.

Routes:
* `/admin/login`              login form (also accepts HTTP Basic)
* `/admin/logout`             clears the form-session cookie
* `/admin/mappings`           list + add form + delete buttons
* `/admin/health`             service status
* More admin pages can be added under `/admin/*` without changes
  elsewhere; the auth middleware handles them automatically.

Use the admin UI to map a slug like `kempen-tv` to your upstream
HLS URL.  The form requires the upstream's hostname for SSRF
allowlisting — it must match `urlparse(UPSTREAM_URL).hostname`.

## Common operations

### Restart the application

```
sudo systemctl restart tv-proxy
```

### View logs

```
sudo journalctl -u tv-proxy -f
sudo journalctl -u nginx -f
sudo journalctl -u cloudflared -f
```

### Test health

```
curl http://127.0.0.1:8080/health        # via nginx
curl http://127.0.0.1:8081/health        # application direct
curl https://tv.berrie.uk/health          # public (requires tunnel)
```

### Update the upstream IPTV URL

1. Edit `/etc/tv-proxy.env` and set `UPSTREAM_URL` + `UPSTREAM_ALLOWED_HOST`.
2. `sudo systemctl restart tv-proxy`.
3. `curl "http://127.0.0.1:8080/live/<channel>.m3u8?token=<ACCESS_TOKEN>"`.

### Rotate the viewer access token

1. Generate a new token: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
2. Edit `/etc/tv-proxy.env` and replace `ACCESS_TOKEN`.
3. `sudo systemctl restart tv-proxy`.
4. Distribute the new token to viewers; old token stops working immediately.

### Check Cloudflare Tunnel status

```
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 100 --no-pager
```

### Disable the service safely

```
sudo systemctl stop tv-proxy
sudo systemctl disable tv-proxy
```

Re-enable with `sudo systemctl enable --now tv-proxy`.

## Security

* Token authentication is enforced for every protected route (HTTP 403 otherwise).
* The token is never logged in full (only redacted to `xxx***xxx`).
* The application refuses to fetch any URL whose hostname is not in the
  `UPSTREAM_ALLOWED_HOST` allowlist (SSRF / open-proxy defence).
* The application additionally rejects RFC1918 / loopback / link-local
  / cloud-metadata destinations.
* The systemd unit is hardened with `NoNewPrivileges`, `ProtectSystem`,
  `ProtectHome`, `MemoryDenyWriteExecute`, etc.
* `UPSTREAM_URL` and any upstream credentials live in
  `/etc/tv-proxy.env` (0640, root:tvproxy) and never appear in
  client-visible responses.
* Neither 8080 nor 8081 is exposed to the LAN — both refused from any
  non-loopback interface during testing.

## Health checks

* `GET /health` -> `OK`
* `GET /`       -> `TVProxy IPTV Proxy / Status: running`

Both endpoints are public (no token required) so external monitors can
verify liveness without sharing the access token.

## Manual run (debugging)

```
sudo -u tvproxy bash -c 'cd /opt/tv-proxy && /opt/tv-proxy/venv/bin/python -m app.main'
```

Stop the systemd service first when running it manually to free port 8081.