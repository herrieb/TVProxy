"""Give channels without a logo one: channel logo > brand logo > category icon.

    python -m app.logos [--viewer twan] [--apply]

1. Channel and brand logos come from the open tv-logo/tv-logos collection on
   GitHub (Dutch, Turkish, British, international, European folders). The name
   is cleaned ("NL: NPO 1 FHD" -> "npo 1") and matched on its compact form, so
   "NPO 1 FHD", "NPO 1HD" and "NPO 1" all get npo-1-nl.png. If there is no exact
   logo, trailing words are dropped until one matches ("ESPN Play 12" ->
   "ESPN Play" -> "ESPN").
2. Otherwise a category icon (made by deploy/make_icons.py, served by the media
   service at <public_url>/media/icons/<name>.png).
Existing logos are never overwritten. Without --apply nothing is written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request

DB_PATH = os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db")
REPO_API = "https://api.github.com/repos/tv-logo/tv-logos/contents/countries/{folder}"
RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/{folder}/{file}"
# folders in order of preference per channel-name prefix
FOLDERS = {"nl": ["netherlands", "belgium", "international", "world-europe", "united-kingdom"],
           "tr": ["turkey", "international/beinsports/old/stacked", "international", "world-europe",
                  "netherlands"],
           "uk": ["united-kingdom", "international", "world-europe"],
           "": ["netherlands", "turkey", "united-kingdom", "international", "international/beinsports/old/stacked",
                "world-europe"]}
QUALITY = r"\b(fhd|uhd|hd|sd|4k|8k|hevc|h265|h264|1080p|720p|p50|50fps|raw|backup|vip|plus\+)\b"
# brands whose logo has a different file name than the cleaned channel name
ALIASES = {"ziggosport1": "ziggosport", "espn1": "espn", "eurosport": "eurosport1",
           "history": "historychannel", "natgeowild": "nationalgeographicwild",
           "natgeo": "nationalgeographic", "id": "idinvestigationdiscovery",
           "discoveryid": "idinvestigationdiscovery", "discoverscience": "discoveryscience",
           "trttabiispor": "trtspor", "beinsporthaber": "beinsportshaber",
           "beinsports": "beinsports4k", "trt": "trt4k", "formula1tv": "f1tv", "f1": "f1tv",
           "100tv": "100pnltv", "djazztv": "stingraydjazz", "djazz": "stingraydjazz",
           "kijk538": "tv538", "538": "tv538", "out": "outtv", "fashion": "fashiontv",
           "eentertainement": "eentertainment", "eentertainment": "eentertainment",
           "flevoland": "omroepflevoland", "skysports": "skysportsmainevent",
           "beinconnect": "beinsports4k"}
# brand spellings that differ only in a word: applied to the cleaned name first
NAME_FIXES = [(r"^sky sport\b", "sky sports"), (r"^bein sport\b(?!s)", "bein sports"),
              (r"^bein sports max (\d+)\b", r"bein sports \1 max")]
# category (channels.description) -> icon file, when no real logo is found
BRAND_ICONS = [(r"Prime Amazon|Prime Video|Amazon Prime", "prime-video"),
               (r"ClipMyHorse", "clipmyhorse"), (r"\bExxen", "exxen")]
CATEGORY_ICONS = [
    (r"Movies Club|Films en Series|Movies", "films"),
    (r"Kinderen|Kids", "kids"),
    (r"Muziek|Music", "music"),
    (r"Documentaires|Lifestyle|Docu", "docu"),
    (r"Regionaal", "regional"),
    (r"Sport|ESPN|DAZN|Viaplay|ClipMyHorse|Spor", "sport"),
    (r"Buitenland|Algemeen|Türkiye|Netherland", "tv"),
]
SEPARATOR_ICON = "section"


def compact(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("+", "plus").replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def clean_name(name: str) -> tuple[str, str]:
    """('nl', 'npo 1') from 'NL: NPO 1 FHD'."""
    m = re.match(r"^\s*([A-Za-z]{2,3})\s*[:|]\s*(.*)$", name)
    prefix, rest = (m.group(1).lower(), m.group(2)) if m else ("", name)
    prefix = {"gb": "uk"}.get(prefix, prefix)
    rest = re.sub(r"\(.*?\)|\[.*?\]", " ", rest)
    rest = re.sub(r"(?<=\d)(hd|fhd|uhd)\b", " ", rest, flags=re.I)      # "NPO 1HD" -> "NPO 1"
    rest = re.sub(QUALITY, " ", rest, flags=re.I)
    rest = re.sub(r"[^\w+&' ]+", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip().lower()
    for pattern, repl in NAME_FIXES:
        rest = re.sub(pattern, repl, rest)
    return prefix, rest


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "tvproxy-logos/1.0",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def load_index(fetch=fetch_json) -> dict[str, dict[str, str]]:
    """{folder: {compact name: raw url}} from the tv-logos repository."""
    folders = sorted({f for fl in FOLDERS.values() for f in fl})
    index: dict[str, dict[str, str]] = {}
    for folder in folders:
        try:
            listing = fetch(REPO_API.format(folder=folder))
        except Exception as exc:                       # one folder missing must not stop the rest
            print(f"warning: {folder}: {type(exc).__name__}", file=sys.stderr)
            continue
        entries = {}
        for item in listing if isinstance(listing, list) else []:
            name = item.get("name", "")
            if not name.endswith(".png"):
                continue
            stem = re.sub(r"-(nl|tr|uk|int|eu|be|us|de|fr)$", "", name[:-4])
            entries.setdefault(compact(stem), RAW.format(folder=folder, file=name))
        index[folder] = entries
    return index


def find_logo(name: str, index: dict[str, dict[str, str]]) -> str | None:
    prefix, cleaned = clean_name(name)
    folders = FOLDERS.get(prefix, FOLDERS[""])
    words = cleaned.split()
    while words:                                       # full name, then drop trailing words
        key = compact(" ".join(words))
        key = ALIASES.get(key, key)
        for folder in folders:
            url = index.get(folder, {}).get(key)
            if url:
                return url
        words = words[:-1]
        if len(words) == 1 and len(compact(words[0])) <= 2:   # do not match on "nl", "tv", ...
            break
    return None


def category_icon(name: str, description: str, base: str) -> str:
    if name.strip().startswith("="):
        return f"{base}/{SEPARATOR_ICON}.png"
    for pattern, icon in BRAND_ICONS:
        if re.search(pattern, name, re.I):
            return f"{base}/{icon}.png"
    for pattern, icon in CATEGORY_ICONS:
        if re.search(pattern, description or "", re.I):
            return f"{base}/{icon}.png"
    return f"{base}/tv.png"


def plan(db: sqlite3.Connection, viewer: str, index, icon_base: str) -> list[tuple[int, str, str, str]]:
    """[(channel id, name, new logo url, kind)] for the viewer's channels without a logo."""
    row = db.execute("SELECT allowed_channels FROM viewers WHERE name=? OR CAST(id AS TEXT)=?",
                     (viewer, viewer)).fetchone()
    if not row:
        raise SystemExit(f"no viewer {viewer!r}")
    ids = json.loads(row[0] or "[]")
    out = []
    for cid, name, desc, logo in db.execute(
            "SELECT id, display_name, description, logo_url FROM channels "
            "WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id", (json.dumps(ids),)):
        if logo and re.match(r"https?://", logo):
            continue
        url = None if name.strip().startswith("=") else find_logo(name, index)
        if url:
            out.append((cid, name, url, "logo"))
        else:
            out.append((cid, name, category_icon(name, desc, icon_base), "category"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.logos")
    ap.add_argument("--viewer", default="twan")
    ap.add_argument("--icon-base", default="https://tv.berrie.uk/media/icons")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    db = sqlite3.connect(DB_PATH if args.apply else f"file:{DB_PATH}?immutable=1",
                         uri=not args.apply, timeout=10)
    try:
        index = load_index()
        print("logo index:", {k: len(v) for k, v in index.items()})
        changes = plan(db, args.viewer, index, args.icon_base)
        logos = [c for c in changes if c[3] == "logo"]
        cats = [c for c in changes if c[3] == "category"]
        print(f"{len(changes)} channels without logo: {len(logos)} real logos, {len(cats)} category icons")
        for cid, name, url, kind in changes:
            print(f"  {kind:8} {name[:45]:45} {url.rsplit('/', 1)[-1]}")
        if args.apply and changes:
            # also replaces junk like "[" left by an import, never a real http(s) logo
            db.executemany("UPDATE channels SET logo_url=? WHERE id=? "
                           "AND (logo_url IS NULL OR logo_url NOT LIKE 'http%')",
                           [(url, cid) for cid, _, url, _ in changes])
            db.commit()
            print(f"saved {len(changes)} logos")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
