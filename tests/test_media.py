"""Tests for app.media (playlist + file server + merged /tv/<code>)."""

import asyncio
import ipaddress
import json as _json
import sqlite3 as _sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.media import (
    Config,
    Source,
    build_playlist,
    create_app,
    parse_networks,
    parse_sources,
    recording_title,
    scan,
)

TOKEN = "s3cret-token"


def _db(tmp_path, twan_channels=(1, 3), recordings=()):
    """tv-proxy-like DB. recordings: (id, viewer_id, title, start_at, status, local_path)."""
    path = tmp_path / "db.sqlite"
    conn = _sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE viewers (id INTEGER PRIMARY KEY, name TEXT, token TEXT, short_code TEXT,
        short_enabled INTEGER, allowed_channels TEXT, disabled INTEGER, expires_at REAL);
      CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT,
        description TEXT, logo_url TEXT, enabled INTEGER);
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE recordings (id INTEGER PRIMARY KEY, viewer_id INTEGER, title TEXT,
        start_at REAL, status TEXT, local_path TEXT NOT NULL DEFAULT '', end_at REAL);
    """)
    conn.executemany("INSERT INTO viewers VALUES (?,?,?,?,?,?,?,?)", [
        (3, "twan", "tok-twan", "ab12", 1, _json.dumps(list(twan_channels)), 0, None),
        (2, "Herrie", "tok-h", "cd34", 1, "[1]", 0, None),
        (4, "off", "tok-o", "ef56", 1, "[1]", 1, None),
        (5, "old", "tok-x", "gh78", 1, "[1]", 0, 1.0),
        (6, "all", "tok-a", "ij90", 1, "[]", 0, None),
    ])
    conn.executemany("INSERT INTO channels VALUES (?,?,?,?,?,?)", [
        (1, "npo-1", "NL: NPO 1", "Netherland - Algemeen", "https://logo/npo1.png", 1),
        (2, "rtl-4", "NL: RTL 4", "Netherland - Algemeen", "", 1),
        (3, "espn", "NL: ESPN", "Netherland - Sport", "", 1),
    ])
    conn.executemany("INSERT INTO recordings (id, viewer_id, title, start_at, status, local_path)"
                     " VALUES (?,?,?,?,?,?)", list(recordings))
    conn.execute("INSERT INTO settings VALUES ('public_url', 'https://tv.example')")
    conn.commit()
    conn.close()
    return str(path)


def _config(tmp_path, networks="127.0.0.0/8,::1/128", min_bytes=10, recordings=()):
    rec = tmp_path / "rec"
    vid = tmp_path / "vid"
    rec.mkdir(exist_ok=True)
    vid.mkdir(exist_ok=True)
    return Config(
        token=TOKEN,
        sources=(Source("recordings", "Opnames", rec), Source("videos", "Video's", vid)),
        networks=parse_networks(networks),
        min_bytes=min_bytes,
        db_path=_db(tmp_path, recordings=recordings),
    )


def _write(path, size=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * (size // 256) + bytes(size % 256))


class TestRecordingTitle:
    def test_dot_part(self):
        title, part = recording_title(
            "20260910-164000-UEFA-Champions-League-PSV-Eindhoven---Sjachtar-Donetsk-68.part3")
        assert title == "2026-09-10 18:40 UEFA Champions League PSV Eindhoven - Sjachtar Donetsk"   # UTC -> CEST
        assert part == 3

    def test_dash_part(self):
        title, part = recording_title(
            "20260906-071737-F1-race---two-hour-diagnostic-recording-65-part02")
        assert title == "2026-09-06 09:17 F1 race - two hour diagnostic recording"
        assert part == 2

    def test_unknown_format(self):
        assert recording_title("my_holiday-video") == ("my holiday video", None)


class TestParse:
    def test_sources(self):
        (a, b) = parse_sources("recordings:Opnames:/a,videos:Video's:/b/c")
        assert (a.key, a.label, str(a.root)) == ("recordings", "Opnames", "/a")
        assert (b.key, b.label, str(b.root)) == ("videos", "Video's", "/b/c")

    def test_bad_key(self):
        with pytest.raises(ValueError):
            parse_sources("bad key:x:/a")

    def test_duplicate_key(self):
        with pytest.raises(ValueError):
            parse_sources("a:x:/a,a:y:/b")

    def test_networks(self):
        nets = parse_networks("192.168.0.0/16, 100.64.0.0/10")
        assert ipaddress.ip_address("100.92.27.95") in nets[1]


class TestScan:
    def test_only_existing_completed_recordings(self, tmp_path):
        rec = tmp_path / "rec"
        psv = rec / "20261031-183600-PSV---Willem-II-97.ts"
        older = rec / "20261001-200000-Older-90.ts"
        rows = [
            (97, 3, "PSV - Willem II", 2.0, "completed", str(psv)),
            (90, 3, "Older", 1.0, "completed", str(older)),
            (91, 3, "Failed one", 1.5, "failed", str(rec / "20261002-200000-Failed-91.ts")),
            (92, 3, "File gone", 1.6, "completed", str(rec / "20261003-200000-Gone-92.ts")),
            (93, 2, "Herrie's", 1.7, "completed", str(rec / "20261004-200000-Herrie-93.ts")),
            (94, 3, "Outside", 1.8, "completed", str(tmp_path / "elsewhere.ts")),
        ]
        cfg = _config(tmp_path, recordings=rows)
        for f in (psv, older, rec / "20261002-200000-Failed-91.ts",
                  rec / "20261004-200000-Herrie-93.ts", tmp_path / "elsewhere.ts"):
            _write(f)
        # leftovers of deleted recordings: no DB row -> never listed
        for n in range(3):
            _write(rec / f"20260910-182500-Formule-1-GP-Race-67.part{n}.ts")

        assert [e.title for e in scan(cfg, viewer_id=3)] == [
            "2026-10-31 19:36 PSV - Willem II", "2026-10-01 22:00 Older"]
        assert [e.rel for e in scan(cfg, viewer_id=3)][0] == psv.name
        assert len(scan(cfg)) == 3                          # all viewers: + Herrie's
        assert scan(cfg, viewer_id=2)[0].title == "2026-10-04 22:00 Herrie's"

    def test_recording_deleted_disappears(self, tmp_path):
        psv = tmp_path / "rec" / "20261031-183600-PSV---Willem-II-97.ts"
        cfg = _config(tmp_path, recordings=[(97, 3, "PSV", 2.0, "completed", str(psv))])
        _write(psv)
        assert len(scan(cfg, 3)) == 1
        conn = _sqlite3.connect(cfg.db_path)
        conn.execute("DELETE FROM recordings WHERE id=97")
        conn.commit()
        conn.close()
        assert scan(cfg, 3) == []

    def test_videos_folder(self, tmp_path):
        cfg = _config(tmp_path)
        _write(cfg.sources[1].root / "Films" / "Movie_One.avi")
        _write(cfg.sources[1].root / "clip.mp4")
        _write(cfg.sources[1].root / ".hidden.mp4")
        _write(cfg.sources[1].root / "notes.txt")
        _write(cfg.sources[1].root / "tiny.mp4", size=1)
        entries = scan(cfg)
        assert [e.title for e in entries] == ["clip", "Movie One"]
        assert entries[0].group == "Video's"
        assert entries[1].group == "Video's / Films"

    def test_symlink_outside_root_hidden(self, tmp_path):
        cfg = _config(tmp_path)
        outside = tmp_path / "secret.mp4"
        _write(outside)
        (cfg.sources[1].root / "link.mp4").symlink_to(outside)
        assert scan(cfg) == []

    def test_missing_root(self, tmp_path):
        cfg = Config(token=TOKEN, sources=(Source("videos", "V", tmp_path / "nope"),),
                     networks=parse_networks("127.0.0.0/8"), db_path=_db(tmp_path))
        assert scan(cfg) == []


async def test_public_url_used_for_links(tmp_path):
    cfg = _config(tmp_path)
    cfg = Config(**{**cfg.__dict__, "public_url": "https://tv.example/media"})
    _write(cfg.sources[1].root / "clip.mp4")
    client = TestClient(TestServer(create_app(cfg)))
    await client.start_server()
    try:
        text = await (await client.get(f"/{TOKEN}/playlist.m3u")).text()
    finally:
        await client.close()
    assert f"https://tv.example/media/{TOKEN}/files/videos/clip.mp4" in text


def test_build_playlist_escapes(tmp_path):
    cfg = _config(tmp_path)
    _write(cfg.sources[1].root / 'a "b", c #1.mp4')
    text = build_playlist(scan(cfg), "http://pi:8090", TOKEN)
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:-1 tvg-name=\"a 'b', c #1\" group-title=\"Video's\",a \"b\"  c #1"
    assert lines[2] == "http://pi:8090/s3cret-token/files/videos/a%20%22b%22%2C%20c%20%231.mp4"


@pytest.fixture
async def client_factory():
    clients = []

    async def make(cfg):
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for c in clients:
        await c.close()


class TestHttp:
    async def test_health_no_token(self, tmp_path, client_factory):
        client = await client_factory(_config(tmp_path))
        resp = await client.get("/health")
        assert resp.status == 200

    async def test_playlist_requires_token(self, tmp_path, client_factory):
        client = await client_factory(_config(tmp_path))
        assert (await client.get("/wrong/playlist.m3u")).status == 403
        resp = await client.get(f"/{TOKEN}/playlist.m3u")
        assert resp.status == 200
        assert "no-store" in resp.headers["Cache-Control"]
        assert "no-cache" in resp.headers["Cache-Control"]
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"
        assert "ETag" not in resp.headers and "Last-Modified" not in resp.headers
        assert (await resp.text()).startswith("#EXTM3U")

    async def test_playlist_is_fresh(self, tmp_path, client_factory):
        cfg = _config(tmp_path)
        client = await client_factory(cfg)
        assert "clip" not in await (await client.get(f"/{TOKEN}/playlist.m3u")).text()
        _write(cfg.sources[1].root / "clip.mp4")
        assert "clip" in await (await client.get(f"/{TOKEN}/playlist.m3u")).text()
        (cfg.sources[1].root / "clip.mp4").unlink()
        assert "clip" not in await (await client.get(f"/{TOKEN}/playlist.m3u8")).text()

    async def test_file_range_request(self, tmp_path, client_factory):
        cfg = _config(tmp_path)
        _write(cfg.sources[1].root / "clip.mp4", size=1000)
        client = await client_factory(cfg)
        resp = await client.get(f"/{TOKEN}/files/videos/clip.mp4",
                                headers={"Range": "bytes=100-199"})
        assert resp.status == 206
        assert resp.headers["Content-Type"] == "video/mp4"
        assert resp.headers["Cache-Control"] == "private, no-transform"
        assert len(await resp.read()) == 100

    async def test_open_ended_range_on_huge_file(self, tmp_path, client_factory):
        # Regression: TV players ask for "bytes=N-" (rest of the file). On the
        # 32-bit Pi, sendfile() raised OverflowError when that rest was > 2 GiB
        # and the connection dropped with 0 bytes. The file must be streamed in
        # chunks instead; the client reads a little and then hangs up.
        cfg = _config(tmp_path)
        big = cfg.sources[1].root / "big.mp4"
        with open(big, "wb") as f:              # sparse 3 GiB file, no real disk use
            f.write(b"HEAD")
            f.seek(2_600_000_000)
            f.write(b"MARKER")
            f.truncate(3 * 1024 ** 3)
        client = await client_factory(cfg)
        for rng, first in (("bytes=0-", b"HEAD"), ("bytes=2600000000-", b"MARKER")):
            resp = await client.get(f"/{TOKEN}/files/videos/big.mp4", headers={"Range": rng})
            assert resp.status == 206, rng
            chunk = await resp.content.readexactly(64 * 1024)
            assert chunk.startswith(first), rng
            resp.close()
        resp = await client.get(f"/{TOKEN}/files/videos/big.mp4",
                                headers={"Range": "bytes=-188"})   # last TS packet
        assert resp.status == 206
        assert len(await resp.read()) == 188

    async def test_file_requires_token(self, tmp_path, client_factory):
        cfg = _config(tmp_path)
        _write(cfg.sources[1].root / "clip.mp4")
        client = await client_factory(cfg)
        assert (await client.get("/nope/files/videos/clip.mp4")).status == 403

    async def test_traversal_and_non_video_rejected(self, tmp_path, client_factory):
        cfg = _config(tmp_path)
        _write(tmp_path / "secret.mp4")
        _write(cfg.sources[1].root / "notes.txt")
        client = await client_factory(cfg)
        for path in ("videos/..%2Fsecret.mp4", "videos/%2E%2E/secret.mp4",
                     "videos/notes.txt", "nosuch/clip.mp4"):
            # 404 from the handler, or 403 when the client normalised the
            # path so it no longer matches the files route at all.
            assert (await client.get(f"/{TOKEN}/files/{path}")).status in (403, 404), path

    async def test_disallowed_network(self, tmp_path, client_factory):
        client = await client_factory(_config(tmp_path, networks="10.0.0.0/8"))
        assert (await client.get("/health")).status == 403


# ---- merged /tv/<code> playlist ------------------------------------------

from aiohttp import web as _web  # noqa: E402

from app.media import (ChunkBuffer, active_streams, add_ts_copies, build_live_playlist, forced_busy,  # noqa: E402
                       merge_playlists, recording_has_priority, use_ts_urls)
from app.streaming import generate_user_m3u  # noqa: E402

UPSTREAM_BODY = '#EXTM3U\n#EXTINF:-1 group-title="X",From upstream\nhttps://up/x.m3u8\n'


def test_merge_playlists():
    live = '#EXTM3U\n#EXTINF:-1,Live\nhttp://x/live.m3u8\n'
    media = '#EXTM3U\n#EXTINF:-1 group-title="Opnames",Rec\nhttp://x/rec.ts\n'
    out = merge_playlists(live, media)
    assert out == live + '#EXTINF:-1 group-title="Opnames",Rec\nhttp://x/rec.ts\n'
    assert merge_playlists(live, "#EXTM3U\n") == live
    assert merge_playlists("", media).startswith("#EXTM3U\n#EXTINF")


class TestBuildLive:
    def test_matches_tv_proxy_generator(self, tmp_path):
        db = _db(tmp_path)
        out, viewer_id = build_live_playlist(db, "ab12", frozenset({"twan"}))
        assert viewer_id == 3
        channels = [
            {"id": 1, "slug": "npo-1", "display_name": "NL: NPO 1", "description": "Netherland - Algemeen",
             "logo_url": "https://logo/npo1.png", "enabled": 1},
            {"id": 3, "slug": "espn", "display_name": "NL: ESPN", "description": "Netherland - Sport",
             "logo_url": "", "enabled": 1},
        ]
        assert out == generate_user_m3u("https://tv.example", channels, "tok-twan", "ab12", [])
        assert "https://tv.example/tv/ab12/npo-1.m3u8" in out
        assert "rtl-4" not in out

    def test_by_id(self, tmp_path):
        assert build_live_playlist(_db(tmp_path), "ab12", frozenset({"3"})) is not None

    def test_not_a_merge_viewer(self, tmp_path):
        db = _db(tmp_path)
        assert build_live_playlist(db, "cd34", frozenset({"twan"})) is None
        assert build_live_playlist(db, "zzzz", frozenset({"twan"})) is None
        assert build_live_playlist(db, "ab12", frozenset()) is None

    def test_disabled_and_expired_get_empty(self, tmp_path):
        db = _db(tmp_path)
        assert build_live_playlist(db, "ef56", frozenset({"off"})) == ("#EXTM3U\n", None)
        assert build_live_playlist(db, "gh78", frozenset({"old"})) == ("#EXTM3U\n", None)

    def test_all_channels_viewer_left_to_tv_proxy(self, tmp_path):
        assert build_live_playlist(_db(tmp_path), "ij90", frozenset({"all"})) is None


@pytest.fixture
async def upstream():
    seen = {}

    async def stream(request):
        seen["stream"] = request.path
        if request.match_info["slug"] in ("gone", "busy"):
            return _web.json_response({"error": "upstream-non-200"}, status=502)
        if request.match_info["slug"] == "offline":
            return _web.json_response({"error": "upstream-fetch-fail"}, status=502)
        resp = _web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        await resp.prepare(request)
        if request.match_info["slug"] == "trickle":          # 10 chunks, 0.1 s apart
            for i in range(10):
                await resp.write(bytes([65 + i]) * 1000)
                await asyncio.sleep(0.1)
            return resp
        if request.match_info["slug"] == "endless":
            seen["endless_closed"] = False
            try:
                while True:
                    await resp.write(b"G" * 188 * 100)
                    await asyncio.sleep(0.01)
            except (ConnectionError, asyncio.CancelledError):
                seen["endless_closed"] = True
                raise
        for _ in range(3):
            await resp.write(b"G" * 188)
        return resp

    async def tv(request):
        seen["headers"] = dict(request.headers)
        if request.match_info["code"] == "boom":
            return _web.Response(status=500, text="err")
        return _web.Response(text=UPSTREAM_BODY, content_type="audio/x-mpegurl")

    app = _web.Application()
    app.router.add_get("/tv/{code}", tv)
    app.router.add_get("/tv/{code}/{slug}.m3u8", stream)
    server = TestServer(app)
    await server.start_server()
    server.seen = seen
    yield server
    await server.close()


def _merge_config(tmp_path, up, **kw):
    rec = tmp_path / "rec"
    psv = rec / "20261031-183600-PSV---Willem-II-97.ts"
    cfg = _config(tmp_path, recordings=[
        (97, 3, "PSV - Willem II", 2.0, "completed", str(psv)),
        (93, 2, "Herrie only", 1.0, "completed", str(rec / "20261004-200000-Herrie-93.ts")),
    ])
    _write(psv)
    _write(rec / "20261004-200000-Herrie-93.ts")
    _write(rec / "20260910-182500-Deleted-67.part0.ts")       # orphan
    return Config(**{**cfg.__dict__, "public_url": "https://tv.example/media",
                     "merge_viewers": frozenset({"twan", "off"}),
                     "upstream": str(up.make_url("")).rstrip("/"), **kw})


class TestMerged:
    async def test_merged_for_allowed_viewer(self, tmp_path, upstream, client_factory):
        client = await client_factory(_merge_config(tmp_path, upstream))
        resp = await client.get("/tv/ab12")
        text = await resp.text()
        assert resp.status == 200
        assert text.startswith("#EXTM3U\n#EXTINF:-1 tvg-logo=")
        assert "https://tv.example/tv/ab12/espn.m3u8" in text
        assert "2026-10-31 19:36 PSV - Willem II" in text
        assert f"https://tv.example/media/{TOKEN}/files/recordings/" in text
        assert "Herrie only" not in text and "Deleted" not in text
        assert text.count('group-title="Opnames"') == 1
        assert "no-store" in resp.headers["Cache-Control"]
        assert "headers" not in upstream.seen          # tv-proxy not called

    async def test_disabled_merge_viewer_gets_nothing(self, tmp_path, upstream, client_factory):
        client = await client_factory(_merge_config(tmp_path, upstream))
        assert await (await client.get("/tv/ef56")).text() == "#EXTM3U\n"

    async def test_other_viewer_proxied_unchanged(self, tmp_path, upstream, client_factory):
        client = await client_factory(_merge_config(tmp_path, upstream))
        resp = await client.get("/tv/cd34", headers={"X-Real-IP": "1.2.3.4"})
        assert await resp.text() == UPSTREAM_BODY
        assert upstream.seen["headers"]["X-Real-IP"] == "1.2.3.4"

    async def test_upstream_error_passed_through(self, tmp_path, upstream, client_factory):
        client = await client_factory(_merge_config(tmp_path, upstream))
        assert (await client.get("/tv/boom")).status == 500

    async def test_broken_db_falls_back_to_tv_proxy(self, tmp_path, upstream, client_factory):
        cfg = _merge_config(tmp_path, upstream, db_path=str(tmp_path / "missing" / "x.db"))
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12")
        assert resp.status == 200
        assert await resp.text() == UPSTREAM_BODY

    async def test_upstream_down(self, tmp_path, upstream, client_factory):
        cfg = _merge_config(tmp_path, upstream, upstream="http://127.0.0.1:1")
        client = await client_factory(cfg)
        assert (await client.get("/tv/cd34")).status == 502


def test_add_ts_copies():
    live = ('#EXTM3U\n#EXTINF:-1 tvg-id="npo-1" group-title="Netherland - Algemeen",NL: NPO 1\n'
            'https://tv.example/tv/ab12/npo-1.m3u8\n'
            '#EXTINF:-1 tvg-id="espn" group-title="Netherland - Sport",NL: ESPN\n'
            'https://tv.example/tv/ab12/espn.m3u8\n')
    out = add_ts_copies(live, ("npo-1",))
    assert out.startswith(live)
    assert out.endswith('#EXTINF:-1 tvg-id="npo-1" group-title="Test .ts",NL: NPO 1 (.ts)\n'
                        'https://tv.example/tv/ab12/npo-1.ts\n')
    assert add_ts_copies(live, ()) == live


class TestTsRelay:
    async def test_relays_stream(self, tmp_path, upstream, client_factory):
        client = await client_factory(_merge_config(tmp_path, upstream))
        resp = await client.get("/tv/ab12/npo-1.ts")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "video/mp2t"
        assert await resp.read() == b"G" * 188 * 3
        assert upstream.seen["stream"] == "/tv/ab12/npo-1.m3u8"

    async def test_upstream_error_passed_through(self, tmp_path, upstream, client_factory):
        client = await client_factory(_merge_config(tmp_path, upstream))
        assert (await client.get("/tv/ab12/gone.ts")).status == 502

    async def test_merged_playlist_has_test_group(self, tmp_path, upstream, client_factory):
        cfg = _merge_config(tmp_path, upstream, ts_test=("espn",))
        client = await client_factory(cfg)
        text = await (await client.get("/tv/ab12")).text()
        assert 'group-title="Test .ts",NL: ESPN (.ts)\nhttps://tv.example/tv/ab12/espn.ts' in text


def test_use_ts_urls():
    live = ('#EXTM3U\n#EXTINF:-1 tvg-id="npo-1",NL: NPO 1\nhttps://tv.example/tv/ab12/npo-1.m3u8\n'
            '#EXTINF:-1,Other\nhttps://other.example/live/x.m3u8\n')
    out = use_ts_urls(live)
    assert "https://tv.example/tv/ab12/npo-1.ts\n" in out
    assert "https://other.example/live/x.m3u8" in out          # not a /tv/ URL: untouched
    assert out.count("#EXTINF") == 2


async def test_merged_with_ts_urls(tmp_path, upstream, client_factory):
    cfg = _merge_config(tmp_path, upstream, ts_urls=True, ts_test=("espn",))
    client = await client_factory(cfg)
    text = await (await client.get("/tv/ab12")).text()
    assert "https://tv.example/tv/ab12/npo-1.ts" in text
    assert "https://tv.example/tv/ab12/espn.ts" in text
    assert ".m3u8" not in text
    assert "Test .ts" not in text                                # test group replaced
    assert 'group-title="Opnames"' in text


async def test_ts_relay_passes_hls_playlist_through(tmp_path, client_factory):
    async def hls(request):
        return _web.Response(text="#EXTM3U\n#EXT-X-VERSION:3\n", content_type="application/vnd.apple.mpegurl")
    app = _web.Application()
    app.router.add_get("/tv/{code}/{slug}.m3u8", hls)
    server = TestServer(app)
    await server.start_server()
    try:
        cfg = _merge_config(tmp_path, server)
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/x.ts")
        assert resp.status == 200
        assert "mpegurl" in resp.headers["Content-Type"]
        assert (await resp.text()).startswith("#EXTM3U")
    finally:
        await server.close()


async def test_ts_urls_kept_when_media_scan_fails(tmp_path, upstream, client_factory, monkeypatch):
    import app.media as m
    monkeypatch.setattr(m, "scan", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    cfg = _merge_config(tmp_path, upstream, ts_urls=True)
    client = await client_factory(cfg)
    text = await (await client.get("/tv/ab12")).text()
    assert "https://tv.example/tv/ab12/npo-1.ts" in text and ".m3u8" not in text



def _add_recording(db, rid, status, start_at, end_at, title="Match"):
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO recordings (id, viewer_id, title, start_at, end_at, status, local_path)"
                 " VALUES (?,?,?,?,?,?,'')", (rid, 3, title, start_at, end_at, status))
    conn.commit()
    conn.close()


class TestRecordingPriority:
    def test_priority_window(self, tmp_path):
        db = _db(tmp_path)
        now = 1_000_000.0
        assert recording_has_priority(db, 60, now) is None
        _add_recording(db, 1, "scheduled", now + 120, now + 3600)     # later: no priority yet
        assert recording_has_priority(db, 60, now) is None
        assert recording_has_priority(db, 60, now + 61)["title"] == "Match"   # within lead
        _add_recording(db, 2, "completed", now - 10, now + 10, "Old")
        _add_recording(db, 3, "scheduled", now - 7200, now - 3600, "Missed")   # already over
        assert recording_has_priority(db, 60, now) is None
        _add_recording(db, 4, "recording", now - 60, now + 60, "Now")
        assert recording_has_priority(db, 60, now)["title"] == "Now"

    async def test_busy_clip_instead_of_channel(self, tmp_path, upstream, client_factory,
                                                monkeypatch):
        import time as _t
        import app.media as m
        monkeypatch.setattr(m, "BUSY_CLIP_SECONDS", 1)     # whole clip may go at once
        clip = tmp_path / "busy.ts"
        clip.write_bytes(b"G" * 188 * 10)
        cfg = _merge_config(tmp_path, upstream, busy_clip=str(clip))
        _add_recording(cfg.db_path, 52, "recording", _t.time() - 10, _t.time() + 600, "PSV")
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/npo-1.ts")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "video/mp2t"
        assert await asyncio.wait_for(resp.read(), 5) == b"G" * 188 * 10
        assert "stream" not in upstream.seen          # provider slot left free

    async def test_clip_is_paced_and_ends_when_recording_ends(self, tmp_path, upstream,
                                                              client_factory, monkeypatch):
        import time as _t
        import app.media as m
        monkeypatch.setattr(m, "PRIORITY_CHECK_SECONDS", 0.2)
        monkeypatch.setattr(m, "BUSY_PREBUFFER", 1.0)
        clip = tmp_path / "busy.ts"
        clip.write_bytes(b"G" * 600_000)                  # 1000 B/s at 600 s
        flag = tmp_path / "force-busy"
        flag.write_text(str(_t.time() + 600))
        cfg = _merge_config(tmp_path, upstream, busy_clip=str(clip), force_busy=str(flag))
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/npo-1.ts")
        first = await resp.content.read(100_000)
        assert 0 < len(first) < 5_000                     # paced, not the whole file
        flag.write_text("0")                              # recording finished
        rest = await asyncio.wait_for(resp.read(), 3)     # stream ends promptly
        assert len(first) + len(rest) < 10_000

    async def test_new_tv_stream_refused_during_recording(self, tmp_path, upstream, client_factory):
        import time as _t
        cfg = _merge_config(tmp_path, upstream, busy_clip=str(tmp_path / "missing.ts"))
        _add_recording(cfg.db_path, 50, "recording", _t.time() - 10, _t.time() + 600, "PSV")
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/npo-1.ts")
        assert resp.status == 503
        assert "PSV" in await resp.text()
        assert "stream" not in upstream.seen          # never reached tv-proxy/provider

    async def test_running_tv_stream_stopped_when_recording_due(self, tmp_path, upstream,
                                                                 client_factory, monkeypatch):
        import time as _t
        import app.media as m
        monkeypatch.setattr(m, "PRIORITY_CHECK_SECONDS", 0.2)
        cfg = _merge_config(tmp_path, upstream)
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/endless.ts")
        assert resp.status == 200
        await resp.content.readexactly(188 * 100)     # TV is watching
        _add_recording(cfg.db_path, 51, "scheduled", _t.time() + 30, _t.time() + 600)  # due in 30 s
        data = await asyncio.wait_for(resp.read(), timeout=5)   # stream ends by itself
        assert len(data) >= 0
        await asyncio.sleep(0.1)
        assert upstream.seen["endless_closed"] is True          # upstream released too


def test_forced_busy(tmp_path):
    f = tmp_path / "force-busy"
    assert forced_busy(str(f), 100.0) is None                 # no file
    f.write_text("130")
    assert forced_busy(str(f), 100.0)["title"] == "test"      # until 130
    assert forced_busy(str(f), 131.0) is None                 # expired
    f.write_text("garbage")
    assert forced_busy(str(f), 100.0) is None


async def test_force_busy_serves_clip(tmp_path, upstream, client_factory, monkeypatch):
    import time as _t
    import app.media as m
    monkeypatch.setattr(m, "BUSY_CLIP_SECONDS", 1)
    clip = tmp_path / "busy.ts"
    clip.write_bytes(b"G" * 188)
    flag = tmp_path / "force-busy"
    flag.write_text(str(_t.time() + 30))
    cfg = _merge_config(tmp_path, upstream, busy_clip=str(clip), force_busy=str(flag))
    client = await client_factory(cfg)
    resp = await client.get("/tv/ab12/npo-1.ts")
    assert await resp.content.readexactly(188) == b"G" * 188
    resp.close()
    flag.write_text("0")
    resp = await client.get("/tv/ab12/npo-1.ts")
    assert await resp.read() == b"G" * 188 * 3               # normal stream again



def test_active_streams(tmp_path):
    tcp = tmp_path / "tcp"
    tcp.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue\n"
        "   0: 0100007F:1F91 0100007F:D1A2 01 00000000:00000000\n"     # :8081 established
        "   1: 00000000:1F91 00000000:0000 0A 00000000:00000000\n"     # :8081 listening
        "   2: 0100007F:1F90 0100007F:D1A3 01 00000000:00000000\n"     # :8080
        "   3: 0100007F:D1A4 0100007F:1F91 01 00000000:00000000\n")    # client side of :8081
    assert active_streams(8081, (str(tcp), str(tmp_path / "missing"))) == 1
    assert active_streams(8080, (str(tcp),)) == 1


class TestInUse:
    async def test_inuse_clip_while_other_device_watches(self, tmp_path, upstream,
                                                         client_factory, monkeypatch):
        import app.media as m
        monkeypatch.setattr(m, "BUSY_CLIP_SECONDS", 1)
        monkeypatch.setattr(m, "PRIORITY_CHECK_SECONDS", 0.2)
        watching = {"n": 1}
        monkeypatch.setattr(m, "active_streams", lambda port, *a: watching["n"])
        clip = tmp_path / "inuse.ts"
        clip.write_bytes(b"U" * 188 * 50)
        cfg = _merge_config(tmp_path, upstream, inuse_clip=str(clip))
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/busy.ts")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "video/mp2t"
        assert (await resp.content.readexactly(188)) == b"U" * 188

    async def test_inuse_clip_ends_when_other_device_stops(self, tmp_path, upstream,
                                                           client_factory, monkeypatch):
        import app.media as m
        monkeypatch.setattr(m, "PRIORITY_CHECK_SECONDS", 0.2)
        monkeypatch.setattr(m, "BUSY_PREBUFFER", 1.0)
        watching = {"n": 1}
        monkeypatch.setattr(m, "active_streams", lambda port, *a: watching["n"])
        clip = tmp_path / "inuse.ts"
        clip.write_bytes(b"U" * 600_000)
        cfg = _merge_config(tmp_path, upstream, inuse_clip=str(clip))
        client = await client_factory(cfg)
        resp = await client.get("/tv/ab12/busy.ts")
        first = await resp.content.read(100_000)
        assert 0 < len(first) < 5_000
        watching["n"] = 0                                   # other device stopped
        rest = await asyncio.wait_for(resp.read(), 3)
        assert len(first) + len(rest) < 10_000

    async def test_refused_without_other_device_is_plain_error(self, tmp_path, upstream,
                                                               client_factory, monkeypatch):
        import app.media as m
        monkeypatch.setattr(m, "active_streams", lambda port, *a: 0)
        clip = tmp_path / "inuse.ts"
        clip.write_bytes(b"U" * 188)
        client = await client_factory(_merge_config(tmp_path, upstream, inuse_clip=str(clip)))
        assert (await client.get("/tv/ab12/busy.ts")).status == 502

    async def test_offline_channel_is_plain_error(self, tmp_path, upstream, client_factory,
                                                  monkeypatch):
        import app.media as m
        monkeypatch.setattr(m, "active_streams", lambda port, *a: 1)
        clip = tmp_path / "inuse.ts"
        clip.write_bytes(b"U" * 188)
        client = await client_factory(_merge_config(tmp_path, upstream, inuse_clip=str(clip)))
        assert (await client.get("/tv/ab12/offline.ts")).status == 502



async def test_chunk_buffer_order_and_eof():
    buf = ChunkBuffer(10_000)
    for i in range(5):
        await buf.put(bytes([i]) * 1000)      # does not block: well under the limit
    await buf.close()
    got = []
    while (c := await buf.get()) is not None:
        got.append(c[0])
    assert got == [0, 1, 2, 3, 4]


async def test_chunk_buffer_blocks_only_when_full():
    buf = ChunkBuffer(2_000)
    await buf.put(b"a" * 1000)
    await buf.put(b"b" * 1000)
    blocked = asyncio.create_task(buf.put(b"c" * 1000))
    await asyncio.sleep(0.05)
    assert not blocked.done()                  # full: provider reading pauses
    assert await buf.get() == b"a" * 1000
    await asyncio.wait_for(blocked, 1)         # space again: continues


class TestRelayBuffer:
    async def test_prebuffer_sends_first_seconds_as_a_burst(self, tmp_path, upstream,
                                                              client_factory):
        import time as _t
        cfg = _merge_config(tmp_path, upstream, relay_prebuffer=0.55)
        client = await client_factory(cfg)
        t0 = _t.monotonic()
        resp = await client.get("/tv/ab12/trickle.ts")
        first = await resp.content.readany()
        while len(first) < 4000:               # the burst may arrive in a few reads
            first += await asyncio.wait_for(resp.content.readany(), 0.05)
        assert _t.monotonic() - t0 >= 0.5      # waited for the start buffer
        assert first[:1000] == b"A" * 1000     # in order, nothing lost
        rest = await resp.read()
        assert len(first) + len(rest) == 10_000

    async def test_no_prebuffer_streams_immediately(self, tmp_path, upstream, client_factory):
        import time as _t
        cfg = _merge_config(tmp_path, upstream, relay_prebuffer=0)
        client = await client_factory(cfg)
        t0 = _t.monotonic()
        resp = await client.get("/tv/ab12/trickle.ts")
        await resp.content.readexactly(1000)
        assert _t.monotonic() - t0 < 0.3
        assert len(await resp.read()) == 9_000



async def test_clip_ends_promptly_for_a_tv_reading_at_playback_speed(tmp_path):
    """Regression (seen on the real TV, 2026-11-01): a TV reads the clip at playback
    speed; it must start streaming right away and end within ~2 s after the
    recording finished. Runs the real service in its own process and reads over a
    socket like a TV does (aiohttp's in-process test server did not reproduce the
    real timing)."""
    import os, socket, subprocess, sys, time as _t
    clip = tmp_path / "busy.ts"
    clip.write_bytes(b"G" * 2_000_000)                       # 10 s clip -> 200 kB/s
    flag = tmp_path / "force-busy"
    flag.write_text(str(_t.time() + 600))
    for d in ("rec", "vid"):
        (tmp_path / d).mkdir(exist_ok=True)
    db = _db(tmp_path)
    s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
    env = {**os.environ, "MEDIA_TOKEN": "x", "MEDIA_HOST": "127.0.0.1", "MEDIA_PORT": str(port),
           "MEDIA_SOURCES": f"recordings:R:{tmp_path/'rec'},videos:V:{tmp_path/'vid'}",
           "MEDIA_DB": db, "MEDIA_BUSY_CLIP": str(clip), "MEDIA_FORCE_BUSY": str(flag)}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import app.media as m; m.BUSY_CLIP_SECONDS=10; m.BUSY_PREBUFFER=0.5; "
            "m.PRIORITY_CHECK_SECONDS=0.2; m.main()")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close(); break
            except OSError:
                await asyncio.sleep(0.1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /tv/ab12/npo-1.ts HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        t0 = _t.monotonic()
        got, first_at, finished, ended_at = 0, None, None, None
        while _t.monotonic() - t0 < 6:
            try:
                chunk = await asyncio.wait_for(reader.read(20_000), 1.0)   # ~200 kB/s
            except asyncio.TimeoutError:
                continue
            if not chunk or chunk.endswith(b"0\r\n\r\n"):
                ended_at = _t.monotonic() - t0
                break
            got += len(chunk)
            if first_at is None:
                first_at = _t.monotonic() - t0
            await asyncio.sleep(0.1)
            if finished is None and _t.monotonic() - t0 > 1.5:
                flag.write_text("0")                            # recording finished
                finished = _t.monotonic() - t0
        writer.close()
    finally:
        proc.terminate(); proc.wait(5)
    assert first_at is not None and first_at < 1.0, "clip did not start streaming right away"
    assert ended_at is not None, "clip kept streaming after the recording ended"
    assert ended_at - finished < 2.0



# ---- home server copies ("Opnames (thuis)") ---------------------------------

def test_recording_copies_in_a_folder_get_dated_titles_newest_first(tmp_path):
    cfg = _config(tmp_path)
    home = Source("thuis", "Opnames (thuis)", tmp_path / "vid")
    cfg = Config(**{**cfg.__dict__, "sources": (home,)})
    _write(home.root / "20261101-180000-Old-1.ts")
    _write(home.root / "20261104-200000-UEFA-Champions-League-PSV---Club-Brugge-101.ts")
    _write(home.root / "holiday.mp4")
    entries = scan(cfg)
    assert [e.title for e in entries] == ["2026-11-04 21:00 UEFA Champions League PSV - Club Brugge",
                                          "2026-11-01 19:00 Old", "holiday"]
    assert {e.group for e in entries} == {"Opnames (thuis)"}


def test_lan_ip_is_an_ipv4_address():
    import app.media as m
    ip = m.lan_ip()
    assert ipaddress.ip_address(ip).version == 4


async def test_public_url_lan_uses_local_address(tmp_path, client_factory, monkeypatch):
    import app.media as m
    monkeypatch.setattr(m, "lan_ip", lambda: "192.168.1.35")
    cfg = Config(**{**_config(tmp_path).__dict__, "public_url": "lan", "port": 8090})
    _write(cfg.sources[1].root / "clip.mp4")
    client = await client_factory(cfg)
    text = await (await client.get(f"/{TOKEN}/playlist.m3u")).text()
    assert f"http://192.168.1.35:8090/{TOKEN}/files/videos/clip.mp4" in text


async def test_remote_playlist_appended_and_unreachable_skipped(tmp_path, upstream, client_factory):
    async def home(request):
        return _web.Response(text='#EXTM3U\n#EXTINF:-1 group-title="Opnames (thuis)",Home rec\n'
                                  'http://192.168.1.35:8090/t/files/thuis/x.ts\n',
                             content_type="audio/x-mpegurl")
    app = _web.Application(); app.router.add_get("/pl.m3u", home)
    srv = TestServer(app); await srv.start_server()
    try:
        cfg = _merge_config(tmp_path, upstream, remote_playlists=(
            str(srv.make_url("/pl.m3u")), "http://127.0.0.1:1/unreachable.m3u"))
        client = await client_factory(cfg)
        text = await (await client.get("/tv/ab12")).text()
        assert 'group-title="Opnames (thuis)",Home rec\nhttp://192.168.1.35:8090/t/files/thuis/x.ts' in text
        assert 'group-title="Opnames"' in text                     # the Pi's own group stays
        assert text.count("#EXTM3U") == 1
        assert "https://tv.example/tv/ab12/npo-1.m3u8" in text     # live channels untouched
    finally:
        await srv.close()



async def test_icons_are_public_and_safe(tmp_path, client_factory):
    icons = tmp_path / "icons"; icons.mkdir()
    (icons / "sport.png").write_bytes(b"\x89PNG fake")
    cfg = Config(**{**_config(tmp_path).__dict__, "icon_dir": str(icons)})
    client = await client_factory(cfg)
    resp = await client.get("/icons/sport.png")                    # no token needed
    assert resp.status == 200 and resp.headers["Content-Type"] == "image/png"
    assert await resp.read() == b"\x89PNG fake"
    assert (await client.get("/icons/missing.png")).status == 404
    assert (await client.get("/icons/..%2F..%2Fetc%2Fpasswd.png")).status in (403, 404)
    assert (await client.get("/icons/Sport.png")).status == 404       # strict names only
