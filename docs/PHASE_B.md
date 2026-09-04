# Phase B — Deferred features

These features were intentionally deferred from the initial TVProxy
build because they add significant complexity (extra deps, more auth
surface, persistent storage concerns on the SD card). They are listed
here as a future-work backlog.

## Status

| Feature | Why deferred |
|---|---|
| Geo-IP country lookups | Requires ~70 MB MaxMind GeoLite2 DB on the Pi; needs periodic refresh. Adds latency to every request unless cached. |
| 2FA TOTP for admin login | Adds enrollment/recovery flow, `pyotp` dep, and a second login step. Worth doing once admins are stable. |
| QR code on viewer detail page | One-line addition (`segno`), trivial when actually needed. |
| Statistics / time-series aggregations + charts | Per-user/per-day aggregates need either continuous sampling or periodic roll-ups. On SD card, lots of small writes are problematic. Better to add after we know what reports are useful. |
| Per-session bandwidth counter | Requires hooking into every byte of the streaming response. Cheap in RAM but extra code paths in the segment handler. |
| IP/country allow/deny lists per viewer | Needs geo-IP first. |
| Pi temperature in System page (`vcgencmd`) | Easy add; left for visual polish pass. |

## Migrations

The original `mappings` and `users` tables are still present in the
SQLite database but unused. Before the next release we should add a
one-shot migration script (`scripts/migrate_v1_to_v2.py`) that:

1. For each row in legacy `mappings`: create a default `connections`
   row pointing at the upstream origin (you supply the base URL and
   host), then create a `channels` row using the slug.
2. For each row in legacy `users`: create a corresponding `admin_users`
   row, prompting for a new password (the old bcrypt hash with a salt
   we don't have anymore cannot be migrated directly — they must reset).
3. Drop the legacy tables after successful validation.

## Test gaps

The current test suite (80 unit tests) covers pure functions and store
CRUD. We still need:

- Integration tests via `aiohttp.test_utils.TestClient` covering:
  - Login flow (cookie + CSRF + redirect)
  - Viewer creation, token, allowed-channel enforcement
  - Channel test-stream button (with a mock upstream)
  - Session terminate and the segment-after-terminate flow
- A test that the new schema is compatible with a `mappings.db`
  that has only legacy tables (no migration yet, but at least
  a clear error message instead of a crash).

## Configurable hardening

The systemd unit (`/etc/systemd/system/tv-proxy.service`) currently has
`MemoryDenyWriteExecute=true` which is fine for Python on CPython 3.10+
but would prevent any future native dep (e.g. `cryptography` for TOTP).
If we add 2FA in Phase B, re-evaluate this hardening setting.