"""Admin web UI.

Provides:
  /admin/                     redirect to /admin/dashboard
  /admin/login (GET/POST)     form login
  /admin/logout (POST)        clear session cookie
  /admin/dashboard (GET)      overview
  /admin/connections (CRUD)
  /admin/channels (CRUD + test-stream)
  /admin/viewers (CRUD + token regenerate)
  /admin/sessions (list/terminate)
  /admin/admins (CRUD + change password)
  /admin/settings (GET/POST)
  /admin/logs (list)
  /admin/system (status + restart buttons)

The first admin can be created via POST /admin/admins/bootstrap when zero
admins exist.  Subsequent admin creation requires owner role.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from .security import constant_time_eq, token_is_valid


SESSION_COOKIE = "tv_session"
SESSION_MAX_AGE = 86400 * 7

logger = logging.getLogger("tvproxy")


# ---------------------------------------------------------------------------
# HTML helpers
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
           border-bottom: 1px solid #8884; padding-bottom: .5rem; margin-bottom: 1rem;
           flex-wrap: wrap; gap: .5rem; }
  h1 { font-size: 1.4rem; margin: 0; }
  nav.main { display: flex; flex-wrap: wrap; gap: .25rem 1rem; }
  nav.main a { padding: .2rem 0; }
  nav span.user { color: #888; margin-right: 1rem; font-size: .9em; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: .5rem .6rem; border-bottom: 1px solid #8884;
           text-align: left; vertical-align: top; }
  th { background: #8882; }
  code { background: #8883; padding: .1rem .3rem; border-radius: .2rem; }
  input[type=text], input[type=email], input[type=password], input[type=url],
  input[type=number], input[type=datetime-local], textarea, select {
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
  .login-card { max-width: 380px; margin: 5rem auto;
                padding: 1.5rem; border: 1px solid #8884; border-radius: .5rem; }
  pre.kv { background: #8882; padding: .8rem; border-radius: .3rem; overflow: auto; }
  .pill { display: inline-block; padding: .1rem .5rem; border-radius: 1rem;
          background: #8883; font-size: .85em; }
  .pill.ok   { background: #16653466; }
  .pill.err  { background: #991b1b66; }
  .pill.warn { background: #b4530966; }
</style>
</head>
<body>
"""


_HTML_FOOT = "</body></html>"


def _html_escape(value) -> str:
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


NAV_ITEMS = [
    ("Dashboard",   "/admin/dashboard"),
    ("Viewers",     "/admin/viewers"),
    ("Channels",    "/admin/channels"),
    ("Connections", "/admin/connections"),
    ("Sessions",    "/admin/sessions"),
    ("Logs",        "/admin/logs"),
    ("System",      "/admin/system"),
    ("Settings",    "/admin/settings"),
    ("Admins",      "/admin/admins"),
]


def _nav(request: web.Request, current: str) -> str:
    user = request.get("admin_user")
    if not user:
        return ""
    links = []
    for label, href in NAV_ITEMS:
        active = ' class="active"' if href == current else ""
        links.append(f'<a{active} href="{href}">{label}</a>')
    name = _html_escape(user.get("display_name") or user.get("email") or "")
    role = _html_escape(user.get("role", ""))
    user_html = (
        f'<span class="user">{name} <span class="pill">{role}</span></span>'
        '<form class="inline" method="post" action="/admin/logout">'
        '<button class="danger" type="submit">Logout</button></form>'
    )
    return f'<nav class="main">{"".join(links)}</nav>{user_html}'


def _render(
    request: web.Request, title: str, current: str, body: str, status: int = 200
) -> web.Response:
    nav = _nav(request, current)
    html = (
        _HTML_HEAD
        + f'<header><h1>{_html_escape(title)}</h1>{nav}</header>'
        + body
        + _HTML_FOOT
    )
    return web.Response(text=html, content_type="text/html", status=status)


def _flash_set(app: web.Application, kind: str, message: str) -> None:
    app["_flash"] = (kind, message)


def _flash_consume(app: web.Application) -> str:
    pair = app.pop("_flash", None)
    if not pair:
        return ""
    kind, message = pair
    return f'<div class="msg {kind}">{_html_escape(message)}</div>'


# ---------------------------------------------------------------------------
# Auth / sessions
# ---------------------------------------------------------------------------


async def _resolve_session(
    request: web.Request, admin_users
) -> Optional[dict]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        raw = base64.urlsafe_b64decode(cookie.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    parts = raw.split("|", 2)
    if len(parts) != 3:
        return None
    try:
        aid = int(parts[0])
    except ValueError:
        return None
    user = await admin_users.get(aid)
    if not user:
        return None
    if user["email"] != parts[1]:
        return None
    return user


async def _authenticate(request: web.Request, admin_users) -> Optional[dict]:
    # Cookie session
    user = await _resolve_session(request, admin_users)
    if user:
        return user
    # HTTP Basic
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", errors="replace")
            if ":" in raw:
                email, _, pw = raw.partition(":")
                return await admin_users.verify(email, pw)
        except Exception:
            return None
    return None


def _is_browser(request: web.Request) -> bool:
    ua = request.headers.get("User-Agent", "").lower()
    return any(
        x in ua for x in ("mozilla", "chrome", "safari", "firefox", "edge", "webkit")
    )


@web.middleware
async def admin_auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/admin"):
        return await handler(request)
    # Public admin paths
    if request.path in ("/admin/login",):
        return await handler(request)
    if request.path == "/admin/admins/bootstrap" and request.method == "POST":
        return await handler(request)

    admin_users = request.app["admin_users"]
    user = await _authenticate(request, admin_users)
    if user is None:
        if request.method == "GET" and _is_browser(request):
            loc = "/admin/login"
            if request.path != "/admin/":
                loc += "?next=" + request.path
                if request.query_string:
                    loc += "?" + request.query_string
            return web.Response(status=302, headers={"Location": loc}, text="")
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="TVProxy Admin", charset="UTF-8"'},
            text="Authentication required",
            content_type="text/plain",
        )
    request["admin_user"] = user
    return await handler(request)


# ---------------------------------------------------------------------------
# In-memory session registry (active viewer sessions)
# ---------------------------------------------------------------------------


class SessionRegistry:
    """Track currently-active HLS viewer sessions for /admin/sessions.

    In-memory only; lost on restart, by design (cheaper than writing to
    SQLite on every request).  Sessions older than STALE_AFTER are
    reaped on each call to list().
    """

    STALE_AFTER = 60.0  # seconds without activity before considered stale

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def touch(
        self,
        viewer_id: int,
        channel_id: int,
        request: web.Request,
        started_at: Optional[float] = None,
    ) -> None:
        ip = request.remote or ""
        ua = request.headers.get("User-Agent", "")[:200]
        key = f"{viewer_id}:{channel_id}:{ip}:{ua}"
        now = time.time()
        async with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                entry = {
                    "id": key,
                    "viewer_id": viewer_id,
                    "channel_id": channel_id,
                    "ip": ip,
                    "user_agent": ua,
                    "started_at": started_at or now,
                }
                self._sessions[key] = entry
            entry["last_seen"] = now

    async def list(self) -> list[dict]:
        now = time.time()
        async with self._lock:
            stale = [k for k, v in self._sessions.items()
                     if now - v.get("last_seen", 0) > self.STALE_AFTER]
            for k in stale:
                self._sessions.pop(k, None)
            return list(self._sessions.values())

    async def terminate(self, session_id: str) -> bool:
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def count(self) -> int:
        return len(await self.list())


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def csrf_token(request: web.Request) -> str:
    """Stateless CSRF token: derived from session cookie.  Both must be
    present and matched to validate a form post.
    """
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    secret = os.environ.get("TVPROXY_CSRF_SECRET", "change-me-csrf-secret")
    digest = hmac.new(secret.encode(), cookie.encode(), "sha256").hexdigest()[:16]
    return digest


