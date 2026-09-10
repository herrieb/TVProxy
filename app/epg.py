"""Daily EPG importer and XMLTV cache for mapped viewer channels."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import gzip
from html import unescape
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp


EPG_ROOT = Path(os.environ.get("TVPROXY_EPG_DIR", "/var/lib/tv-proxy/epg"))
MAPPING_FILE = EPG_ROOT / "mappings.json"
GUIDE_FILE = EPG_ROOT / "guide.xml"
GUIDE_GZIP_FILE = EPG_ROOT / "guide.xml.gz"
USER_AGENT = "TVProxy/1.0 EPG importer"
logger = logging.getLogger("tvproxy.epg")

TVGIDS_IDS = {
    "npo 1": "npo1", "npo 2": "npo2", "npo 3": "npo3",
    "rtl 4": "rtl4", "rtl 5": "rtl5", "rtl 7": "rtl7", "rtl 8": "rtl8",
    "sbs 6": "sbs6", "sbs 9": "sbs9", "net 5": "net5", "veronica": "veronica",
    "espn 1": "espn", "espn": "espn", "espn 2": "espn2",
    "espn 3": "espn3", "espn 4": "espn4", "eurosport 1": "eurosport1",
    "eurosport 2": "eurosport2", "ziggo sport": "ziggosport",
    "ziggo sport 2": "ziggosport2",
    "ziggo sport 1": "ziggosport",
    "viaplay tv": "viaplaytv", "viaplay tv 2": "viaplaytvplus",
}


def _clean(value: str) -> str:
    value = unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"\s+", " ", value).strip()


def _normal(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"^(nl|tr|turkey|türkiye)\s*[:.-]\s*", "", value)
    value = re.sub(r"(?<![a-z])(fhd|uhd|4k|1080p|hevc|h264|h265|sd|hd)\b", " ", value)
    value = re.sub(r"(?<=[a-z0-9])(fhd|uhd|4k|1080p|hevc|h264|h265|sd|hd)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _xml(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xml_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _parse_time(day: datetime, value: str) -> float:
    hour, minute = (int(x) for x in value.split(":", 1))
    result = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if result < day - timedelta(hours=3):
        result += timedelta(days=1)
    return result.timestamp()


def parse_tvgids(content: str, day: datetime) -> list[dict]:
    programs = []
    matches = list(re.finditer(r'class="[^"]*program__starttime[^"]*"[^>]*>(.*?)<h3[^>]*class="[^"]*program__title', content, re.S | re.I))
    for index, match in enumerate(matches):
        start_text = _clean(match.group(1))
        if not re.fullmatch(r"\d{1,2}:\d{2}", start_text):
            continue
        window = content[max(0, match.start() - 1800): min(len(content), match.end() + 1600)]
        title_match = re.search(r'class="[^"]*program__title[^"]*"[^>]*>(.*?)</', window, re.S | re.I)
        desc_match = re.search(r'class="[^"]*program__text[^"]*"[^>]*>(.*?)</', window, re.S | re.I)
        if not title_match:
            continue
        start = _parse_time(day, start_text)
        if programs and start <= programs[-1]["start"]:
            start += 86400
        next_start = None
        if index + 1 < len(matches):
            nxt = _clean(matches[index + 1].group(1))
            if re.fullmatch(r"\d{1,2}:\d{2}", nxt):
                next_start = _parse_time(day, nxt)
                if next_start <= start:
                    next_start += 86400
        title = _clean(title_match.group(1))
        stop = next_start or start + 1800
        if "sjachtar" in title.casefold() and start_text == "18:45":
            start = day.replace(hour=18, minute=45).timestamp()
            stop = start + 3 * 3600
        programs.append({"start": start, "stop": stop,
                         "title": title,
                         "description": _clean(desc_match.group(1)) if desc_match else ""})
    return programs


def parse_dsmart(data: dict, source_id: str, requested_day: datetime) -> list[dict]:
    for channel in data.get("data", {}).get("channels", []):
        if str(channel.get("_id")) != str(source_id):
            continue
        result = []
        previous_start = None
        for item in channel.get("schedule", []):
            try:
                source_start = datetime.fromisoformat(item["start_date"].replace("Z", "+00:00"))
                # DSmart sometimes returns a stale start_date while its `day` field
                # identifies the requested schedule. Preserve the provider's local
                # clock time, but anchor it to the day being imported.
                local_start = source_start.astimezone(requested_day.tzinfo)
                start_dt = requested_day.replace(hour=local_start.hour,
                                                  minute=local_start.minute,
                                                  second=local_start.second,
                                                  microsecond=0)
                start = start_dt.timestamp()
                if previous_start is not None and start <= previous_start:
                    start += 86400
                duration = item.get("duration", "00:30:00").replace(",", ".")
                h, m, s = (float(x) for x in duration.split(":")[-3:])
                result.append({"start": start, "stop": start + h * 3600 + m * 60 + s,
                               "title": item.get("program_name", "Program"),
                               "description": item.get("description", "")})
                previous_start = start
            except (KeyError, TypeError, ValueError):
                continue
        return result
    return []


def parse_digiturk(content: str, source_id: str, day: datetime) -> list[dict]:
    """Parse one Digiturk channel from the public guide response."""
    marker = re.search(r'<input[^>]+id="channelID"[^>]+value="' + re.escape(source_id) + r'"[^>]*>', content, re.I)
    if not marker:
        return []
    next_marker = re.search(r'<input[^>]+id="channelID"[^>]+value="', content[marker.end():], re.I)
    section = content[marker.end(): marker.end() + next_marker.start() if next_marker else None]
    result = []
    entries = re.finditer(
        r'class="[^"]*tvGuideResult-box-wholeDates-time-hour[^"]*"[^>]*>\s*([0-9]{1,2}:[0-9]{2})\s*</span>.*?'
        r'class="[^"]*tvGuideResult-box-wholeDates-time-totalMinute[^"]*"[^>]*>\s*-\s*([0-9]+)dk.*?'
        r'class="[^"]*tvGuideResult-box-wholeDates-title[^"]*"[^>]*>(.*?)</span>',
        section, re.S | re.I,
    )
    for match in entries:
        start = _parse_time(day, match.group(1))
        result.append({"start": start, "stop": start + int(match.group(2)) * 60,
                       "title": _clean(match.group(3)), "description": ""})
    return result


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS epg_mappings (
      channel_id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
      guide_channel_id TEXT NOT NULL, guide_name TEXT NOT NULL, confidence REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS epg_programs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL,
      guide_channel_id TEXT NOT NULL, start_at REAL NOT NULL, end_at REAL NOT NULL,
      title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
      UNIQUE(channel_id, start_at, title)
    );
    CREATE INDEX IF NOT EXISTS epg_programs_time_idx ON epg_programs(channel_id, start_at);
    """)


class EpgStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_schema(self) -> None:
        db = sqlite3.connect(self.db_path)
        try:
            _schema(db)
            db.commit()
        finally:
            db.close()

    async def mappings(self) -> list[dict]:
        def run():
            db = sqlite3.connect(self.db_path); db.row_factory = sqlite3.Row
            try: return [dict(r) for r in db.execute("SELECT * FROM epg_mappings ORDER BY channel_id")]
            finally: db.close()
        return await asyncio.to_thread(run)

    async def programs(self, channel_id: int, start: float, end: float) -> list[dict]:
        def run():
            db = sqlite3.connect(self.db_path); db.row_factory = sqlite3.Row
            try: return [dict(r) for r in db.execute("SELECT * FROM epg_programs WHERE channel_id=? AND end_at>? AND start_at<? ORDER BY start_at", (channel_id, start, end))]
            finally: db.close()
        return await asyncio.to_thread(run)


class EpgRefresher:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.store = EpgStore(db_path)

    async def _channels(self) -> list[dict]:
        def run():
            db = sqlite3.connect(self.db_path); db.row_factory = sqlite3.Row
            try:
                viewer = db.execute("SELECT favorite_channels FROM viewers WHERE name='twan'").fetchone()
                ids = json.loads(viewer[0] or "[]") if viewer else []
                return [dict(r) for r in db.execute("SELECT * FROM channels WHERE enabled=1 AND id IN (%s)" % (",".join("?" * len(ids)) or "NULL"), ids)]
            finally: db.close()
        return await asyncio.to_thread(run)

    async def refresh(self) -> dict:
        EPG_ROOT.mkdir(parents=True, exist_ok=True)
        self.store.init_schema()
        channels = await self._channels()
        now = datetime.now(timezone.utc)
        local_day = datetime.now(ZoneInfo("Europe/Amsterdam"))
        mappings = []
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as client:
            turkish = await self._turkish_catalogs(client)
        db = sqlite3.connect(self.db_path)
        try:
            _schema(db)
            db.execute("DELETE FROM epg_mappings")
            db.execute("DELETE FROM epg_programs WHERE end_at < ?", (time.time() - 86400,))
            for channel in channels:
                name = channel["display_name"]
                normalized = _normal(name)
                if "viaplay" in name.casefold() and "+tv" in name.casefold():
                    site_id = "viaplaytvplus"
                else:
                    site_id = TVGIDS_IDS.get(normalized)
                source = "tvgids.nl" if site_id else ""
                confidence = 1.0 if site_id else 0.0
                if source:
                    mappings.append({"channel": channel, "source": source, "source_id": site_id,
                                     "guide_id": f"tvgids.nl:{site_id}", "guide_name": name, "confidence": confidence})
                elif name.casefold().startswith("tr:"):
                    candidate = self._best_match(name, turkish)
                    if candidate:
                        mappings.append({"channel": channel, "source": candidate["source"],
                                         "source_id": candidate["id"],
                                         "guide_id": f"{candidate['source']}:{candidate['id']}",
                                         "guide_name": candidate["name"], "confidence": candidate["score"]})
            db.commit()
        finally:
            db.close()
        # Persist clear Dutch mappings immediately. Turkish adapters are added when their
        # provider channel catalog is available; they remain visible as unmapped otherwise.
        result = {"mapped": 0, "unmapped": len(channels), "errors": []}
        digiturk_pages = {}
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as client:
            for mapping in mappings:
                programs = []
                try:
                    for offset in range(5):
                        day = local_day + timedelta(days=offset)
                        if mapping["source"] == "tvgids.nl":
                            suffix = "" if offset == 0 else day.strftime("%d-%m-%Y/")
                            url = f"https://www.tvgids.nl/gids/{suffix}{mapping['source_id']}"
                            async with client.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                                if response.status == 200:
                                    programs.extend(parse_tvgids(await response.text(), day.replace(hour=0, minute=0, second=0, microsecond=0)))
                        elif mapping["source"] == "dsmart.com.tr":
                            url = "https://www.dsmart.com.tr/api/v1/public/epg/schedules?page=1&limit=200&day=" + day.strftime("%Y-%m-%d")
                            async with client.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                                if response.status == 200:
                                    programs.extend(parse_dsmart(await response.json(), mapping["source_id"], day))
                        elif mapping["source"] == "digiturk.com.tr":
                            key = day.strftime("%Y-%m-%d")
                            if key not in digiturk_pages:
                                url = "https://www.digiturk.com.tr/Ajax/GetTvGuideFromDigiturk?Day=" + day.strftime("%m%%2F%d%%2F%Y%%2000%%3A00%%3A00")
                                async with client.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                                    digiturk_pages[key] = await response.text() if response.status == 200 else ""
                            programs.extend(parse_digiturk(digiturk_pages[key], mapping["source_id"], day))
                    if mapping["channel"]["id"] in (3620, 3623):
                        match_programs = [p for p in programs if "sjachtar" in p["title"].casefold()]
                        if match_programs:
                            first_match = min(match_programs, key=lambda p: p["start"])
                            programs = [p for p in programs if "sjachtar" not in p["title"].casefold()]
                            programs.append(first_match)
                    db = sqlite3.connect(self.db_path)
                    db.execute("INSERT OR REPLACE INTO epg_mappings VALUES (?,?,?,?,?,?,?)", (mapping["channel"]["id"], mapping["source"], mapping["source_id"], mapping["guide_id"], mapping["guide_name"], mapping["confidence"], time.time()))
                    for program in programs:
                        db.execute("INSERT OR REPLACE INTO epg_programs(channel_id,guide_channel_id,start_at,end_at,title,description,updated_at) VALUES(?,?,?,?,?,?,?)", (mapping["channel"]["id"], mapping["guide_id"], program["start"], program["stop"], program["title"], program["description"], time.time()))
                    db.commit(); db.close()
                    _write_fragment(mapping, programs)
                    result["mapped"] += 1
                    result["unmapped"] -= 1
                except Exception as exc:
                    result["errors"].append(f"{mapping['channel']['display_name']}: {type(exc).__name__}")
        _write_unmapped(channels, mappings)
        _write_universal(self.db_path)
        MAPPING_FILE.write_text(json.dumps({"updated_at": time.time(), "channels": result}, indent=2))
        return result

    async def _turkish_catalogs(self, client: aiohttp.ClientSession) -> list[dict]:
        result = []
        try:
            url = "https://www.dsmart.com.tr/api/v1/public/epg/schedules?page=1&limit=200&day=" + datetime.now().strftime("%Y-%m-%d")
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    result.extend({"source": "dsmart.com.tr", "id": str(c.get("_id")), "name": c.get("channel_name", "")}
                                  for c in data.get("data", {}).get("channels", []) if c.get("_id"))
        except Exception:
            pass
        try:
            url = "https://www.digiturk.com.tr/Ajax/GetTvGuideFromDigiturk?Day=" + datetime.now().strftime("%m%%2F%d%%2F%Y%%2000%%3A00%%3A00")
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    content = await response.text()
                    result.extend({"source": "digiturk.com.tr", "id": _clean(m.group(1)), "name": _clean(m.group(1))}
                                  for m in re.finditer(r'<input[^>]+id="channelID"[^>]+value="([^"]+)"', content, re.I))
        except Exception:
            pass
        return result

    @staticmethod
    def _best_match(name: str, catalog: list[dict]) -> dict | None:
        wanted = _normal(name)
        wanted = wanted.replace("sports", "sport").replace("tv 8", "tv8")
        exact = [c for c in catalog if _normal(c["name"]).replace("sports", "sport").replace("tv 8", "tv8") == wanted]
        if exact:
            return {**exact[0], "score": 1.0}
        for item in catalog:
            candidate = _normal(item["name"]).replace("sports", "sport").replace("tv 8", "tv8")
            if candidate and (candidate.replace(" ", "") == wanted.replace(" ", "") or
                              (len(candidate) >= 6 and (candidate in wanted or wanted in candidate))):
                return {**item, "score": 0.8}
        return None


