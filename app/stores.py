"""SQLite-backed stores for TVProxy.

All I/O happens here.  No HTTP, no template rendering.  Tests can
construct a Store against a temporary file and exercise CRUD without
running aiohttp.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Iterable, Optional, Tuple

import bcrypt


SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    base_url        TEXT NOT NULL,
    auth_header     TEXT NOT NULL DEFAULT '',
    allowed_host    TEXT NOT NULL,
    timeout_connect INTEGER NOT NULL DEFAULT 5,
    timeout_read    INTEGER NOT NULL DEFAULT 30,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_check_at   REAL,
    last_check_ok   INTEGER,
    last_check_ms   INTEGER,
    last_check_msg  TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    logo_url        TEXT NOT NULL DEFAULT '',
    connection_id   INTEGER NOT NULL REFERENCES connections(id) ON DELETE RESTRICT,
    upstream_path   TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_check_at   REAL,
    last_check_ok   INTEGER,
    last_check_ms   INTEGER,
    last_check_msg  TEXT,
    last_check_variants INTEGER,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS channels_slug_idx ON channels(slug);
CREATE INDEX IF NOT EXISTS channels_connection_idx ON channels(connection_id);

CREATE TABLE IF NOT EXISTS viewers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    token           TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL DEFAULT '',
    short_code      TEXT UNIQUE,
    short_enabled   INTEGER NOT NULL DEFAULT 1,
    allowed_channels TEXT NOT NULL DEFAULT '',
    favorite_channels TEXT NOT NULL DEFAULT '',
    max_connections INTEGER NOT NULL DEFAULT 1,
    expires_at      REAL,
    disabled        INTEGER NOT NULL DEFAULT 0,
    last_seen_at    REAL,
    last_ip         TEXT NOT NULL DEFAULT '',
    last_user_agent TEXT NOT NULL DEFAULT '',
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS viewers_token_idx ON viewers(token);

CREATE TABLE IF NOT EXISTS admin_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL,
    salt            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin',
    last_login_at   REAL,
    last_login_ip   TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    actor_id        INTEGER,
    actor_email     TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL,
    target_type     TEXT NOT NULL DEFAULT '',
    target_id       INTEGER,
    details         TEXT NOT NULL DEFAULT '',
    ip              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    token_hash  TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    expires_at  REAL,
    created_at  REAL NOT NULL,
    last_used_at REAL
);

CREATE TABLE IF NOT EXISTS recordings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id   INTEGER NOT NULL REFERENCES viewers(id) ON DELETE CASCADE,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT '',
    start_at    REAL NOT NULL,
    end_at      REAL NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'UTC',
    status      TEXT NOT NULL DEFAULT 'scheduled',
    local_path  TEXT NOT NULL DEFAULT '',
    remote_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS recordings_schedule_idx ON recordings(status, start_at);
CREATE INDEX IF NOT EXISTS recordings_viewer_idx ON recordings(viewer_id, start_at DESC);
"""


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _BaseStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._initialized = False

    def _ensure_dir(self) -> None:
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)

    def init_schema(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._ensure_dir()
            conn = _connect(self.path)
            try:
                conn.executescript(SCHEMA)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(viewers)")}
                if "short_code" not in columns:
                    conn.execute("ALTER TABLE viewers ADD COLUMN short_code TEXT")
                if "short_enabled" not in columns:
                    conn.execute("ALTER TABLE viewers ADD COLUMN short_enabled INTEGER NOT NULL DEFAULT 1")
                if "favorite_channels" not in columns:
                    conn.execute("ALTER TABLE viewers ADD COLUMN favorite_channels TEXT NOT NULL DEFAULT ''")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS viewers_short_code_idx ON viewers(short_code)")
                conn.execute("UPDATE viewers SET short_code=lower(substr(hex(randomblob(3)),1,4)) WHERE short_code IS NULL")
                self._initialized = True
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        self.init_schema()
        return _connect(self.path)


# ---- Connections ----


class ConnectionStore(_BaseStore):
    async def list_all(self) -> list[dict]:
        def _do():
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM connections ORDER BY id ASC"
                ).fetchall()]
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get(self, cid: int) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM connections WHERE id = ?", (cid,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def create(
        self,
        name: str,
        base_url: str,
        auth_header: str,
        allowed_host: str,
        timeout_connect: int,
        timeout_read: int,
    ) -> Tuple[bool, str]:
        now = time.time()
        def _do():
            conn = self._conn()
            try:
                try:
                    conn.execute(
                        """INSERT INTO connections
                           (name, base_url, auth_header, allowed_host,
                            timeout_connect, timeout_read, enabled,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (name, base_url, auth_header, allowed_host,
                         timeout_connect, timeout_read, now, now),
                    )
                    return True, "ok"
                except sqlite3.IntegrityError as exc:
                    return False, f"name-exists: {exc}"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def update(
        self,
        cid: int,
        name: str,
        base_url: str,
        auth_header: str,
        allowed_host: str,
        timeout_connect: int,
        timeout_read: int,
        enabled: bool,
    ) -> Tuple[bool, str]:
        now = time.time()
        def _do():
            conn = self._conn()
            try:
                try:
                    cur = conn.execute(
                        """UPDATE connections SET
                             name=?, base_url=?, auth_header=?, allowed_host=?,
                             timeout_connect=?, timeout_read=?, enabled=?,
                             updated_at=?
                           WHERE id=?""",
                        (name, base_url, auth_header, allowed_host,
                         timeout_connect, timeout_read, 1 if enabled else 0,
                         now, cid),
                    )
                    if cur.rowcount == 0:
                        return False, "not-found"
                    return True, "ok"
                except sqlite3.IntegrityError as exc:
                    return False, f"name-exists: {exc}"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def delete(self, cid: int) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM connections WHERE id = ?", (cid,))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def record_check(
        self, cid: int, ok: bool, ms: int, msg: str
    ) -> None:
        def _do():
            conn = self._conn()
            try:
                conn.execute(
                    """UPDATE connections SET
                         last_check_at=?, last_check_ok=?,
                         last_check_ms=?, last_check_msg=?
                       WHERE id=?""",
                    (time.time(), 1 if ok else 0, ms, msg[:200], cid),
                )
            finally:
                conn.close()
        await asyncio.to_thread(_do)


# ---- Channels ----


class ChannelStore(_BaseStore):
    async def list_all(self) -> list[dict]:
        def _do():
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM channels ORDER BY id ASC"
                ).fetchall()]
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get(self, chid: int) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM channels WHERE id = ?", (chid,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get_by_slug(self, slug: str) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM channels WHERE slug = ? AND enabled = 1",
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
        description: str,
        logo_url: str,
        connection_id: int,
        upstream_path: str,
    ) -> Tuple[bool, str]:
        now = time.time()
        def _do():
            conn = self._conn()
            try:
                try:
                    conn.execute(
                        """INSERT INTO channels
                           (slug, display_name, description, logo_url,
                            connection_id, upstream_path, enabled,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (slug, display_name, description, logo_url,
                         connection_id, upstream_path, now, now),
                    )
                    return True, "ok"
                except sqlite3.IntegrityError as exc:
                    return False, f"slug-exists: {exc}"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def update(
        self,
        chid: int,
        slug: str,
        display_name: str,
        description: str,
        logo_url: str,
        connection_id: int,
        upstream_path: str,
        enabled: bool,
    ) -> Tuple[bool, str]:
        now = time.time()
        def _do():
            conn = self._conn()
            try:
                try:
                    cur = conn.execute(
                        """UPDATE channels SET
                             slug=?, display_name=?, description=?, logo_url=?,
                             connection_id=?, upstream_path=?, enabled=?,
                             updated_at=?
                           WHERE id=?""",
                        (slug, display_name, description, logo_url,
                         connection_id, upstream_path, 1 if enabled else 0,
                         now, chid),
                    )
                    if cur.rowcount == 0:
                        return False, "not-found"
                    return True, "ok"
                except sqlite3.IntegrityError as exc:
                    return False, f"slug-exists: {exc}"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def delete(self, chid: int) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM channels WHERE id = ?", (chid,))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def record_check(
        self, chid: int, ok: bool, ms: int, msg: str, variants: int
    ) -> None:
        def _do():
            conn = self._conn()
            try:
                conn.execute(
                    """UPDATE channels SET
                         last_check_at=?, last_check_ok=?, last_check_ms=?,
                         last_check_msg=?, last_check_variants=?
                       WHERE id=?""",
                    (time.time(), 1 if ok else 0, ms, msg[:200], variants, chid),
                )
            finally:
                conn.close()
        await asyncio.to_thread(_do)