async def check_csrf(request: web.Request) -> bool:
    if request.method != "POST":
        return True
    expected = await csrf_token(request)
    # In an htmx-less setup, accept form token OR a header.
    form_tok = (await request.post()).get("_csrf") if request.can_read_body else None
    if form_tok and constant_time_eq(str(form_tok), expected):
        return True
    header_tok = request.headers.get("X-CSRF-Token", "")
    if constant_time_eq(header_tok, expected):
        return True
    # Also accept if no cookie was set (first-admin bootstrap)
    if not request.cookies.get(SESSION_COOKIE):
        return True
    return False


@web.middleware
async def csrf_middleware(request: web.Request, handler):
    if not request.path.startswith("/admin"):
        return await handler(request)
    if request.method == "POST":
        if not await check_csrf(request):
            return web.Response(status=403, text="CSRF check failed",
                                content_type="text/plain")
    return await handler(request)


# ---------------------------------------------------------------------------
# Subapp factory
# ---------------------------------------------------------------------------


def make_admin_subapp(stores: dict) -> web.Application:
    sub = web.Application(middlewares=[admin_auth_middleware, csrf_middleware])

    sub["admin_users"] = stores["admin_users"]
    sub["viewers"]     = stores["viewers"]
    sub["channels"]    = stores["channels"]
    sub["connections"] = stores["connections"]
    sub["audit"]       = stores["audit"]
    sub["settings_store"] = stores["settings"]
    sub["sessions_registry"] = SessionRegistry()

    # Routes
    sub.router.add_get("/", admin_root)
    sub.router.add_get("/login", admin_login_get)
    sub.router.add_post("/login", admin_login_post)
    sub.router.add_post("/logout", admin_logout)

    sub.router.add_get("/dashboard", admin_dashboard)
    sub.router.add_get("/connections", admin_connections_get)
    sub.router.add_post("/connections", admin_connections_post)
    sub.router.add_get("/connections/{id}/edit", admin_connections_edit_get)
    sub.router.add_post("/connections/{id}", admin_connections_update)
    sub.router.add_post("/connections/{id}/delete", admin_connections_delete)
    sub.router.add_post("/connections/{id}/test", admin_connections_test)

    sub.router.add_get("/channels", admin_channels_get)
    sub.router.add_post("/channels", admin_channels_post)
    sub.router.add_get("/channels/{id}/edit", admin_channels_edit_get)
    sub.router.add_post("/channels/{id}", admin_channels_update)
    sub.router.add_post("/channels/{id}/delete", admin_channels_delete)
    sub.router.add_post("/channels/{id}/test", admin_channels_test)

    sub.router.add_get("/viewers", admin_viewers_get)
    sub.router.add_post("/viewers", admin_viewers_post)
    sub.router.add_get("/viewers/{id}/edit", admin_viewers_edit_get)
    sub.router.add_post("/viewers/{id}", admin_viewers_update)
    sub.router.add_post("/viewers/{id}/delete", admin_viewers_delete)
    sub.router.add_post("/viewers/{id}/token", admin_viewers_regen_token)

    sub.router.add_get("/sessions", admin_sessions_get)
    sub.router.add_post("/sessions/{id}/terminate", admin_sessions_terminate)

    sub.router.add_get("/admins", admin_admins_get)
    sub.router.add_post("/admins", admin_admins_post)
    sub.router.add_get("/admins/{id}/edit", admin_admins_edit_get)
    sub.router.add_post("/admins/{id}", admin_admins_update)
    sub.router.add_post("/admins/{id}/delete", admin_admins_delete)
    sub.router.add_post("/admins/{id}/password", admin_admins_change_password)
    sub.router.add_post("/admins/bootstrap", admin_admins_bootstrap)

    sub.router.add_get("/settings", admin_settings_get)
    sub.router.add_post("/settings", admin_settings_post)

    sub.router.add_get("/logs", admin_logs_get)

    sub.router.add_get("/system", admin_system_get)
    sub.router.add_post("/system/restart", admin_system_restart)

    return sub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_admin(request: web.Request) -> bool:
    u = request.get("admin_user")
    return bool(u and u.get("role") == "admin")


def _is_owner(request: web.Request) -> bool:
    u = request.get("admin_user")
    return bool(u and u.get("role") == "owner")


def _require_admin(request: web.Request) -> Optional[web.Response]:
    if not (request.get("admin_user")):
        return web.Response(status=401, text="Authentication required",
                            content_type="text/plain")
    return None


async def _emit_csrf_field(request: web.Request) -> str:
    tok = await csrf_token(request)
    return f'<input type="hidden" name="_csrf" value="{tok}">'


async def _audit(request: web.Request, action: str, target_type: str,
                 target_id: Optional[int], details: dict) -> None:
    user = request.get("admin_user") or {}
    ip = request.remote or ""
    await request.app["audit"].add(
        action=action,
        actor_id=user.get("id"),
        actor_email=user.get("email", ""),
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details, default=str),
        ip=ip,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def admin_root(request: web.Request) -> web.Response:
    raise web.HTTPFound("/admin/dashboard")


# ---- Login ----


def _login_html(error: Optional[str] = None, next_path: str = "/admin/") -> str:
    err = (
        f'<div class="msg err">{_html_escape(error)}</div>'
        if error
        else ""
    )
    return (
        _HTML_HEAD
        + '<div class="login-card">'
        + "<h1>TVProxy Admin login</h1>"
        + err
        + '<form method="post" action="/admin/login">'
        + f'<input type="hidden" name="next" value="{_html_escape(next_path)}">'
        + '<label>Email</label>'
        + '<input type="email" name="email" autocomplete="username" required>'
        + '<label>Password</label>'
        + '<input type="password" name="password" autocomplete="current-password" required>'
        + '<p><button class="primary" type="submit">Sign in</button></p>'
        + "</form></div>" + _HTML_FOOT
    )


async def admin_login_get(request: web.Request) -> web.Response:
    nxt = request.query.get("next") or "/admin/"
    if not nxt.startswith("/admin") or nxt.startswith("//"):
        nxt = "/admin/"
    return web.Response(text=_login_html(next_path=nxt), content_type="text/html")


async def admin_login_post(request: web.Request) -> web.Response:
    data = await request.post()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    nxt = (data.get("next") or "/admin/").strip()
    if not nxt.startswith("/admin") or nxt.startswith("//"):
        nxt = "/admin/"
    user = await request.app["admin_users"].verify(email, password)
    if not user:
        return web.Response(
            text=_login_html(error="Invalid email or password.", next_path=nxt),
            content_type="text/html",
            status=401,
        )
    # record login
    await request.app["admin_users"].record_login(user["id"], request.remote or "")
    await _audit(request, "admin.login", "admin_user", user["id"],
                 {"email": user["email"]})
    cookie_value = base64.urlsafe_b64encode(
        f"{user['id']}|{user['email']}|{int(time.time())}".encode()
    ).decode()
    raise web.HTTPFound(
        nxt,
        headers={
            "Set-Cookie": (
                f"{SESSION_COOKIE}={cookie_value}; HttpOnly; Path=/admin; "
                f"SameSite=Strict; Max-Age={SESSION_MAX_AGE}"
            )
        },
    )


