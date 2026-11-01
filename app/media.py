"""Media server: an always-fresh M3U playlist of local recordings and videos.

Runs as its own process (``python -m app.media``, unit ``tv-proxy-media``) so it
never touches the IPTV proxy code. nginx publishes it at https://tv.berrie.uk/media/
(prefix stripped), and it also answers directly on the LAN at :8090:

    GET /health                         -> OK (no token)
    GET /<token>/playlist.m3u           -> playlist, rebuilt on every request
    GET /<token>/files/<source>/<path>  -> the file itself, with HTTP Range
                                           support so players can seek
    GET /tv/<short_code>                -> the viewer's normal live playlist from
                                           tv-proxy, with the media entries appended
                                           for viewers in MEDIA_MERGE_VIEWERS

nginx sends /tv/<code> here so one playlist URL has both live TV and the
recordings. For viewers in MEDIA_MERGE_VIEWERS the live part is built here with
tv-proxy's own generate_user_m3u, but loading only the viewer's allowed channels
(tv-proxy loads all ~86k channels per request, which takes 20-80 s on the Pi 2
and makes TV apps time out). Every other viewer, and any error, falls back to
proxying tv-proxy's /tv/<code> unchanged.

The token sits in the path rather than a query string because some TV players
drop query strings or look at the URL suffix to decide how to play an item.

Configuration (environment, normally /etc/tv-proxy-media.env):

    MEDIA_TOKEN            required; long random string
    MEDIA_HOST             default 0.0.0.0
    MEDIA_PORT             default 8090
    MEDIA_SOURCES          comma-separated key:Label:/path entries, default
                           "recordings:Opnames:/var/lib/tv-proxy/recordings,
                            videos:Video's:/var/lib/tv-proxy/media"
    MEDIA_ALLOWED_NETWORKS comma-separated CIDRs allowed to connect, default
                           loopback + RFC1918 + Tailscale (100.64.0.0/10)
    MEDIA_PUBLIC_URL       base URL for links in the playlist, e.g.
                           https://tv.berrie.uk/media when served through
                           nginx + Cloudflare; by default the Host header the
                           player used is reused
    MEDIA_MIN_BYTES        files smaller than this are hidden, default 1 MiB
    MEDIA_TS_URLS          1 = in merged playlists every live channel URL ends in
                           .ts instead of .m3u8 (served by the relay below). The
                           provider sends continuous MPEG-TS, and TV players (LG
                           webOS / SS IPTV) treat .m3u8 as HLS and give up.
    MEDIA_RECORDING_LEAD   seconds before a scheduled recording starts that .ts
                           TV streams are stopped/refused, default 60. The
                           provider allows one stream at a time, so recordings
                           (any viewer) take priority over watching.
    MEDIA_BUSY_CLIP        video played instead of a channel while a recording has
                           priority, default /var/lib/tv-proxy/recording-in-progress.ts
                           (a still "Recording in progress" image, made once by
                           deploy/make-busy-clip.sh); without it the TV gets a 503.
    MEDIA_INUSE_CLIP       video played when the provider refuses a channel while
                           another stream is already running on tv-proxy (another
                           device is watching), default
                           /var/lib/tv-proxy/stream-in-use.ts; it ends when that
                           other stream stops so the TV can reconnect.
    MEDIA_RELAY_BUFFER_MB  RAM buffer per .ts stream between provider and TV, default
                           64 (~60 s of HD). The provider is always read at full
                           speed, so a short Wi-Fi dip at the TV does not make the
                           provider drop the stream; the TV catches up afterwards.
    MEDIA_RELAY_PREBUFFER  seconds collected before the first byte goes to the TV,
                           default 5, sent as one burst so the player starts with a
                           full buffer (live is that much behind).
    MEDIA_FORCE_BUSY       test switch, default /var/lib/tv-proxy/force-busy: if this
                           file holds a Unix time in the future, TV streams behave as
                           if a recording is running until then (no real recording).
    MEDIA_TS_TEST          comma-separated channel slugs that get an extra copy in
                           a "Test .ts" group, served as /tv/<code>/<slug>.ts
                           (same stream, .ts URL for players that pick the
                           decoder from the extension); default none
    MEDIA_MERGE_VIEWERS    comma-separated viewer names or ids whose /tv/<code>
                           playlist gets the media entries; default none
    MEDIA_UPSTREAM         tv-proxy base URL, default http://127.0.0.1:8081
    MEDIA_DB               tv-proxy database, default /var/lib/tv-proxy/mappings.db;
                           the "recordings" source lists only completed recordings
                           that still exist there (merged playlist: viewer's own)
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import os
import re
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web

from app.security import constant_time_eq, redact_token

logger = logging.getLogger("tvproxy.media")

VIDEO_TYPES = {
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".webm": "video/webm",
}

DEFAULT_SOURCES = "recordings:Opnames:/var/lib/tv-proxy/recordings,videos:Video's:/var/lib/tv-proxy/media"
DEFAULT_NETWORKS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fd7a:115c:a1e0::/48"

# The playlist is rebuilt from disk on every request (no in-memory cache); these
# headers stop players and any proxy in between from reusing an old copy.
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# 20260910-164000-Title-With-Dashes-68.part3.ts  /  ...-Title-65-part02.ts
_REC_RE = re.compile(
    r"^(?P<date>\d{8})-(?P<time>\d{4})\d{2}-(?P<title>.+?)(?:-\d+)?(?:[.-]part(?P<part>\d+))?$"
)


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    root: Path


@dataclass(frozen=True)
class Entry:
    source: Source
    rel: str  # POSIX path relative to source.root
    title: str
    group: str


@dataclass(frozen=True)
class Config:
    token: str
    sources: tuple[Source, ...]
    networks: tuple[ipaddress._BaseNetwork, ...]
    public_url: str = ""
    min_bytes: int = 1024 * 1024
    host: str = "0.0.0.0"
    port: int = 8090
    merge_viewers: frozenset = frozenset()
    ts_test: tuple = ()
    ts_urls: bool = False
    recording_lead: int = 60
    busy_clip: str = "/var/lib/tv-proxy/recording-in-progress.ts"
    relay_buffer_mb: int = 64
    relay_prebuffer: float = 5.0
    inuse_clip: str = "/var/lib/tv-proxy/stream-in-use.ts"
    force_busy: str = "/var/lib/tv-proxy/force-busy"
    upstream: str = "http://127.0.0.1:8081"
    db_path: str = "/var/lib/tv-proxy/mappings.db"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            token=env.get("MEDIA_TOKEN", ""),
            sources=parse_sources(env.get("MEDIA_SOURCES", DEFAULT_SOURCES)),
            networks=parse_networks(env.get("MEDIA_ALLOWED_NETWORKS", DEFAULT_NETWORKS)),
            public_url=env.get("MEDIA_PUBLIC_URL", "").rstrip("/"),
            min_bytes=int(env.get("MEDIA_MIN_BYTES", str(1024 * 1024))),
            host=env.get("MEDIA_HOST", "0.0.0.0"),
            port=int(env.get("MEDIA_PORT", "8090")),
            merge_viewers=frozenset(v.strip() for v in env.get("MEDIA_MERGE_VIEWERS", "").split(",") if v.strip()),
            recording_lead=int(env.get("MEDIA_RECORDING_LEAD", "60")),
            force_busy=env.get("MEDIA_FORCE_BUSY", "/var/lib/tv-proxy/force-busy"),
            inuse_clip=env.get("MEDIA_INUSE_CLIP", "/var/lib/tv-proxy/stream-in-use.ts"),
            relay_buffer_mb=int(env.get("MEDIA_RELAY_BUFFER_MB", "64")),
            relay_prebuffer=float(env.get("MEDIA_RELAY_PREBUFFER", "5")),
            busy_clip=env.get("MEDIA_BUSY_CLIP", "/var/lib/tv-proxy/recording-in-progress.ts"),
            ts_urls=env.get("MEDIA_TS_URLS", "") in ("1", "true", "yes"),
            ts_test=tuple(v.strip() for v in env.get("MEDIA_TS_TEST", "").split(",") if v.strip()),
            upstream=env.get("MEDIA_UPSTREAM", "http://127.0.0.1:8081").rstrip("/"),
            db_path=env.get("MEDIA_DB", "/var/lib/tv-proxy/mappings.db"),
        )


def parse_sources(spec: str) -> tuple[Source, ...]:
    sources = []
    for item in filter(None, (part.strip() for part in spec.split(","))):
        key, label, path = item.split(":", 2)
        key = key.strip().lower()
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid media source key: {key!r}")
        sources.append(Source(key, label.strip() or key, Path(path.strip())))
    if len({s.key for s in sources}) != len(sources):
        raise ValueError("duplicate media source key")
    return tuple(sources)


def parse_networks(spec: str) -> tuple[ipaddress._BaseNetwork, ...]:
    return tuple(
        ipaddress.ip_network(part.strip(), strict=False)
        for part in spec.split(",") if part.strip()
    )


def _natural_key(text: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def _pretty(text: str) -> str:
    parts = (p.replace("_", " ").replace("-", " ") for p in text.split("---"))
    return re.sub(r"\s+", " ", " - ".join(parts)).strip()


def recording_title(stem: str) -> tuple[str, int | None]:
    """Return (display title, part number or None) for a recorder file stem."""
    m = _REC_RE.match(stem)
    if not m:
        return _pretty(stem), None
    d, t = m["date"], m["time"]
    title = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]} {_pretty(m['title'])}"
    return title, int(m["part"]) if m["part"] is not None else None


def _video_files(root: Path, min_bytes: int):
    """Yield (relative POSIX path, stat) for playable files under root."""
    if not root.is_dir():
        return
    root_real = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or Path(name).suffix.lower() not in VIDEO_TYPES:
                continue
            full = Path(dirpath, name)
            try:
                if not full.resolve().is_relative_to(root_real):
                    continue
                st = full.stat()
            except OSError:
                continue
            if st.st_size >= min_bytes:
                yield full.relative_to(root).as_posix(), st


def scan(config: Config, viewer_id: int | None = None) -> list[Entry]:
    """Playable entries. Recordings come from tv-proxy's database (only rows that
    still exist, are completed and whose file is on disk; only viewer_id's own
    when given), everything else from the folders."""
    entries: list[Entry] = []
    for source in config.sources:
        if source.key == "recordings":
            entries.extend(_recording_entries(source, config, viewer_id))
            continue
        files = list(_video_files(source.root, config.min_bytes))
        for rel, _st in sorted(files, key=lambda f: _natural_key(f[0])):
            folder = str(Path(rel).parent)
            group = source.label if folder == "." else f"{source.label} / {folder}"
            entries.append(Entry(source, rel, _pretty(Path(rel).stem), group))
    return entries


def completed_recordings(db_path: str, viewer_id: int | None = None) -> list[dict]:
    """Completed recordings that still exist in tv-proxy's database.

    Files in the recordings folder without a row here are leftovers of deleted
    recordings (tv-proxy removes the row and the main file, but not split
    part files) and must not be listed."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT id, viewer_id, title, start_at, local_path FROM recordings"
               " WHERE status = 'completed' AND local_path != ''")
        params: tuple = ()
        if viewer_id is not None:
            sql += " AND viewer_id = ?"
            params = (viewer_id,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY start_at DESC, id DESC", params)]
    finally:
        conn.close()


def _recording_entries(source: Source, config: Config, viewer_id: int | None) -> list[Entry]:
    root = source.root.resolve()
    entries = []
    for row in completed_recordings(config.db_path, viewer_id):
        path = Path(row["local_path"])
        try:
            real = path.resolve()
            if not real.is_relative_to(root) or not real.is_file():
                continue
            if real.stat().st_size < config.min_bytes or real.suffix.lower() not in VIDEO_TYPES:
                continue
        except OSError:
            continue
        stamp_title, _part = recording_title(real.stem)
        stamp = stamp_title[:16] if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d", stamp_title) else ""
        title = " ".join(filter(None, (stamp, (row["title"] or "").strip()))) or stamp_title
        entries.append(Entry(source, real.relative_to(root).as_posix(), title, source.label))
    return entries


def _attr(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ")


def build_playlist(entries: list[Entry], base_url: str, token: str) -> str:
    lines = ["#EXTM3U"]
    for e in entries:
        url = f"{base_url}/{quote(token, safe='')}/files/{e.source.key}/{quote(e.rel)}"
        title = e.title.replace("\n", " ").replace(",", " ")
        lines.append(f'#EXTINF:-1 tvg-name="{_attr(e.title)}" group-title="{_attr(e.group)}",{title}')
        lines.append(url)
    return "\n".join(lines) + "\n"


def merge_playlists(live: str, media: str) -> str:
    """Append the entries of the media playlist to the live one."""
    extra = media.split("\n", 1)[1] if media.startswith("#EXTM3U") else media
    if not extra.strip():
        return live
    if not live.strip():
        live = "#EXTM3U\n"
    return live.rstrip("\n") + "\n" + extra


def build_live_playlist(db_path: str, short_code: str,
                        allowed: frozenset) -> tuple[str, int | None] | None:
    """(live playlist, viewer id) for a merge viewer, or None to let tv-proxy
    handle it. The viewer id is None when the viewer gets nothing extra
    (disabled or expired).

    Mirrors tv-proxy's handle_playlist_m3u (same viewer checks, same
    generate_user_m3u output) but only reads the viewer's own channels. Its
    LAN/filesharez recording links are left out; the media entries replace them.
    """
    if not allowed:
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        viewer = conn.execute(
            "SELECT * FROM viewers WHERE short_code = ? AND short_enabled = 1", (short_code,)
        ).fetchone()
        if not viewer or (str(viewer["id"]) not in allowed and viewer["name"] not in allowed):
            return None
        if viewer["disabled"] or (viewer["expires_at"] and viewer["expires_at"] < time.time()):
            return "#EXTM3U\n", None
        ids = json.loads(viewer["allowed_channels"] or "[]")
        if not ids:
            return None  # "all channels": leave that to tv-proxy
        channels = [dict(r) for r in conn.execute(
            "SELECT * FROM channels WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id ASC",
            (json.dumps([int(i) for i in ids]),),
        )]
        row = conn.execute("SELECT value FROM settings WHERE key = 'public_url'").fetchone()
        public_url = (row[0] if row else "") or "https://tv.berrie.uk"
    finally:
        conn.close()
    from app.streaming import generate_user_m3u
    live = generate_user_m3u(public_url, channels, viewer["token"], short_code, [])
    return live, viewer["id"]


def add_ts_copies(live: str, slugs: tuple, group: str = "Test .ts") -> str:
    """Append a copy of the given channels whose URL ends in .ts instead of .m3u8."""
    if not slugs:
        return live
    lines = live.splitlines()
    extra = []
    for i, line in enumerate(lines[:-1]):
        url = lines[i + 1]
        if not line.startswith("#EXTINF") or not url.endswith(".m3u8"):
            continue
        slug = url.rsplit("/", 1)[-1][:-len(".m3u8")]
        if slug in slugs and "/tv/" in url:
            head, name = line.rsplit(",", 1)
            head = re.sub(r'group-title="[^"]*"', f'group-title="{group}"', head)
            extra += [f"{head},{name} (.ts)", url[:-len(".m3u8")] + ".ts"]
    return live if not extra else live.rstrip("\n") + "\n" + "\n".join(extra) + "\n"


def _recordings_query(db_path: str, sql: str, params: tuple) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


_REC_COLUMNS = ("SELECT id, COALESCE(NULLIF(title, ''), 'Recording ' || id) AS title,"
                " status, start_at, end_at FROM recordings")


def active_streams(port: int, proc_files=("/proc/net/tcp", "/proc/net/tcp6")) -> int:
    """Established TCP connections whose local port is `port` (i.e. clients of
    tv-proxy: nginx for website/TV viewers, ffmpeg for recordings)."""
    count = 0
    for name in proc_files:
        try:
            with open(name) as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[3] == "01" \
                            and int(parts[1].rsplit(":", 1)[1], 16) == port:
                        count += 1
        except OSError:
            continue
    return count


def forced_busy(path: str, now: float | None = None) -> dict | None:
    """Test switch: the file contains a Unix time; busy until then."""
    now = time.time() if now is None else now
    try:
        until = float(Path(path).read_text().strip() or 0)
    except (OSError, ValueError):
        return None
    return {"id": 0, "title": "test", "status": "recording", "start_at": now, "end_at": until} \
        if until > now else None


def recording_has_priority(db_path: str, lead: int, now: float | None = None) -> dict | None:
    """The recording that is running or starts within `lead` seconds, else None."""
    now = time.time() if now is None else now
    rows = _recordings_query(
        db_path,
        _REC_COLUMNS + " WHERE status = 'recording'"
        "    OR (status = 'scheduled' AND start_at <= ? AND end_at > ?)"
        " ORDER BY start_at LIMIT 1",
        (now + lead, now))
    return rows[0] if rows else None


_TV_M3U8_RE = re.compile(r"^(https?://[^\s]*/tv/[^/\s]+/[^/\s]+)\.m3u8$")


def use_ts_urls(live: str) -> str:
    """Rewrite every /tv/<code>/<slug>.m3u8 URL line to .../<slug>.ts."""
    return "\n".join(_TV_M3U8_RE.sub(r"\1.ts", line) for line in live.split("\n"))


_FORWARD_HEADERS = ("User-Agent", "X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto",
                    "CF-Connecting-IP", "Accept-Language")


class ChunkedFileResponse(web.FileResponse):
    """FileResponse that always streams in chunks instead of using sendfile().

    The Pi 2 is 32-bit: os.sendfile() raises OverflowError when asked for more
    than 2 GiB, and TV players request "bytes=N-" (the rest of a multi-GB
    recording) right away. The chunked path reads 256 KiB at a time and only as
    fast as the player takes it (TCP backpressure); Range/seeking is unchanged.
    tests/test_media.py::test_open_ended_range_on_huge_file guards this override.
    """

    async def _sendfile(self, request, fobj, offset, count):
        writer = await web.StreamResponse.prepare(self, request)
        return await self._sendfile_fallback(writer, fobj, offset, count)


class ChunkBuffer:
    """Bounded FIFO of byte chunks between the provider reader and the TV writer."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.chunks: collections.deque[bytes] = collections.deque()
        self.size = 0
        self.eof = False
        self.cond = asyncio.Condition()

    async def put(self, chunk: bytes) -> None:
        async with self.cond:
            # only blocks when the TV is a whole buffer (about a minute) behind
            await self.cond.wait_for(lambda: self.size + len(chunk) <= self.max_bytes
                                     or not self.chunks)
            self.chunks.append(chunk)
            self.size += len(chunk)
            self.cond.notify_all()

    async def close(self) -> None:
        async with self.cond:
            self.eof = True
            self.cond.notify_all()

    async def get(self) -> bytes | None:
        """Next chunk, or None at end of stream."""
        async with self.cond:
            await self.cond.wait_for(lambda: self.chunks or self.eof)
            if not self.chunks:
                return None
            chunk = self.chunks.popleft()
            self.size -= len(chunk)
            self.cond.notify_all()
            return chunk


HTTP_KEY = web.AppKey("http", aiohttp.ClientSession)
PRIORITY_CHECK_SECONDS = 5.0
BUSY_CLIP_SECONDS = 600      # length of recording-in-progress.ts (make-busy-clip.sh)
# Exactly playback speed: anything faster piles up in TCP/Cloudflare/TV buffers,
# and with this low-bitrate still clip a few MB is minutes of video the TV would
# keep showing after the recording ended. BUSY_PREBUFFER gives the head start.
BUSY_PACE = 1.0
BUSY_PREBUFFER = 4.0         # seconds the TV may have buffered ahead


class _NoMedia(Exception):
    """Viewer gets the live playlist only."""


def create_app(config: Config) -> web.Application:
    if not config.token:
        raise RuntimeError("MEDIA_TOKEN is not set")
    sources = {s.key: s for s in config.sources}
    upstream_port = urlsplit(config.upstream).port or 80

    @web.middleware
    async def guard(request: web.Request, handler):
        try:
            ip = ipaddress.ip_address((request.remote or "").split("%")[0])
        except ValueError:
            raise web.HTTPForbidden()
        if not any(ip in net for net in config.networks):
            raise web.HTTPForbidden()
        if request.path != "/health" and request.match_info.route.name not in ("merged", "ts"):
            if not constant_time_eq(request.match_info.get("token", ""), config.token):
                logger.warning("bad token from %s for %s", ip,
                               redact_token(request.match_info.get("token", "")))
                raise web.HTTPForbidden()
        return await handler(request)

    async def health(_request):
        return web.Response(text="OK")

    async def playlist(request: web.Request):
        base = config.public_url or f"{request.scheme}://{request.host}"
        entries = await asyncio.to_thread(scan, config)
        logger.info("playlist for %s: %d items", request.remote, len(entries))
        return web.Response(
            text=build_playlist(entries, base, config.token),
            content_type="audio/x-mpegurl",
            charset="utf-8",
            headers=NO_CACHE_HEADERS,
        )

    async def merged(request: web.Request):
        code = request.match_info["code"]
        result = None
        try:
            result = await asyncio.to_thread(build_live_playlist, config.db_path, code,
                                             config.merge_viewers)
        except Exception:
            logger.exception("fast live playlist failed; falling back to tv-proxy")
        if result is None:
            return await proxy_playlist(request, code)
        live, viewer_id = result
        live = use_ts_urls(live) if config.ts_urls else add_ts_copies(live, config.ts_test)
        body = live
        try:  # never break live TV because of the media part
            if viewer_id is None:
                raise _NoMedia()
            base = config.public_url or f"{request.scheme}://{request.host}"
            entries = await asyncio.to_thread(scan, config, viewer_id)
            body = merge_playlists(live, build_playlist(entries, base, config.token))
            logger.info("merged playlist: %d live lines + %d media items",
                        live.count("#EXTINF"), len(entries))
        except _NoMedia:
            pass
        except Exception:
            logger.exception("media scan failed; serving live playlist only")
        return web.Response(text=body, charset="utf-8", content_type="audio/x-mpegurl",
                            headers=NO_CACHE_HEADERS)

    async def ts_stream(request: web.Request):
        """Same live stream as tv-proxy's /tv/<code>/<slug>.m3u8, under a .ts URL.

        tv-proxy does all auth and limits; this only relays the bytes."""
        code, slug = request.match_info["code"], request.match_info["slug"]
        busy = await priority_check()
        if busy:
            logger.info("ts stream %s refused: recording %r has priority", slug, busy["title"])
            return await busy_response(request, busy)
        headers = {h: request.headers[h] for h in _FORWARD_HEADERS if h in request.headers}
        headers["Host"] = request.host
        url = f"{config.upstream}/tv/{quote(code, safe='')}/{quote(slug, safe='')}.m3u8"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
        try:
            async with request.app[HTTP_KEY].get(url, headers=headers, timeout=timeout) as up:
                if up.status != 200:
                    body, status, ctype = await up.read(), up.status, up.content_type
                    refused = status == 502 and b"upstream-non-200" in body
                    up.release()
                    if refused and await other_device_watching():
                        logger.info("ts stream %s: provider busy with another device", slug)
                        return await paced_clip(request, Path(config.inuse_clip),
                                                still_blocked=other_device_watching)
                    return web.Response(status=status, body=body, content_type=ctype)
                ctype = up.headers.get("Content-Type", "")
                if "mpegurl" in ctype.lower():   # a real HLS playlist: pass it through
                    return web.Response(body=await up.read(), headers={
                        "Content-Type": ctype, "Cache-Control": "no-store"})
                out = web.StreamResponse(headers={"Content-Type": "video/mp2t",
                                                  "Cache-Control": "no-store"})
                await out.prepare(request)
                buf = ChunkBuffer(config.relay_buffer_mb * 1024 * 1024)

                async def read_provider():
                    try:
                        async for chunk in up.content.iter_chunked(64 * 1024):
                            await buf.put(chunk)
                    except (aiohttp.ClientError, TimeoutError) as exc:
                        logger.warning("ts stream %s: provider side ended: %s",
                                       slug, type(exc).__name__)
                    finally:
                        await buf.close()

                reader = asyncio.create_task(read_provider())
                started = time.monotonic()
                next_check = started + PRIORITY_CHECK_SECONDS
                try:
                    # start buffer: collect a few seconds, then send it as one burst
                    while (time.monotonic() - started < config.relay_prebuffer
                           and not buf.eof):
                        await asyncio.sleep(0.2)
                    while (chunk := await buf.get()) is not None:
                        await out.write(chunk)
                        if time.monotonic() >= next_check:
                            next_check = time.monotonic() + PRIORITY_CHECK_SECONDS
                            busy = await priority_check()
                            if busy:
                                logger.info("ts stream %s stopped: recording %r has priority",
                                            slug, busy["title"])
                                break   # closes tv-proxy's (and the provider's) stream
                except (ConnectionError, asyncio.CancelledError):
                    pass  # viewer left; leaving the block closes tv-proxy's stream too
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                return out
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("ts relay failed for %s: %s", slug, type(exc).__name__)
            raise web.HTTPBadGateway()

    async def busy_response(request: web.Request, rec: dict) -> web.StreamResponse:
        """"Recording in progress" clip while a recording has priority."""
        return await paced_clip(request, Path(config.busy_clip), still_blocked=priority_check,
                                missing_text=f"Recording in progress: {rec['title']}")

    async def other_device_watching() -> bool:
        await asyncio.sleep(0.3)        # let our own just-closed upstream socket go away
        return active_streams(upstream_port) > 0

    async def paced_clip(request: web.Request, clip: Path, still_blocked,
                         missing_text: str = "Stream not available") -> web.StreamResponse:
        """Play a notice clip for as long as `still_blocked()` is truthy.

        The clip is sent at its own playback speed (plus a few seconds head start) and the
        stream ends within ~5 s of the reason going away, so the TV only has a few
        seconds buffered, sees the end of the stream and reconnects to the channel
        by itself. If the reason outlasts the clip, the reconnect gets it again.
        """
        if not clip.is_file():
            return web.Response(status=503, text=missing_text, headers={"Retry-After": "60"})
        size = clip.stat().st_size
        rate = size / BUSY_CLIP_SECONDS * BUSY_PACE          # bytes per second
        out = web.StreamResponse(headers={"Content-Type": "video/mp2t",
                                          "Cache-Control": "no-store"})
        await out.prepare(request)
        start = time.monotonic()
        sent = 0
        next_check = start + PRIORITY_CHECK_SECONDS
        try:
            with open(clip, "rb") as f:
                while True:
                    # Check on a timer, whatever the TV is doing: once its buffer is
                    # full the TV reads at playback speed, slower than we pace, so
                    # we may never "catch up" (that is how the first version missed
                    # the end of a recording).
                    if time.monotonic() >= next_check:
                        next_check = time.monotonic() + PRIORITY_CHECK_SECONDS
                        if not await still_blocked():
                            logger.info("%s: reason gone, ending clip so the TV reconnects",
                                        clip.name)
                            break
                    # allow BUSY_PREBUFFER seconds ahead of real time, no more
                    allowed = int(rate * (time.monotonic() - start + BUSY_PREBUFFER))
                    if allowed > sent:
                        chunk = f.read(min(allowed - sent, 64 * 1024))
                        if not chunk:
                            break                            # clip finished
                        await out.write(chunk)
                        sent += len(chunk)
                    else:
                        await asyncio.sleep(0.5)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return out

    async def priority_check() -> dict | None:
        try:
            return (forced_busy(config.force_busy)
                    or await asyncio.to_thread(recording_has_priority, config.db_path,
                                               config.recording_lead))
        except Exception:   # never block TV because the check itself failed
            logger.exception("recording priority check failed")
            return None

    async def proxy_playlist(request: web.Request, code: str):
        headers = {h: request.headers[h] for h in _FORWARD_HEADERS if h in request.headers}
        headers["Host"] = request.host
        try:
            async with request.app[HTTP_KEY].get(f"{config.upstream}/tv/{quote(code, safe='')}",
                                                 headers=headers) as up:
                body = await up.text()
                status, ctype = up.status, up.content_type
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.error("upstream playlist failed: %s", exc)
            raise web.HTTPBadGateway()
        return web.Response(text=body, status=status, charset="utf-8",
                            content_type=ctype or "audio/x-mpegurl", headers=NO_CACHE_HEADERS)

    async def media_file(request: web.Request):
        source = sources.get(request.match_info["source"])
        if source is None:
            raise web.HTTPNotFound()
        rel = request.match_info["path"]
        root = source.root.resolve()
        try:
            path = (root / rel).resolve()
        except (OSError, ValueError):
            raise web.HTTPNotFound()
        ctype = VIDEO_TYPES.get(path.suffix.lower())
        if ctype is None or not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound()
        # "private": the URL carries the token, so Cloudflare (or any shared
        # cache) must not keep a copy; the player itself may still buffer.
        return ChunkedFileResponse(path, headers={"Content-Type": ctype,
                                                  "Cache-Control": "private, no-transform"})

    async def http_client(app):
        timeout = aiohttp.ClientTimeout(total=120, connect=5)
        # force_close: no idle keep-alive sockets to tv-proxy that active_streams()
        # would mistake for another device watching
        app[HTTP_KEY] = aiohttp.ClientSession(timeout=timeout, auto_decompress=True,
                                              connector=aiohttp.TCPConnector(force_close=True))
        yield
        await app[HTTP_KEY].close()

    app = web.Application(middlewares=[guard])
    app.cleanup_ctx.append(http_client)
    app.router.add_get("/health", health)
    # Registered before /{token}/playlist.m3u so /tv/<code> always lands here.
    app.router.add_get("/tv/{code}", merged, name="merged")
    app.router.add_get("/tv/{code}/{slug}.ts", ts_stream, name="ts")
    for name in ("playlist.m3u", "playlist.m3u8"):
        app.router.add_get("/{token}/" + name, playlist)
    app.router.add_get("/{token}/files/{source}/{path:.+}", media_file)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    # access_log=None: the default access log would write the token to the journal.
    web.run_app(create_app(config), host=config.host, port=config.port,
                access_log=None, print=None)


if __name__ == "__main__":
    main()