# ---- Viewers ----


def generate_viewer_token() -> str:
    """Cryptographically strong, URL-safe, ~43 chars."""
    return secrets.token_urlsafe(32)


def generate_viewer_short_code() -> str:
    return secrets.token_urlsafe(3).replace("_", "A").replace("-", "B")[:4]


def hash_password(password: str, salt_hex: str) -> str:
    salted = (salt_hex + ":" + password).encode("utf-8")
    return bcrypt.hashpw(salted, bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    if not password_hash or not salt_hex:
        return False
    salted = (salt_hex + ":" + password).encode("utf-8")
    try:
        return bcrypt.checkpw(salted, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _parse_allowed_channels(json_str: str) -> list[int]:
    if not json_str:
        return []
    try:
        v = json.loads(json_str)
        if isinstance(v, list):
            return [int(x) for x in v if isinstance(x, (int, float))]
    except (ValueError, TypeError):
        pass
    return []


def _dump_allowed_channels(ids: Iterable[int]) -> str:
    return json.dumps(sorted(set(int(i) for i in ids)))


class ViewerStore(_BaseStore):
    async def list_all(self) -> list[dict]:
        def _do():
            conn = self._conn()
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM viewers ORDER BY id ASC"
                ).fetchall()]
                for r in rows:
                    r["allowed_channel_ids"] = _parse_allowed_channels(
                        r.get("allowed_channels", "")
                    )
                    r["favorite_channel_ids"] = _parse_allowed_channels(r.get("favorite_channels", ""))
                return rows
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get(self, vid: int) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM viewers WHERE id = ?", (vid,)).fetchone()
                if not row:
                    return None
                d = dict(row)
                d["allowed_channel_ids"] = _parse_allowed_channels(
                    d.get("allowed_channels", "")
                )
                d["favorite_channel_ids"] = _parse_allowed_channels(d.get("favorite_channels", ""))
                return d
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get_by_token(self, token: str) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM viewers WHERE token = ?", (token,)
                ).fetchone()
                if not row:
                    return None
                d = dict(row)
                d["allowed_channel_ids"] = _parse_allowed_channels(
                    d.get("allowed_channels", "")
                )
                d["favorite_channel_ids"] = _parse_allowed_channels(d.get("favorite_channels", ""))
                return d
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get_by_short_code(self, code: str) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM viewers WHERE short_code = ? AND short_enabled = 1", (code,)
                ).fetchone()
                if not row:
                    return None
                d = dict(row)
                d["allowed_channel_ids"] = _parse_allowed_channels(d.get("allowed_channels", ""))
                d["favorite_channel_ids"] = _parse_allowed_channels(d.get("favorite_channels", ""))
                return d
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def create(
        self,
        name: str,
        description: str,
        allowed_channel_ids: Iterable[int],
        max_connections: int,
        expires_at: Optional[float],
    ) -> Tuple[bool, str, Optional[dict]]:
        token = generate_viewer_token()
        token_prefix = token[:4]
        short_code = generate_viewer_short_code()
        now = time.time()
        allowed_json = _dump_allowed_channels(allowed_channel_ids)

        def _do():
            conn = self._conn()
            try:
                cur = conn.execute(
                    """INSERT INTO viewers
                       (name, description, token, token_prefix, short_code,
                        allowed_channels, max_connections, expires_at,
                        disabled, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (name, description, token, token_prefix, short_code,
                     allowed_json, max_connections, expires_at,
                     now, now),
                )
                return cur.lastrowid
            finally:
                conn.close()

        rowid = await asyncio.to_thread(_do)
        viewer = await self.get(rowid)
        return True, "ok", viewer

    async def update(
        self,
        vid: int,
        name: str,
        description: str,
        allowed_channel_ids: Iterable[int],
        max_connections: int,
        expires_at: Optional[float],
        disabled: bool,
    ) -> Tuple[bool, str]:
        now = time.time()
        allowed_json = _dump_allowed_channels(allowed_channel_ids)

        def _do():
            conn = self._conn()
            try:
                cur = conn.execute(
                    """UPDATE viewers SET
                         name=?, description=?, allowed_channels=?,
                         max_connections=?, expires_at=?, disabled=?,
                         updated_at=?
                       WHERE id=?""",
                    (name, description, allowed_json, max_connections,
                     expires_at, 1 if disabled else 0, now, vid),
                )
                if cur.rowcount == 0:
                    return False, "not-found"
                return True, "ok"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def set_favorites(self, vid: int, channel_ids: Iterable[int]) -> bool:
        now = time.time()
        favorite_json = _dump_allowed_channels(channel_ids)
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("UPDATE viewers SET favorite_channels=?, updated_at=? WHERE id=?", (favorite_json, now, vid))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def regenerate_token(self, vid: int) -> Optional[str]:
        token = generate_viewer_token()
        token_prefix = token[:4]
        now = time.time()

        def _do():
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE viewers SET token=?, token_prefix=?, updated_at=? WHERE id=?",
                    (token, token_prefix, now, vid),
                )
                return cur.rowcount > 0
            finally:
                conn.close()
        ok = await asyncio.to_thread(_do)
        return token if ok else None

    async def set_short_access(self, vid: int, enabled: bool) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("UPDATE viewers SET short_enabled=?, updated_at=? WHERE id=?", (1 if enabled else 0, time.time(), vid))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def regenerate_short_code(self, vid: int) -> Optional[str]:
        for _ in range(10):
            code = generate_viewer_short_code()
            def _do():
                conn = self._conn()
                try:
                    cur = conn.execute("UPDATE viewers SET short_code=?, short_enabled=1, updated_at=? WHERE id=?", (code, time.time(), vid))
                    return cur.rowcount > 0
                except sqlite3.IntegrityError:
                    return False
                finally:
                    conn.close()
            if await asyncio.to_thread(_do):
                return code
        return None

    async def delete(self, vid: int) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM viewers WHERE id = ?", (vid,))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def record_seen(
        self,
        vid: int,
        ip: str,
        user_agent: str,
        bytes_added: int = 0,
    ) -> None:
        def _do():
            conn = self._conn()
            try:
                conn.execute(
                    """UPDATE viewers SET
                         last_seen_at=?, last_ip=?, last_user_agent=?,
                         total_bytes=total_bytes+?
                       WHERE id=?""",
                    (time.time(), ip, user_agent[:200], bytes_added, vid),
                )
            finally:
                conn.close()
        await asyncio.to_thread(_do)


# ---- Admin users ----


class AdminUserStore(_BaseStore):
    async def count(self) -> int:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM admin_users").fetchone()
                return int(row["n"])
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def list_all(self) -> list[dict]:
        def _do():
            conn = self._conn()
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, email, display_name, role, "
                    "last_login_at, last_login_ip, created_at, updated_at "
                    "FROM admin_users ORDER BY id ASC"
                ).fetchall()]
                return rows
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get_by_email(self, email: str) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM admin_users WHERE email = ?",
                    (email.strip().lower(),),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get(self, aid: int) -> Optional[dict]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT id, email, display_name, role, "
                    "last_login_at, last_login_ip, created_at, updated_at "
                    "FROM admin_users WHERE id = ?",
                    (aid,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def verify(self, email: str, password: str) -> Optional[dict]:
        user = await self.get_by_email(email)
        if not user:
            return None
        if not verify_password(password, user["password_hash"], user["salt"]):
            return None
        return user

    async def create(
        self,
        email: str,
        display_name: str,
        password: str,
        role: str,
    ) -> Tuple[bool, str]:
        email = email.strip().lower()
        if "@" not in email:
            return False, "invalid-email"
        if not password or len(password) < 4:
            return False, "password-too-short"
        if role not in ("owner", "admin"):
            role = "admin"
        salt_hex = secrets.token_hex(16)
        pw_hash = hash_password(password, salt_hex)
        now = time.time()

        def _do():
            conn = self._conn()
            try:
                try:
                    conn.execute(
                        """INSERT INTO admin_users
                           (email, display_name, password_hash, salt, role,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (email, display_name, pw_hash, salt_hex, role,
                         now, now),
                    )
                    return True, "ok"
                except sqlite3.IntegrityError:
                    return False, "email-exists"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def update(
        self,
        aid: int,
        email: str,
        display_name: str,
        role: str,
        password: Optional[str],
    ) -> Tuple[bool, str]:
        email = email.strip().lower()
        if "@" not in email:
            return False, "invalid-email"
        if role not in ("owner", "admin"):
            role = "admin"
        if password is not None and len(password) < 4:
            return False, "password-too-short"
        now = time.time()
        pw_clause = ""
        params: list = [email, display_name, role, now]
        if password:
            salt_hex = secrets.token_hex(16)
            pw_hash = hash_password(password, salt_hex)
            pw_clause = ", password_hash=?, salt=?"
            params.extend([pw_hash, salt_hex])
        params.append(aid)

        def _do():
            conn = self._conn()
            try:
                try:
                    cur = conn.execute(
                        f"UPDATE admin_users SET email=?, display_name=?, role=?, "
                        f"updated_at=?{pw_clause} WHERE id=?",
                        params,
                    )
                    if cur.rowcount == 0:
                        return False, "not-found"
                    return True, "ok"
                except sqlite3.IntegrityError:
                    return False, "email-exists"
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def change_password(self, aid: int, new_password: str) -> bool:
        if len(new_password) < 4:
            return False
        salt_hex = secrets.token_hex(16)
        pw_hash = hash_password(new_password, salt_hex)
        now = time.time()

        def _do():
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE admin_users SET password_hash=?, salt=?, updated_at=? WHERE id=?",
                    (pw_hash, salt_hex, now, aid),
                )
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def delete(self, aid: int) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM admin_users WHERE id = ?", (aid,))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def record_login(self, aid: int, ip: str) -> None:
        def _do():
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE admin_users SET last_login_at=?, last_login_ip=? WHERE id=?",
                    (time.time(), ip[:64], aid),
                )
            finally:
                conn.close()
        await asyncio.to_thread(_do)


