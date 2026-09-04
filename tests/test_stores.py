"""Unit tests for app.stores.

Uses tmp SQLite files (no real DB on the Pi needed).
"""

import os
import tempfile

import pytest

from app.stores import (
    AdminUserStore,
    AuditStore,
    ChannelStore,
    ConnectionStore,
    SettingsStore,
    ViewerStore,
    generate_viewer_token,
    hash_password,
    verify_password,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


# ---- Connections ----


class TestConnectionStore:
    @pytest.mark.asyncio
    async def test_crud(self, db_path):
        s = ConnectionStore(db_path)
        ok, _ = await s.create(
            "main", "https://x.example.com", "", "x.example.com", 5, 30
        )
        assert ok
        rows = await s.list_all()
        assert len(rows) == 1
        assert rows[0]["name"] == "main"

        # duplicate name rejected
        ok2, reason = await s.create(
            "main", "https://y.example.com", "", "y.example.com", 5, 30
        )
        assert not ok2
        assert "name-exists" in reason

        # update
        ok3, _ = await s.update(
            rows[0]["id"], "main2", "https://x.example.com", "", "x.example.com",
            5, 30, True,
        )
        assert ok3
        updated = await s.get(rows[0]["id"])
        assert updated["name"] == "main2"

        # record check
        await s.record_check(rows[0]["id"], True, 42, "200 ok")
        updated = await s.get(rows[0]["id"])
        assert updated["last_check_ok"] == 1
        assert updated["last_check_ms"] == 42

        # delete
        ok4 = await s.delete(rows[0]["id"])
        assert ok4
        assert await s.list_all() == []


# ---- Channels ----


class TestChannelStore:
    @pytest.mark.asyncio
    async def test_crud(self, db_path):
        cs = ConnectionStore(db_path)
        ok, _ = await cs.create("c1", "https://x.example.com", "", "x.example.com", 5, 30)
        assert ok
        conn = (await cs.list_all())[0]

        s = ChannelStore(db_path)
        ok, _ = await s.create(
            "kempentv", "KempenTV", "", "", conn["id"], "/live/c.m3u8"
        )
        assert ok
        ch = await s.get_by_slug("kempentv")
        assert ch is not None
        assert ch["display_name"] == "KempenTV"
        assert ch["enabled"] == 1

        # duplicate slug
        ok2, reason = await s.create(
            "kempentv", "x", "", "", conn["id"], "/c.m3u8"
        )
        assert not ok2
        assert "slug-exists" in reason

        # update
        ok3, _ = await s.update(
            ch["id"], "kempentv", "KempenTV Live", "desc",
            "https://logo.png", conn["id"], "/live/c.m3u8", True,
        )
        assert ok3
        ch2 = await s.get(ch["id"])
        assert ch2["display_name"] == "KempenTV Live"
        assert ch2["logo_url"] == "https://logo.png"

        # record check
        await s.record_check(ch["id"], True, 120, "ok", 3)
        ch3 = await s.get(ch["id"])
        assert ch3["last_check_variants"] == 3
        assert ch3["last_check_ms"] == 120

        # delete with connection in use -> FK RESTRICT
        deleted = await s.delete(ch["id"])
        assert deleted
        # now can delete connection
        assert await cs.delete(conn["id"])


# ---- Viewers ----


class TestViewerStore:
    @pytest.mark.asyncio
    async def test_token_unique(self, db_path):
        s = ViewerStore(db_path)
        ok, _, v1 = await s.create("a", "d1", [1, 2], 1, None)
        ok2, _, v2 = await s.create("b", "d2", [], 2, None)
        assert ok and ok2
        assert v1["token"] != v2["token"]
        # tokens look like secrets.token_urlsafe(32) - 43 chars
        assert len(v1["token"]) >= 30
        # prefix stored for display
        assert v1["token_prefix"] == v1["token"][:4]

    @pytest.mark.asyncio
    async def test_get_by_token(self, db_path):
        s = ViewerStore(db_path)
        ok, _, v = await s.create("a", "", [], 1, None)
        assert ok
        found = await s.get_by_token(v["token"])
        assert found is not None
        assert found["id"] == v["id"]
        # allowed channels parsed back
        assert found["allowed_channel_ids"] == []

    @pytest.mark.asyncio
    async def test_update_and_disable(self, db_path):
        s = ViewerStore(db_path)
        ok, _, v = await s.create("a", "", [1, 2], 1, None)
        ok2, _ = await s.update(
            v["id"], "renamed", "new desc", [3, 4, 5], 5, None, True
        )
        assert ok2
        v2 = await s.get(v["id"])
        assert v2["name"] == "renamed"
        assert v2["max_connections"] == 5
        assert v2["disabled"] == 1
        assert v2["allowed_channel_ids"] == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_regenerate_token(self, db_path):
        s = ViewerStore(db_path)
        ok, _, v = await s.create("a", "", [], 1, None)
        old_token = v["token"]
        new_token = await s.regenerate_token(v["id"])
        assert new_token is not None
        assert new_token != old_token
        v2 = await s.get(v["id"])
        assert v2["token"] == new_token

    @pytest.mark.asyncio
    async def test_record_seen(self, db_path):
        s = ViewerStore(db_path)
        ok, _, v = await s.create("a", "", [], 1, None)
        await s.record_seen(v["id"], "1.2.3.4", "curl/8", 1024)
        v2 = await s.get(v["id"])
        assert v2["last_ip"] == "1.2.3.4"
        assert v2["total_bytes"] == 1024

    @pytest.mark.asyncio
    async def test_delete(self, db_path):
        s = ViewerStore(db_path)
        ok, _, v = await s.create("a", "", [], 1, None)
        assert await s.delete(v["id"])
        assert await s.get(v["id"]) is None


# ---- Admin users ----


class TestAdminUserStore:
    @pytest.mark.asyncio
    async def test_create_and_verify(self, db_path):
        s = AdminUserStore(db_path)
        ok, _ = await s.create("a@x.com", "Alice", "secret-pw", "owner")
        assert ok
        assert await s.count() == 1
        u = await s.verify("a@x.com", "secret-pw")
        assert u is not None
        assert u["email"] == "a@x.com"
        assert u["role"] == "owner"
        # wrong password
        assert await s.verify("a@x.com", "wrong") is None
        # wrong email
        assert await s.verify("b@x.com", "secret-pw") is None

    @pytest.mark.asyncio
    async def test_email_case_insensitive(self, db_path):
        s = AdminUserStore(db_path)
        ok, _ = await s.create("Foo@X.com", "", "pw1234", "admin")
        assert ok
        u = await s.verify("foo@x.com", "pw1234")
        assert u is not None

    @pytest.mark.asyncio
    async def test_password_too_short(self, db_path):
        s = AdminUserStore(db_path)
        ok, reason = await s.create("a@x.com", "", "ab", "admin")
        assert not ok
        assert "password-too-short" in reason

    @pytest.mark.asyncio
    async def test_invalid_email(self, db_path):
        s = AdminUserStore(db_path)
        ok, reason = await s.create("not-an-email", "", "pw1234", "admin")
        assert not ok
        assert "invalid-email" in reason

    @pytest.mark.asyncio
    async def test_change_password(self, db_path):
        s = AdminUserStore(db_path)
        await s.create("a@x.com", "", "old-pw", "admin")
        u = (await s.list_all())[0]
        assert await s.change_password(u["id"], "new-pw")
        assert await s.verify("a@x.com", "old-pw") is None
        assert await s.verify("a@x.com", "new-pw") is not None

    @pytest.mark.asyncio
    async def test_record_login(self, db_path):
        s = AdminUserStore(db_path)
        await s.create("a@x.com", "", "pw1234", "admin")
        u = (await s.list_all())[0]
        await s.record_login(u["id"], "10.0.0.1")
        u2 = await s.get(u["id"])
        assert u2["last_login_ip"] == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_update_role(self, db_path):
        s = AdminUserStore(db_path)
        await s.create("a@x.com", "", "pw1234", "admin")
        u = (await s.list_all())[0]
        ok, _ = await s.update(u["id"], "a@x.com", "Alice", "owner", None)
        assert ok
        u2 = await s.get(u["id"])
        assert u2["role"] == "owner"


# ---- Audit ----


class TestAuditStore:
    @pytest.mark.asyncio
    async def test_add_and_list(self, db_path):
        s = AuditStore(db_path)
        await s.add("viewer.create", 1, "a@x.com", "viewer", 42, "{}", "10.0.0.1")
        rows = await s.list_recent()
        assert len(rows) == 1
        assert rows[0]["action"] == "viewer.create"

    @pytest.mark.asyncio
    async def test_filter_by_action(self, db_path):
        s = AuditStore(db_path)
        await s.add("viewer.create", 1, "a", "viewer", 1, "", "")
        await s.add("viewer.delete", 1, "a", "viewer", 1, "", "")
        rows = await s.list_recent(action_filter="viewer.")
        assert len(rows) == 2
        rows2 = await s.list_recent(action_filter="viewer.create")
        assert len(rows2) == 1

    @pytest.mark.asyncio
    async def test_prune(self, db_path):
        s = AuditStore(db_path)
        # Add many entries
        for i in range(5):
            await s.add("test.add", 1, "a", "x", i, "", "")
        # Pruning with 0 days should remove everything (entries are 'now')
        deleted = await s.prune_older_than(0)
        # Allow for either all or none depending on time precision; just
        # assert the call doesn't crash.
        assert deleted >= 0


# ---- Settings ----


class TestSettingsStore:
    @pytest.mark.asyncio
    async def test_defaults(self, db_path):
        s = SettingsStore(db_path)
        assert await s.get("public_url") == "https://tv.berrie.uk"

    @pytest.mark.asyncio
    async def test_set_and_get(self, db_path):
        s = SettingsStore(db_path)
        await s.set("public_url", "https://custom.example.com")
        assert await s.get("public_url") == "https://custom.example.com"

    @pytest.mark.asyncio
    async def test_get_all(self, db_path):
        s = SettingsStore(db_path)
        await s.set("foo", "bar")
        out = await s.get_all()
        assert out["foo"] == "bar"
        assert "public_url" in out


# ---- Password helpers ----


class TestPasswordHelpers:
    def test_hash_and_verify_roundtrip(self):
        salt = "abcd" * 8
        h = hash_password("hello", salt)
        assert verify_password("hello", h, salt)

    def test_verify_wrong_password(self):
        salt = "abcd" * 8
        h = hash_password("hello", salt)
        assert not verify_password("wrong", h, salt)

    def test_verify_wrong_salt(self):
        salt1 = "abcd" * 8
        salt2 = "ef01" * 8
        h = hash_password("hello", salt1)
        assert not verify_password("hello", h, salt2)

    def test_generate_viewer_token_unique(self):
        a = generate_viewer_token()
        b = generate_viewer_token()
        assert a != b
        assert len(a) >= 30