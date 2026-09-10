"""Integration tests via aiohttp test client.

These exercise the full stack: middleware, routes, stores, against a
fresh in-memory SQLite database.
"""

import asyncio
import base64
import os
import sys
import tempfile

import pytest

from aiohttp.test_utils import TestClient, TestServer

# Ensure /opt/tv-proxy is on the path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import admin as admin_mod
from app import main as main_mod
from app.stores import (
    AdminUserStore,
    AuditStore,
    ChannelStore,
    ConnectionStore,
    RecordingStore,
    SettingsStore,
    ViewerStore,
)


@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def stores(tmp_db_path):
    s = {
        "viewers":     ViewerStore(tmp_db_path),
        "channels":    ChannelStore(tmp_db_path),
        "connections": ConnectionStore(tmp_db_path),
        "admin_users": AdminUserStore(tmp_db_path),
        "audit":       AuditStore(tmp_db_path),
        "settings":    SettingsStore(tmp_db_path),
        "recordings":  RecordingStore(tmp_db_path),
    }
    for store in s.values():
        store.init_schema()
    return s


@pytest.fixture
async def client(stores):
    import aiohttp
    from aiohttp import web
    session = aiohttp.ClientSession()
    # Mount the subapp at its production prefix so middleware sees /admin paths.
    root = web.Application()
    root.add_subapp("/admin", admin_mod.make_admin_subapp(stores))
    server = TestServer(root)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await session.close()
        await test_client.close()


class TestLogin:
    async def test_login_get_renders_form(self, client):
        resp = await client.get("/admin/login")
        assert resp.status == 200
        body = await resp.text()
        assert "TVProxy Admin login" in body
        assert 'name="email"' in body
        assert 'name="password"' in body

    async def test_login_post_success(self, client, stores):
        await stores["admin_users"].create(
            "a@x.com", "Alice", "secret", "owner"
        )
        resp = await client.post(
            "/admin/login",
            data={"email": "a@x.com", "password": "secret", "next": "/admin/"},
            allow_redirects=False,
        )
        assert resp.status == 302
        # Session cookie set
        assert "tv_session" in [c.key for c in client.session.cookie_jar]
        # Follow redirect
        resp2 = await client.get("/admin/")
        assert resp2.status == 200

    async def test_login_post_invalid(self, client, stores):
        await stores["admin_users"].create(
            "a@x.com", "Alice", "secret", "owner"
        )
        resp = await client.post(
            "/admin/login",
            data={"email": "a@x.com", "password": "wrong"},
            allow_redirects=False,
        )
        assert resp.status == 401
        body = await resp.text()
        assert "Invalid email or password" in body

    async def test_login_no_csrf_token_needed(self, client, stores):
        """Login form has no _csrf field.  Must still succeed.

        Regression test for: previously the CSRF middleware blocked
        login because no session cookie was set yet.
        """
        await stores["admin_users"].create(
            "a@x.com", "Alice", "secret", "owner"
        )
        resp = await client.post(
            "/admin/login",
            data={"email": "a@x.com", "password": "secret"},
            allow_redirects=False,
        )
        assert resp.status == 302
        # The response must NOT be 403 "CSRF check failed"
        assert resp.status != 403


class TestAuthRequired:
    async def test_root_requires_login(self, client):
        resp = await client.get("/admin/", allow_redirects=False)
        # Browser gets redirected, programmatic gets 401
        assert resp.status in (302, 401)

    async def test_dashboard_requires_login(self, client):
        resp = await client.get("/admin/dashboard", allow_redirects=False)
        assert resp.status in (302, 401)


class TestAdminBootstrap:
    async def test_bootstrap_creates_first_admin(self, client, stores):
        resp = await client.post(
            "/admin/admins/bootstrap",
            data={"email": "first@x.com", "password": "pw1234",
                  "display_name": "First"},
            allow_redirects=False,
        )
        assert resp.status == 302
        # Admin exists now
        assert await stores["admin_users"].count() == 1
        u = await stores["admin_users"].verify("first@x.com", "pw1234")
        assert u is not None
        assert u["role"] == "owner"

    async def test_bootstrap_closed_after_first_admin(self, client, stores):
        await stores["admin_users"].create("a@x.com", "A", "pw1234", "owner")
        resp = await client.post(
            "/admin/admins/bootstrap",
            data={"email": "b@x.com", "password": "pw1234"},
            allow_redirects=False,
        )
        assert resp.status == 403

    async def test_bootstrap_does_not_require_csrf(self, client):
        """Bootstrap has no session cookie by definition.  Must work."""
        resp = await client.post(
            "/admin/admins/bootstrap",
            data={"email": "x@x.com", "password": "pw1234"},
            allow_redirects=False,
        )
        assert resp.status == 302
        assert resp.status != 403


class TestCsrfProtected:
    async def test_post_without_csrf_is_rejected(self, client, stores):
        # Log in first
        await stores["admin_users"].create(
            "a@x.com", "Alice", "secret", "owner"
        )
        await client.post(
            "/admin/login",
            data={"email": "a@x.com", "password": "secret"},
            allow_redirects=False,
        )
        # Now try to POST without _csrf
        resp = await client.post(
            "/admin/viewers",
            data={"name": "Bob"},
            allow_redirects=False,
        )
        assert resp.status == 403

    async def test_post_with_csrf_succeeds(self, client, stores):
        await stores["admin_users"].create(
            "a@x.com", "Alice", "secret", "owner"
        )
        # Login
        await client.post(
            "/admin/login",
            data={"email": "a@x.com", "password": "secret"},
            allow_redirects=False,
        )
        # Fetch dashboard to get CSRF token
        resp = await client.get("/admin/dashboard")
        body = await resp.text()
        # Extract CSRF token from form
        import re
        m = re.search(r'_csrf" value="([a-f0-9]+)"', body)
        assert m, "no _csrf token found"
        csrf = m.group(1)
        # POST with CSRF
        resp = await client.post(
            "/admin/viewers",
            data={
                "_csrf": csrf,
                "name": "Bob",
                "max_connections": "1",
            },
            allow_redirects=False,
        )
        # Should NOT be 403
        assert resp.status != 403


class TestLogout:
    async def test_logout_clears_session(self, client, stores):
        await stores["admin_users"].create(
            "a@x.com", "Alice", "secret", "owner"
        )
        await client.post(
            "/admin/login",
            data={"email": "a@x.com", "password": "secret"},
            allow_redirects=False,
        )
        # Verify we're logged in
        resp = await client.get("/admin/dashboard")
        assert resp.status == 200
        import re
        csrf = re.search(r'name="_csrf" value="([a-f0-9]+)"', await resp.text()).group(1)
        # Logout
        resp = await client.post("/admin/logout", data={"_csrf": csrf}, allow_redirects=False)
        assert resp.status == 302
        # Now dashboard requires auth again
        resp = await client.get("/admin/dashboard", allow_redirects=False)
        assert resp.status in (302, 401)