async def admin_logout(request: web.Request) -> web.Response:
    user = request.get("admin_user")
    if user:
        await _audit(request, "admin.logout", "admin_user", user.get("id"), {})
    raise web.HTTPFound(
        "/admin/login",
        headers={
            "Set-Cookie": (
                f"{SESSION_COOKIE}=; HttpOnly; Path=/admin; "
                "SameSite=Strict; Max-Age=0"
            )
        },
    )


# ---- Dashboard ----


async def admin_dashboard(request: web.Request) -> web.Response:
    viewer_count = len(await request.app["viewers"].list_all())
    channel_count = len(await request.app["channels"].list_all())
    conn_count = len(await request.app["connections"].list_all())
    admin_count = await request.app["admin_users"].count()
    sessions = await request.app["sessions_registry"].list()
    recent_logs = await request.app["audit"].list_recent(limit=10)
    body = _flash_consume(request.app)
    body += (
        f'<p>Welcome, <code>{_html_escape(request["admin_user"]["email"])}</code>.</p>'
        '<div class="grid">'
        f'<div class="login-card"><h2>Viewers</h2><p style="font-size:2em;margin:0">{viewer_count}</p><a href="/admin/viewers">Manage</a></div>'
        f'<div class="login-card"><h2>Channels</h2><p style="font-size:2em;margin:0">{channel_count}</p><a href="/admin/channels">Manage</a></div>'
        f'<div class="login-card"><h2>Connections</h2><p style="font-size:2em;margin:0">{conn_count}</p><a href="/admin/connections">Manage</a></div>'
        f'<div class="login-card"><h2>Active sessions</h2><p style="font-size:2em;margin:0">{len(sessions)}</p><a href="/admin/sessions">View</a></div>'
        f'<div class="login-card"><h2>Admins</h2><p style="font-size:2em;margin:0">{admin_count}</p><a href="/admin/admins">Manage</a></div>'
        "</div>"
        + "<h2 style=\"margin-top:2rem\">Recent activity</h2>"
        + _render_audit_rows(recent_logs)
    )
    return _render(request, "Dashboard", "/admin/dashboard", body)


def _render_audit_rows(rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">No activity yet.</p>'
    out = ["<table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Details</th></tr></thead><tbody>"]
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        out.append(
            "<tr>"
            f"<td>{ts}</td>"
            f"<td>{_html_escape(r.get('actor_email', '-'))}</td>"
            f"<td><code>{_html_escape(r.get('action', '-'))}</code></td>"
            f"<td>{_html_escape(r.get('target_type', '-'))} #{_html_escape(r.get('target_id', '-'))}</td>"
            f"<td><code>{_html_escape(r.get('details', '')[:120])}</code></td>"
            "</tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


# ---- Connections ----


async def admin_connections_get(request: web.Request) -> web.Response:
    rows = await request.app["connections"].list_all()
    flash = _flash_consume(request.app)
    table = ""
    for r in rows:
        table += (
            "<tr>"
            f"<td>{r['id']}</td>"
            f"<td><code>{_html_escape(r['name'])}</code></td>"
            f"<td><code>{_html_escape(r['base_url'])}</code></td>"
            f"<td><code>{_html_escape(r['allowed_host'])}</code></td>"
            f"<td>{'on' if r['enabled'] else 'off'}</td>"
            f"<td>{_fmt_check(r.get('last_check_at'), r.get('last_check_ok'), r.get('last_check_ms'), r.get('last_check_msg'))}</td>"
            "<td>"
            f'<a href="/admin/connections/{r["id"]}/edit">Edit</a> &middot; '
            f'<form class="inline" method="post" action="/admin/connections/{r["id"]}/test">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button type="submit">Test</button></form> &middot; '
            f'<form class="inline" method="post" action="/admin/connections/{r["id"]}/delete" '
            'onsubmit="return confirm(\'Delete this connection?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button class="danger" type="submit">Delete</button></form>'
            "</td></tr>"
        )
    body = flash + "<h2>Connections</h2>" + (
        "<table><thead><tr>"
        "<th>ID</th><th>Name</th><th>Base URL</th><th>Allowed Host</th>"
        "<th>Enabled</th><th>Last check</th><th></th>"
        "</tr></thead><tbody>" + table + "</tbody></table>"
        if table else "<p class=\"muted\">No connections yet.</p>"
    ) + await _connection_form(request)
    return _render(request, "Connections", "/admin/connections", body)


async def _connection_form(request: web.Request, conn: Optional[dict] = None,
                           action: str = "/admin/connections",
                           submit: str = "Add connection",
                           extra: str = "") -> str:
    csrf = await _emit_csrf_field(request)
    c = conn or {}
    return (
        f'<h2 style="margin-top:2rem">{submit}</h2>'
        f"<form method=\"post\" action=\"{action}\">"
        f"{csrf}{extra}"
        '<div class="grid">'
        f'<div><label>Name</label><input name="name" type="text" required value="{_html_escape(c.get("name", ""))}"></div>'
        f'<div><label>Allowed host</label><input name="allowed_host" type="text" required value="{_html_escape(c.get("allowed_host", ""))}"></div>'
        "</div>"
        f'<label>Base URL (e.g. https://origin.example.com)</label>'
        f'<input name="base_url" type="url" required value="{_html_escape(c.get("base_url", ""))}">'
        f'<label>Authorization header (optional, full line e.g. "Bearer abc123")</label>'
        f'<input name="auth_header" type="text" value="{_html_escape(c.get("auth_header", ""))}">'
        '<div class="grid">'
        f'<div><label>Connect timeout (s)</label><input name="timeout_connect" type="number" min="1" max="60" value="{c.get("timeout_connect", 5)}"></div>'
        f'<div><label>Read timeout (s)</label><input name="timeout_read" type="number" min="1" max="600" value="{c.get("timeout_read", 30)}"></div>'
        "</div>"
        f'<label><input type="checkbox" name="enabled" value="1" {"checked" if c.get("enabled", True) else ""}> Enabled</label>'
        f'<p><button class="primary" type="submit">{submit}</button></p>'
        "</form>"
    )


def _fmt_check(at, ok, ms, msg) -> str:
    if at is None:
        return '<span class="pill warn">never</span>'
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))
    pill = "ok" if ok else "err"
    return (
        f'<span class="pill {pill}">{"OK" if ok else "FAIL"}</span> '
        f"{_html_escape(str(ms) + ' ms' if ms is not None else '')} "
        f'<span class="muted">{ts}</span><br>'
        f'<span class="muted">{_html_escape(msg or "")}</span>'
    )