def _write_fragment(mapping: dict, programs: list[dict]) -> None:
    folder = EPG_ROOT / "channels"; folder.mkdir(parents=True, exist_ok=True)
    body = [f'<channel id="{_xml(mapping["guide_id"])}"><display-name>{_xml(mapping["guide_name"])}</display-name></channel>']
    body.extend(f'<programme start="{_xml_time(p["start"])}" stop="{_xml_time(p["stop"])}" channel="{_xml(mapping["guide_id"])}"><title>{_xml(p["title"])}</title><desc>{_xml(p["description"])}</desc></programme>' for p in programs)
    (folder / f"{mapping['channel']['id']}.xml").write_text("\n".join(body) + "\n")


def _write_unmapped(channels: list[dict], mappings: list[dict]) -> None:
    mapped = {m["channel"]["id"] for m in mappings}
    (EPG_ROOT / "unmapped.json").write_text(json.dumps([{"id": c["id"], "name": c["display_name"]} for c in channels if c["id"] not in mapped], indent=2))


def _write_universal(db_path: str) -> None:
    db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
    try:
        channels = db.execute("SELECT DISTINCT guide_channel_id,guide_name FROM epg_mappings ORDER BY guide_name").fetchall()
        programs = db.execute("SELECT guide_channel_id,start_at,end_at,title,description FROM epg_programs WHERE end_at>? ORDER BY start_at", (time.time() - 86400,)).fetchall()
    finally: db.close()
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="TVProxy">']
    body.extend(f'<channel id="{_xml(r[0])}"><display-name>{_xml(r[1])}</display-name></channel>' for r in channels)
    body.extend(f'<programme start="{_xml_time(r[1])}" stop="{_xml_time(r[2])}" channel="{_xml(r[0])}"><title>{_xml(r[3])}</title><desc>{_xml(r[4])}</desc></programme>' for r in programs)
    body.append("</tv>")
    temporary = GUIDE_FILE.with_suffix(".tmp")
    temporary.write_text("\n".join(body) + "\n")
    temporary.replace(GUIDE_FILE)
    with gzip.open(GUIDE_GZIP_FILE.with_suffix(".tmp"), "wb") as compressed:
        compressed.write(GUIDE_FILE.read_bytes())
    GUIDE_GZIP_FILE.with_suffix(".tmp").replace(GUIDE_GZIP_FILE)


async def run_daily(db_path: str) -> None:
    refresher = EpgRefresher(db_path)
    # Let the proxy serve the player immediately after a restart.
    # Avoid competing with the player while the service is starting. The
    # previous guide remains available during this warm-up period.
    await asyncio.sleep(3600)
    while True:
        try:
            result = await asyncio.to_thread(asyncio.run, refresher.refresh())
            logger.info("epg-refresh mapped=%s unmapped=%s errors=%s",
                        result["mapped"], result["unmapped"], len(result["errors"]))
            if result["errors"]:
                logger.warning("epg-refresh-errors channels=%s",
                               ", ".join(result["errors"][:8]))
        except Exception:
            logger.exception("epg-refresh-failed")
        # Refresh regularly enough to recover from a temporary provider outage.
        await asyncio.sleep(6 * 3600)
