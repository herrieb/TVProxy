"""TVProxy IPTV/HLS reverse proxy.

Lightweight async HTTP proxy intended to run on a Raspberry Pi.
Listens on 127.0.0.1:8081, expects an nginx front-end on 127.0.0.1:8080
which in turn is reached from the public Internet only via a Cloudflare
Tunnel.

No transcoding, no decoding, no FFmpeg.  Pure HTTP/HLS proxying.

The app is split into modules:
  app.security   - pure security helpers (SSRF, tokens, IP classification)
  app.streaming  - pure HLS / M3U helpers
  app.stores     - SQLite-backed CRUD (viewers, channels, connections, ...)
  app.admin      - admin HTML handlers + auth/session middleware
  app.main       - this file: aiohttp app + public streaming routes

Currently in this build:
  - Per-viewer tokens (HLS auth)  -- NEW
  - Channels CRUD with /admin/channels (test-stream coming next)
  - Connections CRUD with /admin/connections
  - Admin accounts (owner/admin roles) with /admin/admins
  - Settings page /admin/settings
  - Audit log writes for admin actions
  - In-memory session tracking + terminate via /admin/sessions
  - M3U generator + /playlist.m3u route
  - Bootstrap: first user can be created without auth via /admin/users
                (legacy path) or /admin/admins/bootstrap

Deferred to Phase B (see docs/PHASE_B.md):
  - Geo-IP country lookups
  - 2FA TOTP
  - Statistics graphs / per-user traffic charts
  - QR codes
  - Per-session bandwidth counters
  - IP/country restrictions
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import sys
from typing import Optional

import aiohttp
from aiohttp import web

# Local imports
from .security import (
    ip_is_public,
    redact_token,
    token_is_valid,
    validate_upstream_url_runtime,
)
from .streaming import (
    looks_like_playlist,
    rewrite_playlist,
    safe_host,
)


APP_VERSION = "1.2.0"

LISTEN_HOST = os.environ.get("TVPROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TVPROXY_LISTEN_PORT", "8081"))

UPSTREAM_CONNECT_TIMEOUT = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", "5"))
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "30"))

CHUNK_SIZE = 16 * 1024

DB_PATH = os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db")

FORWARD_HEADERS = (
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "range",
    "if-range",
)

logger = logging.getLogger("tvproxy")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _AccessFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        return f"{ts} {record.getMessage()}"


def _configure_logging() -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_AccessFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Public streaming routes
# ---------------------------------------------------------------------------


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(
        text="TVProxy IPTV Proxy\nStatus: running\n",
        content_type="text/plain",
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK\n", content_type="text/plain")


def _extract_token(request: web.Request) -> Optional[str]:
    token = request.query.get("token")
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def handle_playlist(request: web.Request, session: aiohttp.ClientSession):
    """Serve an HLS playlist by looking up the viewer's allowed channels."""
    viewers = request.app["viewers"]
    viewer = None
    token = request.query.get("token") or ""
    if token:
        viewer = await viewers.get_by_token(token)

    if not viewer:
        return web.json_response({"error": "forbidden"}, status=403)
    if viewer.get("disabled"):
        return web.json_response({"error": "disabled"}, status=403)
    expires_at = viewer.get("expires_at")
    if expires_at and expires_at < asyncio.get_event_loop().time():
        return web.json_response({"error": "expired"}, status=403)

    slug = request.match_info.get("name", "").rsplit(".", 1)[0]
    channel = await request.app["channels"].get_by_slug(slug)
    if not channel:
        return web.json_response({"error": "no-such-channel"}, status=404)

    allowed = viewer.get("allowed_channel_ids") or []
    if allowed and channel["id"] not in allowed:
        return web.json_response({"error": "channel-not-allowed"}, status=403)

    # Build the upstream URL from the channel's connection
    conn = await request.app["connections"].get(channel["connection_id"])
    if not conn or not conn.get("enabled"):
        return web.json_response({"error": "upstream-disabled"}, status=502)
    upstream_url = conn["base_url"].rstrip("/") + "/" + channel["upstream_path"].lstrip("/")

    ok, reason = validate_upstream_url_runtime(upstream_url, conn["allowed_host"])
    if not ok:
        logger.warning("upstream-reject reason=%s slug=%s", reason, slug)
        return web.json_response({"error": "upstream-blocked"}, status=502)

    headers = {
        k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS
    }
    if conn.get("auth_header"):
        headers["Authorization"] = conn["auth_header"]

    timeout = aiohttp.ClientTimeout(
        sock_connect=UPSTREAM_CONNECT_TIMEOUT,
        sock_read=UPSTREAM_READ_TIMEOUT,
    )
    try:
        resp = await session.get(
            upstream_url, headers=headers, allow_redirects=False,
            timeout=timeout, auto_decompress=False,
        )
    except aiohttp.ClientError as exc:
        logger.warning("upstream-connect-fail slug=%s err=%s", slug, type(exc).__name__)
        return web.json_response({"error": "upstream-fetch-fail"}, status=502)

    try:
        if resp.status != 200:
            logger.warning("upstream-fetch-fail slug=%s code=%s", slug, resp.status)
            return web.json_response({"error": "upstream-non-200"}, status=502)
        body = await resp.text()
    finally:
        await resp.release()

    proxy_base = f"{str(request.scheme)}://{request.host}/proxy"
    rewritten = rewrite_playlist(body, upstream_url, proxy_base, token)

    # Record session: mark this viewer as currently active.
    sessions = request.app["sessions"]
    sessions.touch(viewer["id"], channel["id"], request, started_at=None)

    return web.Response(
        text=rewritten,
        content_type="application/vnd.apple.mpegurl",
        charset="utf-8",
    )


