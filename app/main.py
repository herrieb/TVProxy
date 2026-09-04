"""TVProxy IPTV/HLS reverse proxy.

Lightweight async HTTP proxy intended to run on a Raspberry Pi.
Listens on 127.0.0.1:8081, expects an nginx front-end on 127.0.0.1:8080
which in turn is reached from the public Internet only via a Cloudflare
Tunnel.

No transcoding, no decoding, no FFmpeg.  Pure HTTP/HLS proxying.

Security goals:
- The proxy may only reach the configured upstream IPTV/HLS origin.
- Viewers must present the access token (?token=...) on every request.
- Upstream credentials never leak back to the viewer.
- The application never accepts an arbitrary upstream URL from the client.

A small admin UI is exposed under /admin/* for managing channel
mappings (slug -> upstream URL/host) in a SQLite database.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import socket
import sqlite3
import sys
import threading
import time
from typing import Iterable, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlencode, quote

import aiohttp
import bcrypt
from aiohttp import web


APP_VERSION = "1.1.0"

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
# Configuration / env loading
# ---------------------------------------------------------------------------


def _load_env_file(path: str) -> None:
    """Load KEY=VALUE entries from a file into os.environ (no shell eval)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


class Config:
    """Runtime configuration loaded from the environment."""

    def __init__(self) -> None:
        self.access_token: str = os.environ.get("ACCESS_TOKEN", "").strip()
        self.upstream_url: str = os.environ.get("UPSTREAM_URL", "").strip()
        self.upstream_allowed_host: str = os.environ.get(
            "UPSTREAM_ALLOWED_HOST", ""
        ).strip()
        self.admin_user: str = os.environ.get("ADMIN_USER", "").strip()
        # ADMIN_PASSWORD_HASH is the bcrypt hash; if not set we fall back to
        # ADMIN_PASSWORD (plain text) for first-run convenience and hash it
        # into memory.  Plain is never written back.
        self.admin_password_hash: str = os.environ.get(
            "ADMIN_PASSWORD_HASH", ""
        ).strip()
        self.admin_password: str = os.environ.get("ADMIN_PASSWORD", "").strip()

    @property
    def upstream_configured(self) -> bool:
        return bool(self.upstream_url) and bool(self.upstream_allowed_host)

    def describe(self) -> str:
        if self.upstream_configured:
            return f"upstream host={self.upstream_host}"
        return "upstream=UNCONFIGURED"

    @property
    def upstream_host(self) -> str:
        return urlparse(self.upstream_url).hostname or ""

    def admin_password_hash_runtime(self) -> str:
        """Return the bcrypt hash; hash plain ADMIN_PASSWORD on the fly."""
        if self.admin_password_hash:
            return self.admin_password_hash
        if self.admin_password:
            return bcrypt.hashpw(
                self.admin_password.encode("utf-8"),
                bcrypt.gensalt(rounds=10),
            ).decode("utf-8")
        return ""


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _token_is_valid(provided: Optional[str], expected: str) -> bool:
    if not expected:
        return False
    if not provided:
        return False
    if len(provided) != len(expected):
        return False
    return _constant_time_eq(provided, expected)


def _redact_token(value: Optional[str]) -> str:
    if not value:
        return "-"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


def _ip_is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return True


def _resolve_and_check(host: str) -> Tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"dns-fail:{exc}"
    if not infos:
        return False, "dns-empty"
    for info in infos:
        addr = info[4][0]
        if not _ip_is_public(addr):
            return False, f"non-public-ip:{addr}"
    return True, "ok"


def _validate_upstream_url(url: str, allowed_host: str) -> Tuple[bool, str]:
    if not url:
        return False, "no-upstream-configured"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return False, f"parse-error:{exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"bad-scheme:{parsed.scheme}"
    host = parsed.hostname or ""
    if not host:
        return False, "no-host"
    if not allowed_host:
        return False, "no-allowed-host-configured"
    if host.lower() != allowed_host.lower():
        return False, f"host-not-allowed:{host}"
    try:
        ipaddress.ip_address(host)
        if not _ip_is_public(host):
            return False, f"non-public-upstream-ip:{host}"
    except ValueError:
        # The hostname is a DNS name.  We defer the actual DNS lookup
        # to fetch time so that operators can store mappings for hosts
        # that are temporarily unreachable (e.g. on flaky DNS).
        pass
    return True, "ok"


