"""Automatic recording rules: official schedules + the TV guide (epg_programs).

Rule kinds:
  epg   record guide entries whose title matches a pattern (first airing = live)
  f1    official F1 calendar (Jolpica/Ergast API): qualifying, sprint, race,
        recorded on a fixed channel (Viaplay F1) at the real session times
  team  a football team's fixtures (ESPN public API, all competitions); the
        channel is the guide entry with team + opponent around kick-off
Schedules are the source of truth for *when*, so reruns never qualify.

A rule says "record every live broadcast whose title matches X, from N minutes
before until M minutes after". Every run (systemd timer, every 30 minutes):

1. find guide entries in the coming days that match an enabled rule;
2. keep only *live* broadcasts: the guide has no live/rerun flag, so the first
   airing of a programme is taken as live and the same programme again within
   RERUN_WINDOW is a rerun (also remembered across runs in autorecord_log);
3. the same broadcast is often on several channels at once (ESPN 2 / FHD / 4K):
   record it once, on the preferred channel (FHD > HD > plain > 4K/UHD > SD);
4. schedule it as a normal recording for the rule's viewer; if the guide time
   moves before it starts, the scheduled recording moves with it;
5. overlapping recordings: the provider allows one stream at a time, so the rule
   with the higher priority is recorded in full and the other is trimmed around
   it (possibly into two parts) or cancelled if nothing useful is left.

Usage:
    python -m app.autorecord run [--dry-run]
    python -m app.autorecord list
    python -m app.autorecord upcoming
    python -m app.autorecord add NAME --include REGEX [--exclude REGEX]
                                 [--before MIN] [--after MIN] [--priority N] [--viewer NAME]
    python -m app.autorecord add-f1 NAME --channel CHANNEL_ID [--before 10 --after 30]
    python -m app.autorecord add-team NAME --espn-team 148 --word PSV [--exclude REGEX]
    python -m app.autorecord enable|disable|delete ID
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

DB_PATH = os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db")
HORIZON = 8 * 24 * 3600          # look this far ahead in the guide
RERUN_WINDOW = 7 * 24 * 3600     # same programme again within this window = rerun
MOVE_TOLERANCE = 6 * 3600        # a guide entry moved by up to this much is the same broadcast

SCHEMA = """
CREATE TABLE IF NOT EXISTS autorecord_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    include_re  TEXT NOT NULL,
    exclude_re  TEXT NOT NULL DEFAULT '',
    before_min  INTEGER NOT NULL DEFAULT 10,
    after_min   INTEGER NOT NULL DEFAULT 20,
    priority    INTEGER NOT NULL DEFAULT 100,     -- lower number wins a conflict
    viewer_id   INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'epg',      -- epg | f1 | team
    params      TEXT NOT NULL DEFAULT '{}'        -- JSON, per kind
);
CREATE TABLE IF NOT EXISTS autorecord_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       INTEGER NOT NULL,
    match_key     TEXT NOT NULL,
    first_air_at  REAL NOT NULL,
    program_id    INTEGER,
    channel_id    INTEGER,
    recording_id  INTEGER,
    state         TEXT NOT NULL,                  -- scheduled | conflict | skipped
    note          TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    UNIQUE(rule_id, match_key, first_air_at)
);
"""

# Words that mark something that is not the live event itself.
NOT_LIVE = r"samenvatting|highlights|hoogtepunten|terugblik|voorbeschouwing|nabeschouwing|herhaling|\(h\)"


@dataclass
class Program:
    id: int
    channel_id: int
    channel_name: str
    start_at: float
    end_at: float
    title: str


@dataclass
class Plan:
    rule: dict
    program: Program
    key: str
    start_at: float
    end_at: float
    title: str


# ---- text helpers ----------------------------------------------------------

def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def clean_title(title: str) -> str:
    """'Eredivisie: PSV - Willem II PSV - Willem II' -> 'Eredivisie: PSV - Willem II'."""
    title = re.sub(r"\s+", " ", title).strip()
    words = title.split(" ")
    for n in range(len(words) // 2, 0, -1):          # drop a repeated tail
        if words[-n:] == words[-2 * n:-n]:
            return " ".join(words[:-n])
    return title


def match_key(title: str) -> str:
    """Programme identity for rerun detection (same key = same event)."""
    t = _fold(clean_title(title))
    t = re.sub(r"\([^)]*\)", " ", t)                 # (live), (h), ...
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def channel_rank(name: str) -> int:
    """Lower is better: FHD > HD > plain > 4K/UHD (heavy) > SD."""
    n = name.upper()
    if "FHD" in n:
        return 0
    if re.search(r"\b(4K|UHD)\b", n):
        return 3
    if re.search(r"\bSD\b", n):
        return 4
    if re.search(r"\bHD\b", n):
        return 1
    return 2


# ---- planning --------------------------------------------------------------

def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(autorecord_rules)")}
    if "kind" not in cols:
        db.execute("ALTER TABLE autorecord_rules ADD COLUMN kind TEXT NOT NULL DEFAULT 'epg'")
    if "params" not in cols:
        db.execute("ALTER TABLE autorecord_rules ADD COLUMN params TEXT NOT NULL DEFAULT '{}'")
    db.commit()
    return db


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "TVProxy-autorecord/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _ts(date: str, clock: str | None) -> float:
    return datetime.fromisoformat((date + "T" + (clock or "00:00:00Z")).replace("Z", "+00:00")).timestamp()


def _local(t: float) -> str:
    return time.strftime("%a %d-%m %H:%M", time.localtime(t))


# ---- schedule sources ------------------------------------------------------

F1_URL = "https://api.jolpi.ca/ergast/f1/current.json"
# minutes each session normally takes (before the rule's after_min margin)
F1_SESSIONS = {"Qualifying": ("Kwalificatie", 70), "Sprint": ("Sprint", 45),
               "SprintQualifying": ("Sprint-kwalificatie", 50),
               "SprintShootout": ("Sprint-kwalificatie", 50)}
F1_RACE_MINUTES = 120


def f1_sessions(data: dict) -> list[dict]:
    """[{key, title, start, minutes}] from a Jolpica/Ergast season document."""
    out = []
    table = data["MRData"]["RaceTable"]
    for race in table["Races"]:
        base = f"f1:{table.get('season', race.get('season', ''))}:{race['round']}"
        name = race["raceName"]
        out.append({"key": f"{base}:race", "title": f"Formule 1: {name} – Race",
                    "start": _ts(race["date"], race.get("time")), "minutes": F1_RACE_MINUTES})
        for field, (label, minutes) in F1_SESSIONS.items():
            if field in race:
                out.append({"key": f"{base}:{field.lower()}", "title": f"Formule 1: {name} – {label}",
                            "start": _ts(race[field]["date"], race[field].get("time")),
                            "minutes": minutes})
    return out


ESPN_SCHEDULE = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/{team}/schedule?fixture=true"
GENERIC_WORDS = {"fc", "sc", "ac", "sv", "cf", "cd", "afc", "club", "de", "the", "real", "sporting",
                 "united", "city", "football", "calcio", "1"}


def team_fixtures(data: dict, team_id: str) -> list[dict]:
    """[{key, title, kickoff, opponent, competition}] from an ESPN team schedule."""
    out = []
    for e in data.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        teams = comp.get("competitors", [])
        opp = next((c for c in teams if str(c.get("team", {}).get("id")) != str(team_id)), None)
        if not opp:
            continue
        status = (comp.get("status") or e.get("status") or {}).get("type", {})
        if status.get("completed"):
            continue
        out.append({"key": f"team:{team_id}:{e['id']}",
                    "kickoff": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp(),
                    "opponent": opp["team"].get("displayName", ""),
                    "competition": (e.get("league") or {}).get("name", "")})
    return out


def opponent_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _fold(name))
    return {w for w in words if len(w) >= 3 and w not in GENERIC_WORDS} or set(words)


def matches(rule: dict, title: str) -> bool:
    t = clean_title(title)
    if not re.search(rule["include_re"], t, re.I):
        return False
    exclude = "|".join(x for x in (rule.get("exclude_re") or "", NOT_LIVE) if x)
    return not re.search(exclude, t, re.I)


def _programs(db: sqlite3.Connection, now: float) -> list[Program]:
    rows = db.execute(
        """SELECT p.id, p.channel_id, COALESCE(c.display_name, '') AS channel_name,
                  p.start_at, p.end_at, p.title
             FROM epg_programs p LEFT JOIN channels c ON c.id = p.channel_id
            WHERE p.end_at > ? AND p.start_at < ? AND COALESCE(c.enabled, 1) = 1
            ORDER BY p.start_at""",
        (now - RERUN_WINDOW, now + HORIZON)).fetchall()
    return [Program(**dict(r)) for r in rows]


def plan_epg(db, rule, programs, now, notes) -> list[Plan]:
    hits = [p for p in programs if matches(rule, p.title)]
    # first airing per programme (from guide history and our own log)
    first: dict[str, float] = {}
    for p in hits:
        k = match_key(p.title)
        first[k] = min(first.get(k, p.start_at), p.start_at)
    for k, t in db.execute("SELECT match_key, first_air_at FROM autorecord_log WHERE rule_id=?",
                           (rule["id"],)):
        if now - t >= RERUN_WINDOW:
            continue
        # if the guide still has this broadcast (possibly moved), the guide time wins
        if any(match_key(p.title) == k and abs(p.start_at - t) <= MOVE_TOLERANCE for p in hits):
            continue
        first[k] = min(first.get(k, t), t)
    best: dict[tuple, Program] = {}
    for p in hits:
        k = match_key(p.title)
        if p.start_at != first[k]:
            if p.end_at > now and p.start_at - first[k] < RERUN_WINDOW:
                notes.append(f"rerun skipped: {clean_title(p.title)} {_local(p.start_at)}")
            continue
        if p.end_at <= now:
            continue
        slot = (k, p.start_at)
        cur = best.get(slot)
        if cur is None or channel_rank(p.channel_name) < channel_rank(cur.channel_name):
            best[slot] = p
    return [Plan(rule, p, k, p.start_at - rule["before_min"] * 60, p.end_at + rule["after_min"] * 60,
                 clean_title(p.title)) for (k, _), p in best.items()]


def plan_f1(db, rule, params, now, notes, fetch) -> list[Plan]:
    channel_id = int(params["channel_id"])
    row = db.execute("SELECT display_name FROM channels WHERE id=?", (channel_id,)).fetchone()
    name = row[0] if row else str(channel_id)
    plans = []
    for s in f1_sessions(fetch(params.get("url", F1_URL))):
        end = s["start"] + s["minutes"] * 60
        if end <= now or s["start"] > now + HORIZON:
            continue
        prog = Program(None, channel_id, name, s["start"], end, s["title"])
        plans.append(Plan(rule, prog, s["key"], s["start"] - rule["before_min"] * 60,
                          end + rule["after_min"] * 60, s["title"]))
    return plans


def plan_team(db, rule, params, programs, now, notes, fetch) -> list[Plan]:
    team_id = str(params["espn_team_id"])
    word = params.get("guide_word", "PSV")
    match_minutes = int(params.get("match_minutes", 115))     # kick-off .. final whistle (incl. break)
    plans = []
    for fx in team_fixtures(fetch(ESPN_SCHEDULE.format(team=team_id)), team_id):
        k0 = fx["kickoff"]
        if k0 + match_minutes * 60 <= now or k0 > now + HORIZON:
            continue
        tokens = opponent_tokens(fx["opponent"])
        cands = [p for p in programs
                 if k0 - 90 * 60 <= p.start_at <= k0 + 15 * 60 and p.end_at > k0
                 and re.search(rf"\b{re.escape(word)}\b", p.title, re.I)
                 and tokens & set(re.findall(r"[a-z0-9]+", _fold(p.title)))
                 and not re.search(NOT_LIVE, p.title, re.I)
                 and not (rule.get("exclude_re") and re.search(rule["exclude_re"], p.title, re.I))]
        title = f"{word} – {fx['opponent']}" + (f" ({fx['competition']})" if fx["competition"] else "")
        if not cands:
            notes.append(f"no channel yet: {title} {_local(k0)} (not in the TV guide)")
            continue
        best = min(cands, key=lambda p: (channel_rank(p.channel_name), abs(p.start_at - k0)))
        start = best.start_at - rule["before_min"] * 60
        end = max(best.end_at, k0 + match_minutes * 60) + rule["after_min"] * 60
        prog = Program(best.id, best.channel_id, best.channel_name, k0, end, clean_title(best.title))
        plans.append(Plan(rule, prog, fx["key"], start, end, clean_title(best.title)))
    return plans


def plan(db: sqlite3.Connection, now: float | None = None,
         fetch=fetch_json) -> tuple[list[Plan], list[str]]:
    """Live broadcasts to record, and notes about what was skipped."""
    now = time.time() if now is None else now
    rules = [dict(r) for r in db.execute("SELECT * FROM autorecord_rules WHERE enabled=1")]
    if not rules:
        return [], []
    programs = _programs(db, now)
    notes: list[str] = []
    plans: list[Plan] = []
    for rule in rules:
        kind = rule.get("kind") or "epg"
        params = json.loads(rule.get("params") or "{}")
        try:
            if kind == "f1":
                plans += plan_f1(db, rule, params, now, notes, fetch)
            elif kind == "team":
                plans += plan_team(db, rule, params, programs, now, notes, fetch)
            else:
                plans += plan_epg(db, rule, programs, now, notes)
        except Exception as exc:          # one broken source must not stop the others
            notes.append(f"rule '{rule['name']}' skipped this run: {type(exc).__name__}: {exc}")
    plans.sort(key=lambda x: (x.start_at, x.rule["priority"]))
    return plans, sorted(set(notes))


SLOT_GAP = 60          # seconds between two recordings so the provider frees the stream
MIN_PART = 10 * 60    # leftover pieces shorter than this are not worth recording


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def free_parts(start: float, end: float, busy: list[dict]) -> list[tuple[float, float]]:
    """The pieces of [start, end) not covered by busy slots (with SLOT_GAP around
    them); pieces shorter than MIN_PART are dropped."""
    parts = [(start, end)]
    for b in busy:
        b0, b1 = b["start_at"] - SLOT_GAP, b["end_at"] + SLOT_GAP
        nxt = []
        for s0, s1 in parts:
            if not _overlaps(s0, s1, b0, b1):
                nxt.append((s0, s1))
                continue
            if s0 < b0:
                nxt.append((s0, b0))
            if b1 < s1:
                nxt.append((b1, s1))
        parts = nxt
    return [(a, b) for a, b in sorted(parts) if b - a >= MIN_PART]


def _short(title: str) -> str:
    """'UEFA Champions League: PSV - Club Brugge' -> 'PSV - Club Brugge'."""
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", title)
    return (t.split(": ", 1)[1] if ": " in t else t)[:40]


def part_title(title: str, i: int, n: int, p0: float, p1: float, trimmed: bool, blocker: str) -> str:
    """Recording title, with a visible label when it is not recorded in full."""
    hhmm = lambda t: time.strftime("%H:%M", time.localtime(t))  # noqa: E731
    if n > 1:
        return f"{title} [deel {i}/{n}: {hhmm(p0)}-{hhmm(p1)}, onderbroken voor {blocker}]"
    if trimmed:
        return f"{title} [gedeeltelijk {hhmm(p0)}-{hhmm(p1)}, voorrang {blocker}]"
    return title


def apply(db: sqlite3.Connection, plans: list[Plan], now: float | None = None,
          dry_run: bool = False) -> list[str]:
    """Turn plans into recordings, highest priority (lowest number) first.

    Only one stream at a time: a lower-priority programme is trimmed around the
    slots already taken (it can become two parts), or cancelled if nothing is
    left. This is recomputed every run, so a higher-priority programme that
    appears later in the guide still pushes an already scheduled one aside.
    Recordings that are already running are never touched.
    """
    now = time.time() if now is None else now
    report: list[str] = []
    auto_ids = {r[0] for r in db.execute(
        "SELECT recording_id FROM autorecord_log WHERE recording_id IS NOT NULL")}
    active = [dict(r) for r in db.execute(
        "SELECT id, title, start_at, end_at, status FROM recordings "
        "WHERE status IN ('scheduled','recording') AND end_at > ?", (now,))]
    # fixed slots: manual recordings and anything already running
    busy = [r for r in active if r["id"] not in auto_ids or r["status"] == "recording"]

    def find_log(rule_id, key, first_air):
        return db.execute(
            "SELECT * FROM autorecord_log WHERE rule_id=? AND match_key=? "
            "AND ABS(first_air_at - ?) <= ? ORDER BY ABS(first_air_at - ?) LIMIT 1",
            (rule_id, key, first_air, MOVE_TOLERANCE, first_air)).fetchone()

    def recording(log):
        if log and log["recording_id"]:
            return db.execute("SELECT * FROM recordings WHERE id=?", (log["recording_id"],)).fetchone()
        return None

    for pl in sorted(plans, key=lambda x: (x.rule["priority"], x.start_at)):
        fmt = time.strftime("%a %d-%m %H:%M", time.localtime(pl.start_at))
        log = find_log(pl.rule["id"], pl.key, pl.program.start_at)
        if log and log["state"] == "skipped":      # user said: not this one
            continue
        rec = recording(log)
        if rec is not None and rec["status"] == "recording":
            continue                                # running: leave it alone (already busy)

        parts = free_parts(pl.start_at, pl.end_at, busy)
        if not parts:
            clash = next((b for b in busy if _overlaps(pl.start_at, pl.end_at, b["start_at"], b["end_at"])), None)
            note = f"overlaps with '{clash['title']}'" if clash else "no free slot"
            if rec is not None and rec["status"] == "scheduled":
                report.append(f"cancelled {pl.title} {fmt}: {note}")
                if not dry_run:
                    db.execute("DELETE FROM recordings WHERE id=?", (rec["id"],))
            elif not log or log["state"] != "conflict":
                report.append(f"CONFLICT  {pl.title} {fmt} ({pl.rule['name']}): {note}")
            if not dry_run:
                if log:
                    db.execute("UPDATE autorecord_log SET state='conflict', recording_id=NULL, note=? "
                               "WHERE id=?", (note, log["id"]))
                else:
                    db.execute("INSERT INTO autorecord_log (rule_id, match_key, first_air_at, program_id, "
                               "channel_id, state, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                               (pl.rule["id"], pl.key, pl.program.start_at, pl.program.id,
                                pl.program.channel_id, "conflict", note, now))
            parts = []

        n = len(parts)
        cover = [b for b in busy if _overlaps(pl.start_at, pl.end_at, b["start_at"], b["end_at"])]
        blocker = _short(max(cover, key=lambda b: min(b["end_at"], pl.end_at) - max(b["start_at"], pl.start_at))
                         ["title"]) if cover else ""
        for i, (p0, p1) in enumerate(parts, 1):
            key = pl.key if i == 1 else f"{pl.key}#{i}"
            trimmed = p0 > pl.start_at + 1 or p1 < pl.end_at - 1
            title = part_title(pl.title, i, n, p0, p1, trimmed, blocker)
            when = (time.strftime("%a %d-%m %H:%M", time.localtime(p0)) + "-"
                    + time.strftime("%H:%M", time.localtime(p1)))
            plog = log if i == 1 else find_log(pl.rule["id"], key, pl.program.start_at)
            prec = rec if i == 1 else recording(plog)
            busy.append({"id": prec["id"] if prec else None, "title": title, "start_at": p0, "end_at": p1})
            if prec is not None and prec["status"] == "scheduled":
                changed = (abs(prec["start_at"] - p0) > 1 or abs(prec["end_at"] - p1) > 1
                           or prec["channel_id"] != pl.program.channel_id or prec["title"] != title[:200])
                if changed:
                    report.append(f"moved     {title} -> {when} on {pl.program.channel_name}"
                                  + (" (trimmed around a higher-priority recording)" if trimmed else ""))
                    if not dry_run:
                        db.execute("UPDATE recordings SET start_at=?, end_at=?, channel_id=?, title=?, "
                                   "updated_at=? WHERE id=?",
                                   (p0, p1, pl.program.channel_id, title[:200], now, prec["id"]))
                        db.execute("UPDATE autorecord_log SET first_air_at=?, program_id=?, channel_id=?, "
                                   "state='scheduled', note=? WHERE id=?",
                                   (pl.program.start_at, pl.program.id, pl.program.channel_id,
                                    "trimmed" if trimmed else "", plog["id"]))
                continue
            report.append(f"scheduled {title} {when} on {pl.program.channel_name} ({pl.rule['name']})"
                          + (" (trimmed around a higher-priority recording)" if trimmed else "")
                          + (f"  [key {pl.key}]" if dry_run else ""))
            if dry_run:
                continue
            rid = db.execute(
                """INSERT INTO recordings (viewer_id, channel_id, title, start_at, end_at, timezone,
                                           status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'Europe/Amsterdam', 'scheduled', ?, ?)""",
                (pl.rule["viewer_id"], pl.program.channel_id, title[:200], p0, p1, now, now)).lastrowid
            busy[-1]["id"] = rid
            if plog:
                db.execute("UPDATE autorecord_log SET recording_id=?, state='scheduled', note=?, "
                           "program_id=?, channel_id=?, first_air_at=? WHERE id=?",
                           (rid, "trimmed" if trimmed else "", pl.program.id, pl.program.channel_id,
                            pl.program.start_at, plog["id"]))
            else:
                db.execute("INSERT INTO autorecord_log (rule_id, match_key, first_air_at, program_id, "
                           "channel_id, recording_id, state, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (pl.rule["id"], key, pl.program.start_at, pl.program.id,
                            pl.program.channel_id, rid, "scheduled", "trimmed" if trimmed else "", now))
        # parts that are no longer needed (e.g. the overlap went away)
        for extra in db.execute("SELECT * FROM autorecord_log WHERE rule_id=? AND match_key LIKE ? "
                                "AND recording_id IS NOT NULL", (pl.rule["id"], pl.key + "#%")).fetchall():
            idx = int(extra["match_key"].rsplit("#", 1)[1])
            if idx > n:
                xrec = recording(extra)
                if xrec is not None and xrec["status"] == "scheduled":
                    report.append(f"cancelled {xrec['title']}: no longer needed")
                    if not dry_run:
                        db.execute("DELETE FROM recordings WHERE id=?", (xrec["id"],))
                        db.execute("UPDATE autorecord_log SET recording_id=NULL, state='conflict', "
                                   "note='merged' WHERE id=?", (extra["id"],))
    if not dry_run:
        db.commit()
    return report


def run(db_path: str = DB_PATH, dry_run: bool = False, now: float | None = None,
        fetch=fetch_json) -> list[str]:
    db = connect(db_path)
    try:
        plans, notes = plan(db, now, fetch)
        return apply(db, plans, now, dry_run) + notes
    finally:
        db.close()


# ---- CLI -------------------------------------------------------------------

def _viewer_id(db: sqlite3.Connection, name: str) -> int:
    row = db.execute("SELECT id FROM viewers WHERE name=? OR CAST(id AS TEXT)=?", (name, name)).fetchone()
    if not row:
        raise SystemExit(f"no viewer {name!r}")
    return row["id"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.autorecord")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--dry-run", action="store_true")
    sub.add_parser("list")
    sub.add_parser("upcoming")
    a = sub.add_parser("add")
    a.add_argument("name"); a.add_argument("--include", required=True)
    a.add_argument("--exclude", default=""); a.add_argument("--before", type=int, default=10)
    a.add_argument("--after", type=int, default=20); a.add_argument("--priority", type=int, default=100)
    a.add_argument("--viewer", default="twan")
    f = sub.add_parser("add-f1", help="F1 sessions from the official calendar")
    f.add_argument("name"); f.add_argument("--channel", type=int, required=True)
    f.add_argument("--before", type=int, default=10); f.add_argument("--after", type=int, default=30)
    f.add_argument("--priority", type=int, default=10); f.add_argument("--viewer", default="twan")
    t = sub.add_parser("add-team", help="a team's fixtures (ESPN) + channel from the guide")
    t.add_argument("name"); t.add_argument("--espn-team", required=True)
    t.add_argument("--word", required=True, help="word that must be in the guide title, e.g. PSV")
    t.add_argument("--exclude", default=""); t.add_argument("--before", type=int, default=10)
    t.add_argument("--after", type=int, default=20); t.add_argument("--priority", type=int, default=20)
    t.add_argument("--viewer", default="twan")
    k = sub.add_parser("skip", help="never record this programme (see keys with run --dry-run)")
    k.add_argument("rule_id", type=int); k.add_argument("key")
    k.add_argument("--note", default="skipped by user")
    for c in ("enable", "disable", "delete"):
        sub.add_parser(c).add_argument("id", type=int)
    args = ap.parse_args(argv)

    db = connect()
    try:
        if args.cmd == "run":
            plans, notes = plan(db)
            out = apply(db, plans, dry_run=args.dry_run) + notes
            print("\n".join(out) if out else "nothing new")
        elif args.cmd == "list":
            for rr in db.execute("SELECT * FROM autorecord_rules ORDER BY priority, id"):
                what = (f"params {rr['params']}" if rr["kind"] != "epg"
                        else f"include /{rr['include_re']}/")
                print(f"{rr['id']:3} {'on ' if rr['enabled'] else 'off'} prio {rr['priority']:3} "
                      f"-{rr['before_min']}/+{rr['after_min']} min  [{rr['kind']}] {rr['name']}: "
                      f"{what} exclude /{rr['exclude_re']}/")
        elif args.cmd == "upcoming":
            for row in db.execute(
                    """SELECT l.state, l.note, r.name, rec.title, rec.start_at, rec.end_at, rec.status,
                              c.display_name, l.first_air_at
                         FROM autorecord_log l JOIN autorecord_rules r ON r.id = l.rule_id
                         LEFT JOIN recordings rec ON rec.id = l.recording_id
                         LEFT JOIN channels c ON c.id = COALESCE(rec.channel_id, l.channel_id)
                        WHERE COALESCE(rec.end_at, l.first_air_at + 4*3600) > ?
                        ORDER BY COALESCE(rec.start_at, l.first_air_at)""", (time.time(),)):
                when = row["start_at"] or row["first_air_at"]
                end = time.strftime("%H:%M", time.localtime(row["end_at"])) if row["end_at"] else "?"
                print(f"{time.strftime('%a %d-%m %H:%M', time.localtime(when))}-{end}  "
                      f"{(row['status'] or row['state']):9} {row['title'] or ''}  "
                      f"[{row['display_name'] or ''}] ({row['name']}) {row['note']}")
        elif args.cmd == "skip":
            plans, _ = plan(db)
            pl = next((x for x in plans if x.rule["id"] == args.rule_id and x.key == args.key), None)
            first = pl.program.start_at if pl else time.time()
            row = db.execute("SELECT id, recording_id FROM autorecord_log WHERE rule_id=? AND match_key=?",
                             (args.rule_id, args.key)).fetchone()
            if row:
                if row["recording_id"]:
                    db.execute("DELETE FROM recordings WHERE id=? AND status='scheduled'", (row["recording_id"],))
                db.execute("UPDATE autorecord_log SET state='skipped', recording_id=NULL, note=? WHERE id=?",
                           (args.note, row["id"]))
            else:
                db.execute("INSERT INTO autorecord_log (rule_id, match_key, first_air_at, state, note, created_at) "
                           "VALUES (?,?,?,?,?,?)", (args.rule_id, args.key, first, "skipped", args.note, time.time()))
            db.commit(); print(f"skipped {args.key}" + (f" ({pl.title})" if pl else ""))
        elif args.cmd in ("add-f1", "add-team"):
            if args.cmd == "add-f1":
                kind, params, inc, exc = "f1", {"channel_id": args.channel}, "", ""
            else:
                kind, params, inc, exc = "team", {"espn_team_id": args.espn_team, "guide_word": args.word}, "", args.exclude
            db.execute("INSERT INTO autorecord_rules (name, include_re, exclude_re, before_min, after_min, "
                       "priority, viewer_id, created_at, kind, params) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (args.name, inc, exc, args.before, args.after, args.priority,
                        _viewer_id(db, args.viewer), time.time(), kind, json.dumps(params)))
            db.commit(); print("rule added")
        elif args.cmd == "add":
            re.compile(args.include); re.compile(args.exclude or "x")
            db.execute("INSERT INTO autorecord_rules (name, include_re, exclude_re, before_min, after_min, "
                       "priority, viewer_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (args.name, args.include, args.exclude, args.before, args.after,
                        args.priority, _viewer_id(db, args.viewer), time.time()))
            db.commit(); print("rule added")
        else:
            if args.cmd == "delete":
                db.execute("DELETE FROM autorecord_rules WHERE id=?", (args.id,))
            else:
                db.execute("UPDATE autorecord_rules SET enabled=? WHERE id=?",
                           (1 if args.cmd == "enable" else 0, args.id))
            db.commit(); print("ok")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