async def handle_segment(request: web.Request, session: aiohttp.ClientSession):
    viewers = request.app["viewers"]
    token = request.query.get("token") or ""
    viewer = None
    if token:
        viewer = await viewers.get_by_token(token)

    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)

    target = request.query.get("upstream", "")
    if not target:
        return web.json_response({"error": "missing-upstream"}, status=400)

    # Verify target's host is in one of the connection allowlists.
    target_host = safe_host(target)
    connections = await request.app["connections"].list_all()
    conn = None
    for c in connections:
        if c.get("enabled") and c["allowed_host"].lower() == target_host.lower():
            conn = c
            break
    if conn is None:
        return web.json_response({"error": "upstream-blocked"}, status=403)

    ok, reason = validate_upstream_url_runtime(target, target_host)
    if not ok:
        return web.json_response({"error": "upstream-blocked"}, status=403)

    headers = {
        k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS
    }
    if conn.get("auth_header"):
        headers["Authorization"] = conn["auth_header"]

    timeout = aiohttp.ClientTimeout(
        sock_connect=UPSTREAM_CONNECT_TIMEOUT,
        sock_read=UPSTREAM_READ_TIMEOUT,
    )
    try:
        resp = await session.get(
            target, headers=headers, allow_redirects=False,
            timeout=timeout, auto_decompress=False,
        )
    except aiohttp.ClientError as exc:
        logger.warning(
            "upstream-connect-fail url=%s err=%s", target_host, type(exc).__name__
        )
        return web.json_response({"error": "upstream-fetch-fail"}, status=502)

    try:
        if resp.status >= 400:
            return web.json_response({"error": "upstream-non-2xx"}, status=502)

        ctype = resp.headers.get("Content-Type", "")
        if looks_like_playlist(target, ctype):
            body = await resp.text()
            proxy_base = f"{str(request.scheme)}://{request.host}/proxy"
            rewritten = rewrite_playlist(body, target, proxy_base, token)
            return web.Response(
                text=rewritten,
                content_type="application/vnd.apple.mpegurl",
                charset="utf-8",
            )

        out = web.StreamResponse(status=resp.status)
        passthrough = (
            "content-type", "content-length", "content-range",
            "accept-ranges", "last-modified", "etag", "cache-control",
        )
        for k, v in resp.headers.items():
            if k.lower() in passthrough:
                out.headers[k] = v
        await out.prepare(request)
        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
            await out.write(chunk)
        return out
    finally:
        if not resp.closed:
            await resp.release()