def _validate_upstream_url_runtime(url: str, allowed_host: str) -> Tuple[bool, str]:
    """Validate URL structure + add an explicit DNS check that all
    returned addresses are public.  Called at fetch time.
    """
    ok, reason = _validate_upstream_url(url, allowed_host)
    if not ok:
        return ok, reason
    try:
        ipaddress.ip_address(urlparse(url).hostname or "")
        # IP literal already checked by _validate_upstream_url.
        return True, "ok"
    except ValueError:
        host = urlparse(url).hostname or ""
        ok, msg = _resolve_and_check(host)
        if not ok:
            return False, f"dns-block:{msg}"
    return True, "ok"


def _join_url(base: str, ref: str) -> str:
    return urljoin(base, ref)


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
# SQLite mappings DB
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mappings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL DEFAULT '',
    upstream_url    TEXT NOT NULL,
    upstream_host   TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mappings_slug_idx ON mappings(slug);
"""


class MappingStore:
    """Thread-safe wrapper around the SQLite mappings DB.

    All public methods are coroutine-safe via an asyncio.Lock and a
    per-call connection.  The store is opened lazily.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_dir(self) -> None:
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema_sync(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._ensure_dir()
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._initialized = True
            finally:
                conn.close()

    async def init(self) -> None:
        await asyncio.to_thread(self._init_schema_sync)

    async def list_all(self):
        def _do():
            self._init_schema_sync()
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM mappings ORDER BY id ASC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await asyncio.to_thread(_do)

    async def get_by_slug(self, slug: str):
        def _do():
            self._init_schema_sync()
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM mappings WHERE slug = ? AND enabled = 1",
                    (slug,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await asyncio.to_thread(_do)

    async def create(
        self,
        slug: str,
        display_name: str,
        upstream_url: str,
        upstream_host: str,
    ) -> Tuple[bool, str]:
        def _do():
            self._init_schema_sync()
            now = time.time()
            conn = self._connect()
            try:
                try:
                    conn.execute(
                        """
                        INSERT INTO mappings
                            (slug, display_name, upstream_url, upstream_host,
                             enabled, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (slug, display_name, upstream_url, upstream_host, now, now),
                    )
                    return True, "ok"
                except sqlite3.IntegrityError as exc:
                    return False, f"slug-exists: {exc}"
            finally:
                conn.close()

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def update(
        self,
        mapping_id: int,
        slug: str,
        display_name: str,
        upstream_url: str,
        upstream_host: str,
        enabled: bool,
    ) -> Tuple[bool, str]:
        def _do():
            self._init_schema_sync()
            now = time.time()
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE mappings SET
                        slug = ?, display_name = ?, upstream_url = ?,
                        upstream_host = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        slug,
                        display_name,
                        upstream_url,
                        upstream_host,
                        1 if enabled else 0,
                        now,
                        mapping_id,
                    ),
                )
                if cur.rowcount == 0:
                    return False, "not-found"
                return True, "ok"
            except sqlite3.IntegrityError as exc:
                return False, f"slug-exists: {exc}"
            finally:
                conn.close()

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def delete(self, mapping_id: int) -> bool:
        def _do():
            self._init_schema_sync()
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM mappings WHERE id = ?", (mapping_id,))
                return cur.rowcount > 0
            finally:
                conn.close()

        async with self._lock:
            return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Admin authentication
# ---------------------------------------------------------------------------


def _admin_check_credentials(cfg: Config, user: str, password: str) -> bool:
    if not cfg.admin_user or not user:
        if not _constant_time_eq(user or "", cfg.admin_user):
            return False
    if not _constant_time_eq(user, cfg.admin_user):
        return False
    hash_str = cfg.admin_password_hash_runtime()
    if not hash_str:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hash_str.encode("utf-8"))
    except ValueError:
        return False


def _parse_basic_auth(header: str) -> Optional[Tuple[str, str]]:
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", errors="replace")
    except Exception:
        return None
    if ":" not in raw:
        return None
    user, _, password = raw.partition(":")
    return user, password


@web.middleware
async def admin_auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/admin"):
        return await handler(request)
    if request.path in ("/admin/login",):
        return await handler(request)
    cfg: Config = request.app["config"]
    creds = _parse_basic_auth(request.headers.get("Authorization", ""))
    if creds and _admin_check_credentials(cfg, *creds):
        request["admin_user"] = creds[0]
        return await handler(request)
    # WWW-Authenticate to trigger browser prompt; non-browser clients still
    # get 401 from the response.
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="TVProxy Admin", charset="UTF-8"'},
        text="Authentication required",
        content_type="text/plain",
    )


# ---------------------------------------------------------------------------
# Upstream fetch
# ---------------------------------------------------------------------------


class Upstream:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.session = session

    async def fetch_stream(
        self, url: str, request_headers: Iterable[Tuple[str, str]]
    ):
        timeout = aiohttp.ClientTimeout(
            sock_connect=UPSTREAM_CONNECT_TIMEOUT,
            sock_read=UPSTREAM_READ_TIMEOUT,
        )
        headers = {
            k: v for k, v in request_headers if k.lower() in FORWARD_HEADERS
        }
        resp = await self.session.get(
            url,
            headers=headers,
            allow_redirects=False,
            timeout=timeout,
            auto_decompress=False,
        )
        return resp

    def validate(self, url: str, allowed_host: str) -> Tuple[bool, str]:
        return _validate_upstream_url_runtime(url, allowed_host)


# ---------------------------------------------------------------------------
# Public request handlers
# ---------------------------------------------------------------------------


async def _stream_response(
    request: web.Request, resp: aiohttp.ClientResponse
) -> web.StreamResponse:
    out = web.StreamResponse(status=resp.status)
    passthrough = (
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "last-modified",
        "etag",
        "cache-control",
    )
    for k, v in resp.headers.items():
        if k.lower() in passthrough:
            out.headers[k] = v
    await out.prepare(request)
    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
        await out.write(chunk)
    return out


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


async def _resolve_stream(
    request: web.Request,
    cfg: Config,
    store: MappingStore,
    slug_from_path: str,
) -> Optional[Tuple[str, str, str]]:
    """Return (upstream_url, allowed_host, slug) or None if slug not found.

    Slug from the URL maps to a row in the mappings DB.  If absent we
    fall back to the single upstream from /etc/tv-proxy.env when the
    requested slug matches "channel" (legacy behaviour).
    """
    mapping = await store.get_by_slug(slug_from_path)
    if mapping:
        return mapping["upstream_url"], mapping["upstream_host"], mapping["slug"]
    if slug_from_path == "channel" and cfg.upstream_configured:
        return cfg.upstream_url, cfg.upstream_allowed_host, "channel"
    return None


async def handle_playlist(request: web.Request, upstream: Upstream, cfg: Config):
    store: MappingStore = request.app["store"]
    slug = request.match_info.get("name", "").rsplit(".", 1)[0]
    resolved = await _resolve_stream(request, cfg, store, slug)
    if not resolved:
        return web.json_response({"error": "no-such-channel"}, status=404)
    target_url, allowed_host, real_slug = resolved

    provided = _extract_token(request)
    if not _token_is_valid(provided, cfg.access_token):
        return web.json_response({"error": "forbidden"}, status=403)

    ok, reason = upstream.validate(target_url, allowed_host)
    if not ok:
        logger.warning("upstream-reject reason=%s slug=%s", reason, real_slug)
        return web.json_response({"error": "upstream-blocked"}, status=502)

    try:
        resp = await upstream.fetch_stream(target_url, request.headers.items())
    except aiohttp.ClientError as exc:
        logger.warning(
            "upstream-connect-fail slug=%s err=%s", real_slug, type(exc).__name__
        )
        return web.json_response({"error": "upstream-fetch-fail"}, status=502)

    try:
        if resp.status != 200:
            logger.warning(
                "upstream-fetch-fail slug=%s code=%s", real_slug, resp.status
            )
            return web.json_response({"error": "upstream-non-200"}, status=502)
        body = await resp.text()
    finally:
        await resp.release()

    # We rewrite URLs relative to the proxy path the viewer used
    # (/live/<slug>.m3u8).  Subsequent segment/child URLs go through
    # /proxy?upstream=...&token=...
    proxy_base = f"{str(request.scheme)}://{request.host}/proxy"
    rewritten = rewrite_playlist(
        body,
        target_url,
        proxy_base,
        provided,
    )

    return web.Response(
        text=rewritten,
        content_type="application/vnd.apple.mpegurl",
        charset="utf-8",
    )


async def handle_segment(request: web.Request, upstream: Upstream, cfg: Config):
    if not cfg.upstream_configured:
        # cfg.upstream_configured is a legacy check; with mappings DB
        # we still need a token to validate.  Don't gate on env upstream.
        pass

    provided = _extract_token(request)
    if not _token_is_valid(provided, cfg.access_token):
        return web.json_response({"error": "forbidden"}, status=403)

    target = request.query.get("upstream", "")
    if not target:
        return web.json_response({"error": "missing-upstream"}, status=400)

    # Allow the target if it matches any enabled mapping OR if it matches
    # the legacy env-configured upstream.
    store: MappingStore = request.app["store"]
    allowed_hosts: set[str] = set()
    async for m in await store.list_all():
        if m.get("enabled"):
            # Host for this mapping's upstream.
            try:
                allowed_hosts.add(urlparse(m["upstream_url"]).hostname or "")
            except Exception:
                pass
    if cfg.upstream_configured:
        allowed_hosts.add(cfg.upstream_allowed_host)

    try:
        target_host = urlparse(target).hostname or ""
    except Exception:
        target_host = ""

    if target_host.lower() not in {h.lower() for h in allowed_hosts if h}:
        logger.warning(
            "upstream-reject slug=- reason=host-not-allowed:%s", target_host
        )
        return web.json_response({"error": "upstream-blocked"}, status=403)

    ok, reason = upstream.validate(target, target_host)
    if not ok:
        logger.warning("upstream-reject reason=%s", reason)
        return web.json_response({"error": "upstream-blocked"}, status=403)

    try:
        resp = await upstream.fetch_stream(target, request.headers.items())
    except aiohttp.ClientError as exc:
        logger.warning(
            "upstream-connect-fail url=%s err=%s",
            _safe_host(target),
            type(exc).__name__,
        )
        return web.json_response({"error": "upstream-fetch-fail"}, status=502)

    try:
        if resp.status >= 400:
            logger.warning(
                "upstream-fetch-fail url=%s code=%s",
                _safe_host(target),
                resp.status,
            )
            return web.json_response({"error": "upstream-non-2xx"}, status=502)

        ctype = resp.headers.get("Content-Type", "")
        if _looks_like_playlist(target, ctype):
            body = await resp.text()
            proxy_base = f"{str(request.scheme)}://{request.host}/proxy"
            rewritten = rewrite_playlist(body, target, proxy_base, provided)
            return web.Response(
                text=rewritten,
                content_type="application/vnd.apple.mpegurl",
                charset="utf-8",
            )

        return await _stream_response(request, resp)
    finally:
        if not resp.closed:
            await resp.release()


def _looks_like_playlist(url: str, content_type: str) -> bool:
    if url.lower().endswith(".m3u8"):
        return True
    if "mpegurl" in content_type.lower():
        return True
    return False


def _safe_host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


def rewrite_playlist(
    body: str, base_url: str, proxy_base_url: str, token: Optional[str]
) -> str:
    out_lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#EXT"):
            out_lines.append(
                _rewrite_attribute_line(stripped, base_url, proxy_base_url, token)
            )
            continue
        out_lines.append(_rewrite_uri_line(stripped, base_url, proxy_base_url, token))
    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else "")


def _rewrite_uri_line(
    uri: str, base_url: str, proxy_base_url: str, token: Optional[str]
) -> str:
    absolute = _join_url(base_url, uri)
    proxy_qs = urlencode({"upstream": absolute, "token": token or ""})
    return f"{proxy_base_url}?{proxy_qs}"


def _rewrite_attribute_line(
    line: str, base_url: str, proxy_base_url: str, token: Optional[str]
) -> str:
    if "URI=\"" not in line:
        return line
    prefix, rest = line.split("URI=\"", 1)
    uri, after = rest.split("\"", 1)
    absolute = _join_url(base_url, uri)
    proxy_qs = urlencode({"upstream": absolute, "token": token or ""})
    new_uri = f"{proxy_base_url}?{proxy_qs}"
    return f"{prefix}URI=\"{new_uri}\"{after}"


# ---------------------------------------------------------------------------
# Admin UI handlers
# ---------------------------------------------------------------------------


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TVProxy Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 980px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.4; }
  header { display: flex; align-items: baseline; justify-content: space-between;
           border-bottom: 1px solid #8884; padding-bottom: .5rem; margin-bottom: 1rem; }
  h1 { font-size: 1.4rem; margin: 0; }
  nav a { margin-right: 1rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: .5rem .6rem; border-bottom: 1px solid #8884;
           text-align: left; vertical-align: top; }
  th { background: #8882; }
  code { background: #8883; padding: .1rem .3rem; border-radius: .2rem; }
  input[type=text], input[type=url], textarea {
    width: 100%; box-sizing: border-box; padding: .45rem .55rem;
    border: 1px solid #8886; border-radius: .3rem;
    background: #8881; color: inherit; font-family: inherit;
  }
  label { display: block; font-weight: 600; margin-top: .8rem; }
  .row { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
  button { padding: .5rem .9rem; border: 1px solid #8886;
           border-radius: .3rem; background: #8882; color: inherit;
           cursor: pointer; }
  button.primary { background: #2563eb; color: white; border-color: #2563eb; }
  button.danger  { background: #b91c1c; color: white; border-color: #b91c1c; }
  .msg { padding: .6rem .8rem; border-radius: .3rem; margin-bottom: 1rem; }
  .msg.ok   { background: #16653433; }
  .msg.err  { background: #991b1b33; }
  .muted { color: #888; font-size: .9em; }
  form.inline { display: inline; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
"""

_HTML_FOOT = """</body></html>"""


def _render_admin_page(title: str, body: str) -> web.Response:
    html = (
        _HTML_HEAD
        + f"<header><h1>{title}</h1><nav>"
        + '<a href="/admin/mappings">Mappings</a>'
        + '<a href="/admin/health">Health</a>'
        + '<form class="inline" method="post" action="/admin/logout">'
        + '<button type="submit" class="danger">Logout</button>'
        + "</form></nav></header>"
        + body
        + _HTML_FOOT
    )
    return web.Response(text=html, content_type="text/html")


def _flash(request: web.Request, kind: str, message: str) -> None:
    request.app["_flash"] = (kind, message)


def _consume_flash(request: web.Request) -> str:
    pair = request.app.pop("_flash", None)
    if not pair:
        return ""
    kind, message = pair
    return f'<div class="msg {kind}">{message}</div>'


async def admin_index(request: web.Request) -> web.Response:
    raise web.HTTPFound("/admin/mappings")


async def admin_login_get(request: web.Request) -> web.Response:
    body = (
        "<p>Authentication required.</p>"
        "<p>This is a private admin interface. Use the credentials supplied "
        "by the operator.</p>"
    )
    return _render_admin_page("Admin login", body)


async def admin_login_post(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    # Allow POST-based login (form-style) for non-Basic clients.
    data = await request.post()
    user = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if _admin_check_credentials(cfg, user, password):
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        raise web.HTTPFound(
            "/admin/mappings",
            headers={"Set-Cookie": f"tv_basic={token}; HttpOnly; Path=/admin; SameSite=Strict"},
        )
    body = '<div class="msg err">Invalid credentials.</div><p><a href="/admin/login">Retry</a></p>'
    return _render_admin_page("Admin login", body)


async def admin_logout(request: web.Request) -> web.Response:
    raise web.HTTPFound(
        "/admin/login",
        headers={"Set-Cookie": "tv_basic=; HttpOnly; Path=/admin; SameSite=Strict; Max-Age=0"},
    )


async def admin_health(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    rows = [
        ("Status", "OK"),
        ("App version", APP_VERSION),
        ("Listen address", f"{LISTEN_HOST}:{LISTEN_PORT}"),
        ("DB path", DB_PATH),
        ("Env upstream configured", "yes" if cfg.upstream_configured else "no"),
    ]
    body = (
        _consume_flash(request)
        + "<table>"
        + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
        + "</table>"
    )
    return _render_admin_page("Admin health", body)


async def admin_mappings_get(request: web.Request) -> web.Response:
    store: MappingStore = request.app["store"]
    rows = await store.list_all()

    flash = _consume_flash(request)

    table_rows = ""
    if rows:
        for r in rows:
            table_rows += (
                "<tr>"
                f"<td>{r['id']}</td>"
                f"<td><code>{_html_escape(r['slug'])}</code></td>"
                f"<td>{_html_escape(r['display_name'])}</td>"
                f"<td><code>{_html_escape(r['upstream_url'])}</code></td>"
                f"<td><code>{_html_escape(r['upstream_host'])}</code></td>"
                f"<td>{'yes' if r['enabled'] else 'no'}</td>"
                "<td>"
                f"<form class=inline method=post action='/admin/mappings/{r['id']}/delete'>"
                f"<button class=danger type=submit>Delete</button></form>"
                "</td>"
                "</tr>"
            )
    else:
        table_rows = '<tr><td colspan=7 class=muted>No mappings yet.</td></tr>'

    body = (
        flash
        + '<h2>Mappings</h2>'
        + '<table><thead><tr>'
        '<th>ID</th><th>Slug</th><th>Name</th>'
        '<th>Upstream URL</th><th>Upstream Host</th><th>Enabled</th><th></th>'
        '</tr></thead><tbody>'
        + table_rows
        + '</tbody></table>'

        + '<h2 style="margin-top:2rem">Add mapping</h2>'
        + '<form method="post" action="/admin/mappings">'
        + '<div class="grid">'
        + '<div><label>Slug</label><input name="slug" type="text" pattern="[a-zA-Z0-9._-]{1,64}" required placeholder="kempen-tv"></div>'
        + '<div><label>Display name</label><input name="display_name" type="text" placeholder="TVProxy Live"></div>'
        + '</div>'
        + '<label>Upstream m3u8 URL</label>'
        + '<input name="upstream_url" type="url" required placeholder="https://origin.example/path/channel.m3u8">'
        + '<label>Upstream host (allowlist; must match the hostname of the URL above)</label>'
        + '<input name="upstream_host" type="text" required placeholder="origin.example">'
        + '<p class=muted>Players will use: '
        + '<code>https://tv.berrie.uk/live/&lt;slug&gt;.m3u8?token=&lt;ACCESS_TOKEN&gt;</code></p>'
        + '<p><button class=primary type=submit>Add mapping</button></p>'
        + '</form>'
    )
    return _render_admin_page("Mappings", body)


async def admin_mappings_post(request: web.Request) -> web.Response:
    store: MappingStore = request.app["store"]
    data = await request.post()
    slug = (data.get("slug") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    upstream_url = (data.get("upstream_url") or "").strip()
    upstream_host = (data.get("upstream_host") or "").strip()

    if not slug or not upstream_url or not upstream_host:
        _flash(request, "err", "slug, upstream_url and upstream_host are required.")
        raise web.HTTPFound("/admin/mappings")

    # Validate against SSRF rules before storing.
    ok, reason = _validate_upstream_url(upstream_url, upstream_host)
    if not ok:
        _flash(request, "err", f"Upstream rejected: {reason}")
        raise web.HTTPFound("/admin/mappings")

    ok, reason = await store.create(slug, display_name, upstream_url, upstream_host)
    if not ok:
        _flash(request, "err", f"Could not create mapping: {reason}")
    else:
        _flash(request, "ok", f"Mapping '{slug}' created.")
    raise web.HTTPFound("/admin/mappings")


async def admin_mapping_delete(request: web.Request) -> web.Response:
    store: MappingStore = request.app["store"]
    mapping_id = int(request.match_info["id"])
    deleted = await store.delete(mapping_id)
    if deleted:
        _flash(request, "ok", f"Mapping #{mapping_id} deleted.")
    else:
        _flash(request, "err", f"Mapping #{mapping_id} not found.")
    raise web.HTTPFound("/admin/mappings")


def _html_escape(value: object) -> str:
    if value is None:
        return ""
    s = str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


@web.middleware
async def access_log_middleware(request: web.Request, handler):
    resp = await handler(request)
    logger.info(
        "access method=%s path=%s status=%s token=%s admin=%s ua=%s",
        request.method,
        request.path,
        resp.status,
        _redact_token(_extract_token(request)),
        "-" if not request.path.startswith("/admin") else "y",
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


def _make_app(cfg: Config, upstream: Upstream, store: MappingStore) -> web.Application:
    app = web.Application(
        middlewares=[
            admin_auth_middleware,
            access_log_middleware,
            error_middleware,
        ]
    )
    app["config"] = cfg
    app["upstream"] = upstream
    app["store"] = store

    app.router.add_get("/", lambda r: handle_root(r))
    app.router.add_get("/health", lambda r: handle_health(r))
    app.router.add_get(
        "/live/{name:.+\\.m3u8}", lambda r: handle_playlist(r, upstream, cfg)
    )
    app.router.add_get("/proxy", lambda r: handle_segment(r, upstream, cfg))

    # Admin
    app.router.add_get("/admin/", admin_index)
    app.router.add_get("/admin", admin_index)
    app.router.add_get("/admin/login", admin_login_get)
    app.router.add_post("/admin/login", admin_login_post)
    app.router.add_post("/admin/logout", admin_logout)
    app.router.add_get("/admin/mappings", admin_mappings_get)
    app.router.add_post("/admin/mappings", admin_mappings_post)
    app.router.add_post(
        "/admin/mappings/{id}/delete", admin_mapping_delete
    )
    app.router.add_get("/admin/health", admin_health)
    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run() -> None:
    _configure_logging()
    _load_env_file("/etc/tv-proxy.env")
    cfg = Config()

    if not cfg.access_token:
        logger.error("config-missing ACCESS_TOKEN; refusing to start")
        sys.exit(2)
    if not cfg.admin_user or not cfg.admin_password_hash_runtime():
        logger.warning(
            "config-partial ADMIN_USER/ADMIN_PASSWORD[_HASH] not set; "
            "/admin routes will return 401"
        )

    logger.info(
        "starting listen=%s:%s version=%s %s",
        LISTEN_HOST,
        LISTEN_PORT,
        APP_VERSION,
        cfg.describe(),
    )

    connector = aiohttp.TCPConnector(
        limit=20,
        limit_per_host=10,
        ttl_dns_cache=300,
        ssl=False,
    )
    timeout = aiohttp.ClientTimeout(
        total=None, connect=UPSTREAM_CONNECT_TIMEOUT
    )
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    upstream = Upstream(cfg, session)

    store = MappingStore(DB_PATH)
    await store.init()

    app = _make_app(cfg, upstream, store)
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