# ---- Management API tokens ----


class ApiTokenStore(_BaseStore):
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create(self, name: str, description: str = "", expires_at=None):
        token = secrets.token_urlsafe(32)
        now = time.time()

        def _do():
            conn = self._conn()
            try:
                cur = conn.execute(
                    """INSERT INTO api_tokens
                       (name, description, token_hash, token_prefix, enabled,
                        expires_at, created_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (name.strip(), description.strip(), self._hash(token),
                     token[:8], expires_at, now),
                )
                return cur.lastrowid
            finally:
                conn.close()

        row_id = await asyncio.to_thread(_do)
        return token, await self.get(row_id)

    async def get(self, token_id: int):
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    """SELECT id, name, description, token_prefix, enabled,
                              expires_at, created_at, last_used_at
                       FROM api_tokens WHERE id=?""", (token_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def list_all(self):
        def _do():
            conn = self._conn()
            try:
                return [dict(row) for row in conn.execute(
                    """SELECT id, name, description, token_prefix, enabled,
                              expires_at, created_at, last_used_at
                       FROM api_tokens ORDER BY id"""
                ).fetchall()]
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def authenticate(self, token: str):
        token_hash = self._hash(token)
        now = time.time()

        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    """SELECT id, name, description, enabled, expires_at
                       FROM api_tokens WHERE token_hash=?""", (token_hash,)
                ).fetchone()
                if not row or not row["enabled"] or (row["expires_at"] and row["expires_at"] < now):
                    return None
                conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, row["id"]))
                return dict(row)
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def set_enabled(self, token_id: int, enabled: bool) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("UPDATE api_tokens SET enabled=? WHERE id=?", (1 if enabled else 0, token_id))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def delete(self, token_id: int) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)


# ---- Recordings ----


class RecordingStore(_BaseStore):
    async def create(self, viewer_id: int, channel_id: int, title: str,
                     start_at: float, end_at: float, timezone: str):
        now = time.time()
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute(
                    """INSERT INTO recordings
                       (viewer_id, channel_id, title, start_at, end_at,
                        timezone, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)""",
                    (viewer_id, channel_id, title[:200], start_at, end_at,
                     timezone[:64], now, now),
                )
                return cur.lastrowid
            finally:
                conn.close()
        return await self.get(await asyncio.to_thread(_do))

    async def get(self, recording_id: int):
        def _do():
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def list_all(self, viewer_id: Optional[int] = None):
        def _do():
            conn = self._conn()
            try:
                if viewer_id is None:
                    rows = conn.execute("SELECT * FROM recordings ORDER BY start_at DESC").fetchall()
                else:
                    rows = conn.execute("SELECT * FROM recordings WHERE viewer_id=? ORDER BY start_at DESC", (viewer_id,)).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def due(self, now: float):
        def _do():
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM recordings WHERE status='scheduled' AND start_at<=? ORDER BY start_at",
                    (now,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def update(self, recording_id: int, **fields) -> bool:
        allowed = {"status", "local_path", "remote_path", "error", "title", "start_at", "end_at", "timezone", "actual_duration"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        fields["updated_at"] = time.time()
        def _do():
            conn = self._conn()
            try:
                sql = ", ".join(f"{key}=?" for key in fields)
                cur = conn.execute(f"UPDATE recordings SET {sql} WHERE id=?", (*fields.values(), recording_id))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def delete(self, recording_id: int) -> bool:
        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
                return cur.rowcount > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_do)


# ---- Audit log ----


class AuditStore(_BaseStore):
    async def add(
        self,
        action: str,
        actor_id: Optional[int],
        actor_email: str,
        target_type: str,
        target_id: Optional[int],
        details: str,
        ip: str,
    ) -> None:
        def _do():
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO audit_log
                       (ts, actor_id, actor_email, action,
                        target_type, target_id, details, ip)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), actor_id, actor_email, action,
                     target_type, target_id, details[:500], ip[:64]),
                )
            finally:
                conn.close()
        await asyncio.to_thread(_do)

    async def list_recent(
        self,
        limit: int = 200,
        action_filter: Optional[str] = None,
        actor_filter: Optional[str] = None,
    ) -> list[dict]:
        def _do():
            conn = self._conn()
            try:
                sql = "SELECT * FROM audit_log"
                params: list = []
                where = []
                if action_filter:
                    where.append("action LIKE ?")
                    params.append(action_filter + "%")
                if actor_filter:
                    where.append("actor_email LIKE ?")
                    params.append("%" + actor_filter + "%")
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY ts DESC LIMIT ?"
                params.append(int(limit))
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def prune_older_than(self, days: int) -> int:
        cutoff = time.time() - days * 86400.0

        def _do():
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
                return cur.rowcount
            finally:
                conn.close()
        return await asyncio.to_thread(_do)


# ---- Settings ----


class SettingsStore(_BaseStore):
    DEFAULTS = {
        "public_url": "https://tv.berrie.uk",
        "default_max_connections": "1",
        "default_expires_days": "0",   # 0 = no expiry
        "session_timeout_seconds": "0", # 0 = no timeout
        "log_retention_days": "30",
    }

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        def _do():
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    return row["value"]
                return self.DEFAULTS.get(key, default)
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def get_all(self) -> dict[str, str]:
        def _do():
            conn = self._conn()
            try:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
                out = dict(self.DEFAULTS)
                for r in rows:
                    out[r["key"]] = r["value"]
                return out
            finally:
                conn.close()
        return await asyncio.to_thread(_do)

    async def set(self, key: str, value: str) -> None:
        def _do():
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                      updated_at=excluded.updated_at""",
                    (key, value, time.time()),
                )
            finally:
                conn.close()
        await asyncio.to_thread(_do)

    async def set_many(self, items: dict[str, str]) -> None:
        for k, v in items.items():
            await self.set(k, v)