async def handle_playlist_m3u(request: web.Request):
    """Render an M3U listing all channels the viewer can watch."""
    viewers = request.app["viewers"]
    settings_store = request.app["settings_store"]
    token = request.query.get("token") or ""
    viewer = await viewers.get_by_token(token)
    if not viewer or viewer.get("disabled"):
        return web.Response(text="#EXTM3U\n", content_type="audio/x-mpegurl")
    if viewer.get("expires_at") and viewer["expires_at"] < asyncio.get_event_loop().time():
        return web.Response(text="#EXTM3U\n", content_type="audio/x-mpegurl")

    allowed = viewer.get("allowed_channel_ids") or []
    all_channels = await request.app["channels"].list_all()
    if allowed:
        channels = [c for c in all_channels if c["id"] in allowed]
    else:
        channels = all_channels

    public_url = (await settings_store.get("public_url")) or "https://tv.berrie.uk"
    from .streaming import generate_user_m3u
    m3u = generate_user_m3u(public_url, channels, token)
    return web.Response(text=m3u, content_type="audio/x-mpegurl")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@web.middleware
async def access_log_middleware(request: web.Request, handler):
    resp = await handler(request)
    logger.info(
        "access method=%s path=%s status=%s token=%s ua=%s",
        request.method,
        request.path,
        resp.status,
        redact_token(_extract_token(request)),
        request.headers.get("User-Agent", "-"),
    )
    return resp


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:  # pragma: no cover - last-resort guard
        logger.exception("unhandled-error path=%s", request.path)
        return web.json_response({"error": "internal-error"}, status=500)


def _make_app(stores: dict, session: aiohttp.ClientSession) -> web.Application:
    app = web.Application(
        middlewares=[access_log_middleware, error_middleware],
    )
    app["viewers"] = stores["viewers"]
    app["channels"] = stores["channels"]
    app["connections"] = stores["connections"]
    app["admin_users"] = stores["admin_users"]
    app["audit"] = stores["audit"]
    app["settings_store"] = stores["settings"]
    app["session"] = session

    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/playlist.m3u", handle_playlist_m3u)
    app.router.add_get(
        "/live/{name:.+\\.m3u8}",
        lambda r: handle_playlist(r, session),
    )
    app.router.add_get("/proxy", lambda r: handle_segment(r, session))

    # Admin subapp
    from . import admin as admin_mod
    admin_app = admin_mod.make_admin_subapp(stores)
    app.add_subapp("/admin", admin_app)

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run() -> None:
    _configure_logging()

    from .stores import (
        AdminUserStore,
        AuditStore,
        ChannelStore,
        ConnectionStore,
        SettingsStore,
        ViewerStore,
    )
    from .admin import SessionRegistry

    stores = {
        "viewers":     ViewerStore(DB_PATH),
        "channels":    ChannelStore(DB_PATH),
        "connections": ConnectionStore(DB_PATH),
        "admin_users": AdminUserStore(DB_PATH),
        "audit":       AuditStore(DB_PATH),
        "settings":    SettingsStore(DB_PATH),
    }
    for s in stores.values():
        s.init_schema()

    logger.info("starting listen=%s:%s version=%s", LISTEN_HOST, LISTEN_PORT, APP_VERSION)

    connector = aiohttp.TCPConnector(limit=20, limit_per_host=10, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, connect=UPSTREAM_CONNECT_TIMEOUT)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    app = _make_app(stores, session)
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    site = web.SockSite(runner, sock)
    await site.start()

    logger.info("listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        await session.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()