async def admin_connections_post(request: web.Request) -> web.Response:
    data = await request.post()
    name = (data.get("name") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    auth_header = (data.get("auth_header") or "").strip()
    allowed_host = (data.get("allowed_host") or "").strip()
    try:
        timeout_connect = int(data.get("timeout_connect") or 5)
        timeout_read = int(data.get("timeout_read") or 30)
    except ValueError:
        _flash_set(request.app, "err", "Timeout must be a number.")
        raise web.HTTPFound("/admin/connections")
    if not name or not base_url or not allowed_host:
        _flash_set(request.app, "err", "name, base_url, allowed_host required.")
        raise web.HTTPFound("/admin/connections")
    ok, reason = await request.app["connections"].create(
        name, base_url, auth_header, allowed_host,
        timeout_connect, timeout_read,
    )
    if not ok:
        _flash_set(request.app, "err", f"Could not create connection: {reason}")
    else:
        await _audit(request, "connection.create", "connection", None,
                     {"name": name})
        _flash_set(request.app, "ok", f"Connection '{name}' created.")
    raise web.HTTPFound("/admin/connections")


async def admin_connections_edit_get(request: web.Request) -> web.Response:
    cid = int(request.match_info["id"])
    conn = await request.app["connections"].get(cid)
    if not conn:
        _flash_set(request.app, "err", "Connection not found.")
        raise web.HTTPFound("/admin/connections")
    csrf = await _emit_csrf_field(request)
    body = (
        f"<p><a href=\"/admin/connections\">&laquo; Back</a></p>"
        + await _connection_form(
            request, conn,
            action=f"/admin/connections/{cid}",
            submit="Save changes",
        )
    )
    return _render(request, f"Edit connection #{cid}",
                   "/admin/connections", body)


async def admin_connections_update(request: web.Request) -> web.Response:
    cid = int(request.match_info["id"])
    data = await request.post()
    name = (data.get("name") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    auth_header = (data.get("auth_header") or "").strip()
    allowed_host = (data.get("allowed_host") or "").strip()
    enabled = bool(data.get("enabled"))
    try:
        timeout_connect = int(data.get("timeout_connect") or 5)
        timeout_read = int(data.get("timeout_read") or 30)
    except ValueError:
        _flash_set(request.app, "err", "Timeout must be a number.")
        raise web.HTTPFound(f"/admin/connections/{cid}/edit")
    ok, reason = await request.app["connections"].update(
        cid, name, base_url, auth_header, allowed_host,
        timeout_connect, timeout_read, enabled,
    )
    if not ok:
        _flash_set(request.app, "err", f"Update failed: {reason}")
        raise web.HTTPFound(f"/admin/connections/{cid}/edit")
    await _audit(request, "connection.update", "connection", cid, {"name": name})
    _flash_set(request.app, "ok", f"Connection '{name}' updated.")
    raise web.HTTPFound("/admin/connections")


async def admin_connections_delete(request: web.Request) -> web.Response:
    cid = int(request.match_info["id"])
    conn = await request.app["connections"].get(cid)
    name = conn["name"] if conn else "?"
    if await request.app["connections"].delete(cid):
        await _audit(request, "connection.delete", "connection", cid, {"name": name})
        _flash_set(request.app, "ok", f"Connection '{name}' deleted.")
    else:
        _flash_set(request.app, "err", "Delete failed (in use?).")
    raise web.HTTPFound("/admin/connections")


async def admin_connections_test(request: web.Request) -> web.Response:
    cid = int(request.match_info["id"])
    conn = await request.app["connections"].get(cid)
    if not conn:
        _flash_set(request.app, "err", "Connection not found.")
        raise web.HTTPFound("/admin/connections")
    await _audit(request, "connection.test", "connection", cid,
                 {"name": conn["name"]})
    result = await _test_upstream(
        conn["base_url"], conn["allowed_host"], conn.get("auth_header", ""),
        conn.get("timeout_connect", 5), conn.get("timeout_read", 30),
    )
    await request.app["connections"].record_check(
        cid, result["ok"], result["ms"], result["msg"]
    )
    if result["ok"]:
        _flash_set(request.app, "ok",
                   f"OK {result['ms']}ms: {result['msg']}")
    else:
        _flash_set(request.app, "err",
                   f"FAIL: {result['msg']}")
    raise web.HTTPFound("/admin/connections")


async def _test_upstream(base_url: str, allowed_host: str, auth_header: str,
                         timeout_connect: int, timeout_read: int) -> dict:
    """Fetch base_url and return {ok, ms, msg}."""
    from .security import validate_upstream_url_runtime
    ok, reason = validate_upstream_url_runtime(base_url, allowed_host)
    if not ok:
        return {"ok": False, "ms": 0, "msg": f"blocked: {reason}"}
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    t0 = time.time()
    timeout = aiohttp.ClientTimeout(sock_connect=timeout_connect,
                                    sock_read=timeout_read)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(base_url, headers=headers,
                             allow_redirects=False) as resp:
                ms = int((time.time() - t0) * 1000)
                msg = f"HTTP {resp.status}"
                return {"ok": 200 <= resp.status < 400, "ms": ms, "msg": msg}
    except aiohttp.ClientError as exc:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "msg": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "msg": f"{type(exc).__name__}: {exc}"}


# ---- Channels ----


async def admin_channels_get(request: web.Request) -> web.Response:
    rows = await request.app["channels"].list_all()
    conns = {c["id"]: c for c in await request.app["connections"].list_all()}
    flash = _flash_consume(request.app)
    table = ""
    for r in rows:
        conn = conns.get(r["connection_id"])
        table += (
            "<tr>"
            f"<td>{r['id']}</td>"
            f"<td><code>{_html_escape(r['slug'])}</code></td>"
            f"<td>{_html_escape(r['display_name'])}</td>"
            f"<td>{_html_escape(conn['name']) if conn else '?'}</td>"
            f"<td><code>{_html_escape(r['upstream_path'])}</code></td>"
            f"<td>{'on' if r['enabled'] else 'off'}</td>"
            f"<td>{_fmt_check(r.get('last_check_at'), r.get('last_check_ok'), r.get('last_check_ms'), r.get('last_check_msg'))}</td>"
            "<td>"
            f'<a href="/admin/channels/{r["id"]}/edit">Edit</a> &middot; '
            f'<form class="inline" method="post" action="/admin/channels/{r["id"]}/test">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button type="submit">Test stream</button></form> &middot; '
            f'<form class="inline" method="post" action="/admin/channels/{r["id"]}/delete" '
            'onsubmit="return confirm(\'Delete this channel?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button class="danger" type="submit">Delete</button></form>'
            "</td></tr>"
        )
    body = flash + "<h2>Channels</h2>" + (
        "<table><thead><tr>"
        "<th>ID</th><th>Slug</th><th>Name</th><th>Connection</th>"
        "<th>Path</th><th>Enabled</th><th>Last check</th><th></th>"
        "</tr></thead><tbody>" + table + "</tbody></table>"
        if table else "<p class=\"muted\">No channels yet.</p>"
    ) + await _channel_form(request, conns)
    return _render(request, "Channels", "/admin/channels", body)


async def _channel_form(request: web.Request, conns: dict,
                        ch: Optional[dict] = None,
                        action: str = "/admin/channels",
                        submit: str = "Add channel") -> str:
    csrf = await _emit_csrf_field(request)
    c = ch or {}
    options = "".join(
        f'<option value="{cid}" {"selected" if c.get("connection_id") == cid else ""}>'
        f'{_html_escape(name)}</option>'
        for cid, conn in sorted(conns.items())
        for name in [conn["name"]]
    )
    return (
        f'<h2 style="margin-top:2rem">{submit}</h2>'
        f"<form method=\"post\" action=\"{action}\">{csrf}"
        '<div class="grid">'
        f'<div><label>Slug</label><input name="slug" type="text" pattern="[a-zA-Z0-9._-]{{1,64}}" required value="{_html_escape(c.get("slug", ""))}"></div>'
        f'<div><label>Display name</label><input name="display_name" type="text" required value="{_html_escape(c.get("display_name", ""))}"></div>'
        "</div>"
        f'<label>Connection</label><select name="connection_id" required>{options}</select>'
        f'<label>Upstream path (joined to connection base_url)</label>'
        f'<input name="upstream_path" type="text" required value="{_html_escape(c.get("upstream_path", ""))}">'
        '<div class="grid">'
        f'<div><label>Description</label><input name="description" type="text" value="{_html_escape(c.get("description", ""))}"></div>'
        f'<div><label>Logo URL</label><input name="logo_url" type="url" value="{_html_escape(c.get("logo_url", ""))}"></div>'
        "</div>"
        f'<label><input type="checkbox" name="enabled" value="1" {"checked" if c.get("enabled", True) else ""}> Enabled</label>'
        f'<p><button class="primary" type="submit">{submit}</button></p>'
        "</form>"
    )


async def admin_channels_post(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        cid = int(data.get("connection_id"))
    except (TypeError, ValueError):
        _flash_set(request.app, "err", "Choose a connection.")
        raise web.HTTPFound("/admin/channels")
    fields = {
        "slug": (data.get("slug") or "").strip(),
        "display_name": (data.get("display_name") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "logo_url": (data.get("logo_url") or "").strip(),
        "upstream_path": (data.get("upstream_path") or "").strip(),
    }
    if not all([fields["slug"], fields["display_name"], fields["upstream_path"]]):
        _flash_set(request.app, "err", "slug, display_name, path required.")
        raise web.HTTPFound("/admin/channels")
    ok, reason = await request.app["channels"].create(
        fields["slug"], fields["display_name"], fields["description"],
        fields["logo_url"], cid, fields["upstream_path"],
    )
    if not ok:
        _flash_set(request.app, "err", f"Could not create channel: {reason}")
    else:
        await _audit(request, "channel.create", "channel", None, fields)
        _flash_set(request.app, "ok", f"Channel '{fields['slug']}' created.")
    raise web.HTTPFound("/admin/channels")


async def admin_channels_edit_get(request: web.Request) -> web.Response:
    chid = int(request.match_info["id"])
    ch = await request.app["channels"].get(chid)
    if not ch:
        _flash_set(request.app, "err", "Channel not found.")
        raise web.HTTPFound("/admin/channels")
    conns = {c["id"]: c for c in await request.app["connections"].list_all()}
    body = (
        f"<p><a href=\"/admin/channels\">&laquo; Back</a></p>"
        + await _channel_form(
            request, conns, ch,
            action=f"/admin/channels/{chid}",
            submit="Save changes",
        )
    )
    return _render(request, f"Edit channel #{chid}",
                   "/admin/channels", body)


async def admin_channels_update(request: web.Request) -> web.Response:
    chid = int(request.match_info["id"])
    data = await request.post()
    try:
        cid = int(data.get("connection_id"))
    except (TypeError, ValueError):
        _flash_set(request.app, "err", "Choose a connection.")
        raise web.HTTPFound(f"/admin/channels/{chid}/edit")
    fields = {
        "slug": (data.get("slug") or "").strip(),
        "display_name": (data.get("display_name") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "logo_url": (data.get("logo_url") or "").strip(),
        "upstream_path": (data.get("upstream_path") or "").strip(),
    }
    enabled = bool(data.get("enabled"))
    ok, reason = await request.app["channels"].update(
        chid, fields["slug"], fields["display_name"], fields["description"],
        fields["logo_url"], cid, fields["upstream_path"], enabled,
    )
    if not ok:
        _flash_set(request.app, "err", f"Update failed: {reason}")
        raise web.HTTPFound(f"/admin/channels/{chid}/edit")
    await _audit(request, "channel.update", "channel", chid, fields)
    _flash_set(request.app, "ok", f"Channel '{fields['slug']}' updated.")
    raise web.HTTPFound("/admin/channels")


async def admin_channels_delete(request: web.Request) -> web.Response:
    chid = int(request.match_info["id"])
    if await request.app["channels"].delete(chid):
        await _audit(request, "channel.delete", "channel", chid, {})
        _flash_set(request.app, "ok", f"Channel #{chid} deleted.")
    else:
        _flash_set(request.app, "err", "Delete failed.")
    raise web.HTTPFound("/admin/channels")


async def admin_channels_test(request: web.Request) -> web.Response:
    chid = int(request.match_info["id"])
    ch = await request.app["channels"].get(chid)
    if not ch:
        _flash_set(request.app, "err", "Channel not found.")
        raise web.HTTPFound("/admin/channels")
    conn = await request.app["connections"].get(ch["connection_id"])
    if not conn:
        _flash_set(request.app, "err", "Connection missing.")
        raise web.HTTPFound("/admin/channels")
    upstream = conn["base_url"].rstrip("/") + "/" + ch["upstream_path"].lstrip("/")
    await _audit(request, "channel.test", "channel", chid,
                 {"slug": ch["slug"]})
    result = await _test_channel(upstream, conn["allowed_host"],
                                 conn.get("auth_header", ""))
    await request.app["channels"].record_check(
        chid, result["ok"], result["ms"], result["msg"], result.get("variants", 0)
    )
    msg = (f"OK {result['ms']}ms — {result['msg']} (variants: "
           f"{result.get('variants', 0)})" if result["ok"]
           else f"FAIL: {result['msg']}")
    _flash_set(request.app, "ok" if result["ok"] else "err", msg)
    raise web.HTTPFound("/admin/channels")


async def _test_channel(upstream: str, allowed_host: str, auth_header: str) -> dict:
    from .security import validate_upstream_url_runtime
    from .streaming import classify_hls
    ok, reason = validate_upstream_url_runtime(upstream, allowed_host)
    if not ok:
        return {"ok": False, "ms": 0, "msg": f"blocked: {reason}"}
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    timeout = aiohttp.ClientTimeout(sock_connect=5, sock_read=15)
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(upstream, headers=headers,
                             allow_redirects=False) as resp:
                ms = int((time.time() - t0) * 1000)
                if resp.status != 200:
                    return {"ok": False, "ms": ms, "msg": f"HTTP {resp.status}"}
                body = await resp.text()
                info = classify_hls(body)
                if info["kind"] == "unknown":
                    return {"ok": False, "ms": ms,
                            "msg": "Response is not HLS",
                            "variants": 0}
                kind_label = {
                    "master": "master playlist",
                    "media": "media playlist",
                }.get(info["kind"], info["kind"])
                return {
                    "ok": True,
                    "ms": ms,
                    "msg": f"HTTP 200 — {kind_label}",
                    "variants": info["variants"],
                }
    except aiohttp.ClientError as exc:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "msg": f"{type(exc).__name__}: {exc}"}


# ---- Viewers ----


async def admin_viewers_get(request: web.Request) -> web.Response:
    rows = await request.app["viewers"].list_all()
    flash = _flash_consume(request.app)
    table = ""
    now = time.time()
    for r in rows:
        exp = r.get("expires_at")
        exp_str = (
            "no expiry"
            if exp is None else
            time.strftime("%Y-%m-%d", time.localtime(exp))
        )
        if exp is not None and exp < now:
            exp_pill = ' <span class="pill err">expired</span>'
        else:
            exp_pill = ""
        last = r.get("last_seen_at")
        last_str = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
            if last else "never"
        )
        muted_dash = '<span class="muted">-</span>'
        name_cell = _html_escape(r['name']) or muted_dash
        table += (
            "<tr>"
            f"<td>{r['id']}</td>"
            f"<td>{name_cell}</td>"
            f"<td><code>{_html_escape(r['token_prefix'])}***</code></td>"
            f"<td>{r['max_connections']}</td>"
            f"<td>{exp_str}{exp_pill}</td>"
            f"<td>{'disabled' if r['disabled'] else 'enabled'}</td>"
            f"<td>{last_str}</td>"
            "<td>"
            f'<a href="/admin/viewers/{r["id"]}/edit">Edit</a> &middot; '
            f'<form class="inline" method="post" action="/admin/viewers/{r["id"]}/token" '
            'onsubmit="return confirm(\'Regenerate access token? Old token stops working immediately.\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button type="submit">New token</button></form> &middot; '
            f'<form class="inline" method="post" action="/admin/viewers/{r["id"]}/delete" '
            'onsubmit="return confirm(\'Delete this viewer?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button class="danger" type="submit">Delete</button></form>'
            "</td></tr>"
        )
    body = flash + "<h2>Viewers</h2>" + (
        "<table><thead><tr>"
        "<th>ID</th><th>Name</th><th>Token</th><th>Max conn</th>"
        "<th>Expires</th><th>Status</th><th>Last seen</th><th></th>"
        "</tr></thead><tbody>" + table + "</tbody></table>"
        if table else "<p class=\"muted\">No viewers yet.</p>"
    ) + await _viewer_form(request)
    return _render(request, "Viewers", "/admin/viewers", body)


async def _viewer_form(request: web.Request, viewer: Optional[dict] = None,
                       action: str = "/admin/viewers",
                       submit: str = "Add viewer") -> str:
    csrf = await _emit_csrf_field(request)
    v = viewer or {}
    channels = await request.app["channels"].list_all()
    selected = set(v.get("allowed_channel_ids") or [])
    options = "".join(
        f'<label><input type="checkbox" name="allowed_channel_ids" value="{c["id"]}" '
        f'{"checked" if c["id"] in selected else ""}> '
        f'{_html_escape(c["display_name"])} ({_html_escape(c["slug"])})</label>'
        for c in channels
    )
    exp_val = ""
    if v.get("expires_at"):
        exp_val = time.strftime("%Y-%m-%dT%H:%M",
                                time.localtime(v["expires_at"]))
    return (
        f'<h2 style="margin-top:2rem">{submit}</h2>'
        f"<form method=\"post\" action=\"{action}\">{csrf}"
        '<div class="grid">'
        f'<div><label>Name</label><input name="name" type="text" value="{_html_escape(v.get("name", ""))}"></div>'
        f'<div><label>Description</label><input name="description" type="text" value="{_html_escape(v.get("description", ""))}"></div>'
        "</div>"
        '<div class="grid">'
        f'<div><label>Max simultaneous connections</label><input name="max_connections" type="number" min="1" max="50" value="{v.get("max_connections", 1)}"></div>'
        f'<div><label>Expires at (optional)</label><input name="expires_at" type="datetime-local" value="{exp_val}"></div>'
        "</div>"
        f'<label><input type="checkbox" name="disabled" value="1" {"checked" if v.get("disabled") else ""}> Disabled</label>'
        f'<label>Allowed channels (none = all)</label>'
        f'<div style="border:1px solid #8886;border-radius:.3rem;padding:.5rem">{options or "<span class=muted>No channels configured yet.</span>"}</div>'
        f'<p><button class="primary" type="submit">{submit}</button></p>'
        "</form>"
    )


async def admin_viewers_post(request: web.Request) -> web.Response:
    data = await request.post()
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    try:
        max_conn = int(data.get("max_connections") or 1)
    except ValueError:
        max_conn = 1
    exp_raw = (data.get("expires_at") or "").strip()
    expires_at = None
    if exp_raw:
        try:
            expires_at = time.mktime(time.strptime(exp_raw, "%Y-%m-%dT%H:%M"))
        except ValueError:
            _flash_set(request.app, "err", "Invalid expiry date.")
            raise web.HTTPFound("/admin/viewers")
    allowed = [int(x) for x in data.getall("allowed_channel_ids") if x]
    ok, reason, viewer = await request.app["viewers"].create(
        name, description, allowed, max_conn, expires_at,
    )
    if not ok:
        _flash_set(request.app, "err", f"Could not create viewer: {reason}")
        raise web.HTTPFound("/admin/viewers")
    await _audit(request, "viewer.create", "viewer", viewer["id"],
                 {"name": name})
    _flash_set(request.app, "ok",
               f"Viewer created. Token: <code>{viewer['token']}</code>")
    raise web.HTTPFound(f"/admin/viewers/{viewer['id']}/edit")


async def admin_viewers_edit_get(request: web.Request) -> web.Response:
    vid = int(request.match_info["id"])
    viewer = await request.app["viewers"].get(vid)
    if not viewer:
        _flash_set(request.app, "err", "Viewer not found.")
        raise web.HTTPFound("/admin/viewers")
    body = (
        f"<p><a href=\"/admin/viewers\">&laquo; Back</a></p>"
        f"<p><strong>Token:</strong> <code>{viewer['token']}</code></p>"
        + await _viewer_form(
            request, viewer,
            action=f"/admin/viewers/{vid}",
            submit="Save changes",
        )
    )
    return _render(request, f"Edit viewer #{vid}",
                   "/admin/viewers", body)


async def admin_viewers_update(request: web.Request) -> web.Response:
    vid = int(request.match_info["id"])
    data = await request.post()
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    try:
        max_conn = int(data.get("max_connections") or 1)
    except ValueError:
        max_conn = 1
    exp_raw = (data.get("expires_at") or "").strip()
    expires_at = None
    if exp_raw:
        try:
            expires_at = time.mktime(time.strptime(exp_raw, "%Y-%m-%dT%H:%M"))
        except ValueError:
            _flash_set(request.app, "err", "Invalid expiry date.")
            raise web.HTTPFound(f"/admin/viewers/{vid}/edit")
    disabled = bool(data.get("disabled"))
    allowed = [int(x) for x in data.getall("allowed_channel_ids") if x]
    ok, reason = await request.app["viewers"].update(
        vid, name, description, allowed, max_conn, expires_at, disabled,
    )
    if not ok:
        _flash_set(request.app, "err", f"Update failed: {reason}")
        raise web.HTTPFound(f"/admin/viewers/{vid}/edit")
    await _audit(request, "viewer.update", "viewer", vid,
                 {"name": name, "disabled": disabled})
    _flash_set(request.app, "ok", "Viewer updated.")
    raise web.HTTPFound("/admin/viewers")


async def admin_viewers_delete(request: web.Request) -> web.Response:
    vid = int(request.match_info["id"])
    if await request.app["viewers"].delete(vid):
        await _audit(request, "viewer.delete", "viewer", vid, {})
        _flash_set(request.app, "ok", f"Viewer #{vid} deleted.")
    raise web.HTTPFound("/admin/viewers")


async def admin_viewers_regen_token(request: web.Request) -> web.Response:
    vid = int(request.match_info["id"])
    new = await request.app["viewers"].regenerate_token(vid)
    if new:
        await _audit(request, "viewer.token_regenerate", "viewer", vid, {})
        _flash_set(request.app, "ok",
                   f"New token: <code>{new}</code>")
    raise web.HTTPFound(f"/admin/viewers/{vid}/edit")


# ---- Sessions ----


async def admin_sessions_get(request: web.Request) -> web.Response:
    sessions = await request.app["sessions_registry"].list()
    viewers = {v["id"]: v for v in await request.app["viewers"].list_all()}
    channels = {c["id"]: c for c in await request.app["channels"].list_all()}
    flash = _flash_consume(request.app)
    rows = ""
    for s in sessions:
        v = viewers.get(s["viewer_id"])
        c = channels.get(s["channel_id"])
        started = time.strftime("%H:%M:%S", time.localtime(s["started_at"]))
        last = time.strftime("%H:%M:%S", time.localtime(s["last_seen"]))
        dur = int(s["last_seen"] - s["started_at"])
        rows += (
            "<tr>"
            f"<td>{_html_escape((v['name'] or '-') if v else str(s['viewer_id']))}</td>"
            f"<td>{_html_escape((c['display_name'] or c['slug']) if c else str(s['channel_id']))}</td>"
            f"<td>{_html_escape(s['ip'])}</td>"
            f"<td>{started} → {last} ({dur}s)</td>"
            f"<td><span class=\"muted\">{_html_escape(s['user_agent'][:60])}</span></td>"
            "<td>"
            f'<form class="inline" method="post" action="/admin/sessions/{s["id"]}/terminate" '
            'onsubmit="return confirm(\'Terminate this session?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button class="danger" type="submit">Terminate</button></form>'
            "</td></tr>"
        )
    body = flash + "<h2>Active sessions</h2>" + (
        "<p class=\"muted\">In-memory only; sessions are reaped after 60s of inactivity. "
        "Terminate will block new segment requests from this viewer/channel pair.</p>"
        "<table><thead><tr>"
        "<th>Viewer</th><th>Channel</th><th>IP</th><th>Started → Last</th><th>UA</th><th></th>"
        "</tr></thead><tbody>" + rows + "</tbody></table>"
        if rows else "<p class=\"muted\">No active sessions.</p>"
    )
    return _render(request, "Sessions", "/admin/sessions", body)


async def admin_sessions_terminate(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    if await request.app["sessions_registry"].terminate(sid):
        await _audit(request, "session.terminate", "session", None,
                     {"id": sid})
        _flash_set(request.app, "ok", "Session terminated.")
    raise web.HTTPFound("/admin/sessions")


# ---- Admins ----


async def admin_admins_get(request: web.Request) -> web.Response:
    rows = await request.app["admin_users"].list_all()
    flash = _flash_consume(request.app)
    table = ""
    for r in rows:
        last = r.get("last_login_at")
        last_str = (time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
                    if last else "never")
        table += (
            "<tr>"
            f"<td>{r['id']}</td>"
            f"<td><code>{_html_escape(r['email'])}</code></td>"
            f"<td>{_html_escape(r['display_name'])}</td>"
            f"<td><span class=\"pill\">{_html_escape(r['role'])}</span></td>"
            f"<td>{last_str} from {_html_escape(r.get('last_login_ip', '-'))}</td>"
            "<td>"
            f'<a href="/admin/admins/{r["id"]}/edit">Edit</a> &middot; '
            f'<form class="inline" method="post" action="/admin/admins/{r["id"]}/password" '
            'onsubmit="return confirm(\'Reset password?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button type="submit">Set password</button></form> &middot; '
            f'<form class="inline" method="post" action="/admin/admins/{r["id"]}/delete" '
            'onsubmit="return confirm(\'Delete this admin?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<button class="danger" type="submit">Delete</button></form>'
            "</td></tr>"
        )
    body = flash + "<h2>Admins</h2>" + (
        "<table><thead><tr>"
        "<th>ID</th><th>Email</th><th>Name</th><th>Role</th>"
        "<th>Last login</th><th></th>"
        "</tr></thead><tbody>" + table + "</tbody></table>"
        if table else "<p class=\"muted\">No admins.</p>"
    ) + await _admin_form(request)
    return _render(request, "Admins", "/admin/admins", body)


async def _admin_form(request: web.Request, admin: Optional[dict] = None,
                      action: str = "/admin/admins",
                      submit: str = "Add admin") -> str:
    csrf = await _emit_csrf_field(request)
    a = admin or {}
    return (
        f'<h2 style="margin-top:2rem">{submit}</h2>'
        f"<form method=\"post\" action=\"{action}\">{csrf}"
        '<div class="grid">'
        f'<div><label>Email</label><input name="email" type="email" required value="{_html_escape(a.get("email", ""))}"></div>'
        f'<div><label>Display name</label><input name="display_name" type="text" value="{_html_escape(a.get("display_name", ""))}"></div>'
        "</div>"
        '<div class="grid">'
        f'<div><label>Password</label><input name="password" type="password" minlength="4"></div>'
        f'<div><label>Role</label><select name="role"><option value="admin" {"selected" if a.get("role") != "owner" else ""}>admin</option><option value="owner" {"selected" if a.get("role") == "owner" else ""}>owner</option></select></div>'
        "</div>"
        f'<p><button class="primary" type="submit">{submit}</button></p>'
        "</form>"
    )


async def admin_admins_post(request: web.Request) -> web.Response:
    if not _is_owner(request):
        return web.Response(status=403, text="Owner only",
                            content_type="text/plain")
    data = await request.post()
    email = (data.get("email") or "").strip()
    name = (data.get("display_name") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or "admin"
    ok, reason = await request.app["admin_users"].create(
        email, name, password, role,
    )
    if not ok:
        _flash_set(request.app, "err", f"Create failed: {reason}")
    else:
        await _audit(request, "admin.create", "admin_user", None,
                     {"email": email, "role": role})
        _flash_set(request.app, "ok", f"Admin '{email}' created.")
    raise web.HTTPFound("/admin/admins")


async def admin_admins_edit_get(request: web.Request) -> web.Response:
    aid = int(request.match_info["id"])
    admin = await request.app["admin_users"].get(aid)
    if not admin:
        _flash_set(request.app, "err", "Admin not found.")
        raise web.HTTPFound("/admin/admins")
    body = (
        f"<p><a href=\"/admin/admins\">&laquo; Back</a></p>"
        + await _admin_form(
            request, admin,
            action=f"/admin/admins/{aid}",
            submit="Save changes",
        )
    )
    return _render(request, f"Edit admin #{aid}",
                   "/admin/admins", body)


async def admin_admins_update(request: web.Request) -> web.Response:
    if not _is_owner(request):
        return web.Response(status=403, text="Owner only",
                            content_type="text/plain")
    aid = int(request.match_info["id"])
    data = await request.post()
    email = (data.get("email") or "").strip()
    name = (data.get("display_name") or "").strip()
    password_raw = data.get("password") or ""
    role = data.get("role") or "admin"
    password = password_raw if password_raw else None
    ok, reason = await request.app["admin_users"].update(
        aid, email, name, role, password,
    )
    if not ok:
        _flash_set(request.app, "err", f"Update failed: {reason}")
        raise web.HTTPFound(f"/admin/admins/{aid}/edit")
    await _audit(request, "admin.update", "admin_user", aid, {"email": email})
    _flash_set(request.app, "ok", "Admin updated.")
    raise web.HTTPFound("/admin/admins")


async def admin_admins_delete(request: web.Request) -> web.Response:
    if not _is_owner(request):
        return web.Response(status=403, text="Owner only",
                            content_type="text/plain")
    aid = int(request.match_info["id"])
    me = request["admin_user"]
    if aid == me["id"]:
        _flash_set(request.app, "err", "Cannot delete yourself.")
        raise web.HTTPFound("/admin/admins")
    rows = await request.app["admin_users"].list_all()
    if len([r for r in rows if r["role"] == "owner"]) <= 1:
        target = next((r for r in rows if r["id"] == aid), None)
        if target and target["role"] == "owner":
            _flash_set(request.app, "err", "Refusing to delete the last owner.")
            raise web.HTTPFound("/admin/admins")
    if await request.app["admin_users"].delete(aid):
        await _audit(request, "admin.delete", "admin_user", aid, {})
        _flash_set(request.app, "ok", f"Admin #{aid} deleted.")
    raise web.HTTPFound("/admin/admins")


async def admin_admins_change_password(request: web.Request) -> web.Response:
    aid = int(request.match_info["id"])
    # owner or self can change password
    me = request["admin_user"]
    if not (_is_owner(request) or aid == me["id"]):
        return web.Response(status=403, text="Forbidden",
                            content_type="text/plain")
    data = await request.post()
    pw = data.get("password") or ""
    if not pw:
        _flash_set(request.app, "err", "Password required.")
        raise web.HTTPFound("/admin/admins")
    if await request.app["admin_users"].change_password(aid, pw):
        await _audit(request, "admin.password_change", "admin_user", aid, {})
        _flash_set(request.app, "ok", "Password changed.")
    raise web.HTTPFound("/admin/admins")


async def admin_admins_bootstrap(request: web.Request) -> web.Response:
    """First-admin bootstrap: open only when zero admins exist."""
    n = await request.app["admin_users"].count()
    if n > 0:
        return web.Response(status=403, text="Bootstrap closed",
                            content_type="text/plain")
    data = await request.post()
    email = (data.get("email") or "").strip()
    name = (data.get("display_name") or "").strip()
    password = data.get("password") or ""
    role = "owner"
    ok, reason = await request.app["admin_users"].create(email, name, password, role)
    if not ok:
        return web.Response(status=400, text=f"create failed: {reason}",
                            content_type="text/plain")
    await request.app["audit"].add(
        action="admin.bootstrap", actor_id=None, actor_email=email,
        target_type="admin_user", target_id=None,
        details="{}", ip=request.remote or "",
    )
    raise web.HTTPFound("/admin/login")


# ---- Settings ----


async def admin_settings_get(request: web.Request) -> web.Response:
    if not _is_owner(request):
        return web.Response(status=403, text="Owner only",
                            content_type="text/plain")
    settings = await request.app["settings_store"].get_all()
    flash = _flash_consume(request.app)
    csrf = await _emit_csrf_field(request)
    body = flash + (
        f"<form method=\"post\" action=\"/admin/settings\">{csrf}"
        '<label>Public URL (used in M3U and player links)</label>'
        f'<input name="public_url" type="url" required value="{_html_escape(settings.get("public_url", ""))}">'
        '<div class="grid">'
        f'<div><label>Default max connections</label><input name="default_max_connections" type="number" min="1" max="50" value="{_html_escape(settings.get("default_max_connections", "1"))}"></div>'
        f'<div><label>Default expires days (0 = no expiry)</label><input name="default_expires_days" type="number" min="0" max="3650" value="{_html_escape(settings.get("default_expires_days", "0"))}"></div>'
        "</div>"
        '<div class="grid">'
        f'<div><label>Session timeout seconds (0 = no timeout)</label><input name="session_timeout_seconds" type="number" min="0" max="86400" value="{_html_escape(settings.get("session_timeout_seconds", "0"))}"></div>'
        f'<div><label>Upstream connect timeout (s)</label><input name="upstream_connect_timeout" type="number" min="1" max="60" value="{_html_escape(settings.get("upstream_connect_timeout", "5"))}"></div>'
        "</div>"
        '<div class="grid">'
        f'<div><label>Upstream read timeout (s)</label><input name="upstream_read_timeout" type="number" min="1" max="600" value="{_html_escape(settings.get("upstream_read_timeout", "30"))}"></div>'
        f'<div><label>Log retention (days)</label><input name="log_retention_days" type="number" min="1" max="3650" value="{_html_escape(settings.get("log_retention_days", "30"))}"></div>'
        "</div>"
        '<p><button class="primary" type="submit">Save settings</button></p>'
        "</form>"
    )
    return _render(request, "Settings", "/admin/settings", body)


async def admin_settings_post(request: web.Request) -> web.Response:
    if not _is_owner(request):
        return web.Response(status=403, text="Owner only",
                            content_type="text/plain")
    data = await request.post()
    keys = [
        "public_url", "default_max_connections", "default_expires_days",
        "session_timeout_seconds", "upstream_connect_timeout",
        "upstream_read_timeout", "log_retention_days",
    ]
    updates = {k: str(data.get(k) or "").strip() for k in keys}
    await request.app["settings_store"].set_many(updates)
    # prune audit log
    try:
        days = int(updates["log_retention_days"])
    except ValueError:
        days = 30
    pruned = await request.app["audit"].prune_older_than(days)
    await _audit(request, "settings.update", "settings", None,
                 {"pruned_audit_rows": pruned, **updates})
    _flash_set(request.app, "ok", f"Settings saved (pruned {pruned} old audit rows).")
    raise web.HTTPFound("/admin/settings")


# ---- Logs ----


async def admin_logs_get(request: web.Request) -> web.Response:
    action_filter = request.query.get("action") or None
    actor_filter = request.query.get("actor") or None
    rows = await request.app["audit"].list_recent(
        limit=500, action_filter=action_filter, actor_filter=actor_filter,
    )
    flash = _flash_consume(request.app)
    csrf = await _emit_csrf_field(request)
    body = flash + (
        f"<form method=\"get\" action=\"/admin/logs\">{csrf}"
        '<div class="grid">'
        f'<div><label>Action filter (prefix)</label><input name="action" type="text" value="{_html_escape(action_filter or "")}"></div>'
        f'<div><label>Actor email contains</label><input name="actor" type="text" value="{_html_escape(actor_filter or "")}"></div>'
        "</div>"
        '<p><button type="submit">Filter</button> '
        '<a href="/admin/logs">Clear</a></p></form>'
        + _render_audit_rows(rows)
    )
    return _render(request, "Logs", "/admin/logs", body)


# ---- System ----


async def admin_system_get(request: web.Request) -> web.Response:
    csrf = await _emit_csrf_field(request)
    # We can't run systemctl from this process (no auth); instruct admin
    # to use the buttons which hit our restart endpoint.
    services = ["tv-proxy", "nginx", "cloudflared"]
    rows = ""
    for s in services:
        active = (await _read_text(
            f"/run/systemd/system/{s}.service.wants/"  # dummy probe
        )) is not None
        rows += (
            "<tr>"
            f"<td><code>{s}</code></td>"
            "<td>see systemctl on host</td>"
            f"<td><form class=\"inline\" method=\"post\" action=\"/admin/system/restart\" "
            'onsubmit="return confirm(\'Restart ' + s + '?\')">'
            f'{_html_escape(await _emit_csrf_field(request))}'
            f'<input type="hidden" name="service" value="{s}">'
            f'<button type="submit">Restart</button></form></td>'
            "</tr>"
        )
    body = (
        "<p>System status is approximate; full health checks live in "
        "individual sections. Use the Restart buttons below; "
        "tv-proxy, nginx and cloudflared are managed by systemd.</p>"
        '<table><thead><tr><th>Service</th><th>Status</th><th></th></tr></thead><tbody>'
        + rows + "</tbody></table>"
    )
    return _render(request, "System", "/admin/system", body)


async def admin_system_restart(request: web.Request) -> web.Response:
    if not _is_owner(request):
        return web.Response(status=403, text="Owner only",
                            content_type="text/plain")
    data = await request.post()
    service = (data.get("service") or "").strip()
    allowed = {"tv-proxy", "nginx", "cloudflared"}
    if service not in allowed:
        _flash_set(request.app, "err", "Unknown service.")
        raise web.HTTPFound("/admin/system")
    await _audit(request, "system.restart", "service", None, {"service": service})
    # Spawn the restart in a subprocess; do not await; do not block the
    # response.  Best-effort; failures are logged via journalctl.
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "systemctl", "restart", service,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    asyncio.create_task(proc.wait())
    _flash_set(request.app, "ok", f"Restart of {service} initiated.")
    raise web.HTTPFound("/admin/system")


async def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except (FileNotFoundError, PermissionError):
        return None