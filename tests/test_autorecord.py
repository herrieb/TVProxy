"""Tests for app.autorecord, using real guide titles seen on 2026-11-01."""

import sqlite3

import pytest

from app import autorecord as ar

PSV = dict(name="PSV live", include_re=r"(?=.*\bPSV\b)(?=.*\S\s-\s\S)",
           exclude_re=r"\bJong PSV\b|\bVrouwen\b|\bWomen\b|\bO1[79]\b",
           before_min=10, after_min=20, priority=20)
F1 = dict(name="Formule 1 live", include_re=r"^Formule 1\b.*\b(Race|Sprint|Kwalificatie)\b",
          exclude_re=r"faster food|vrooooom|porsche|training",
          before_min=10, after_min=30, priority=10)
H = 3600
NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "db.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE viewers (id INTEGER PRIMARY KEY, name TEXT);
      CREATE TABLE channels (id INTEGER PRIMARY KEY, display_name TEXT, enabled INTEGER DEFAULT 1);
      CREATE TABLE recordings (id INTEGER PRIMARY KEY AUTOINCREMENT, viewer_id INTEGER, channel_id INTEGER,
        title TEXT, start_at REAL, end_at REAL, timezone TEXT, status TEXT, local_path TEXT DEFAULT '',
        remote_path TEXT DEFAULT '', error TEXT DEFAULT '', created_at REAL, updated_at REAL);
      CREATE TABLE epg_programs (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,
        guide_channel_id TEXT DEFAULT '', start_at REAL, end_at REAL, title TEXT,
        description TEXT DEFAULT '', updated_at REAL DEFAULT 0);
    """)
    c.execute("INSERT INTO viewers VALUES (3, 'twan')")
    c.executemany("INSERT INTO channels (id, display_name) VALUES (?, ?)", [
        (3577, "NL: ESPN 2 4K"), (3578, "NL: ESPN 2 FHD"), (3579, "NL: ESPN 2"),
        (3547, "NL: Viaplay TV"), (3548, "NL: Viaplay +TV")])
    c.commit()
    c.close()
    db = ar.connect(str(path))
    yield db
    db.close()


def add_rule(db, rule):
    db.execute("INSERT INTO autorecord_rules (name, include_re, exclude_re, before_min, after_min, "
               "priority, viewer_id, created_at) VALUES (?,?,?,?,?,?,3,0)",
               (rule["name"], rule["include_re"], rule["exclude_re"], rule["before_min"],
                rule["after_min"], rule["priority"]))
    db.commit()


def prog(db, ch, start, end, title):
    db.execute("INSERT INTO epg_programs (channel_id, start_at, end_at, title) VALUES (?,?,?,?)",
               (ch, start, end, title))
    db.commit()


def recordings(db):
    return [dict(r) for r in db.execute("SELECT * FROM recordings ORDER BY start_at")]


def test_clean_title_and_key():
    assert ar.clean_title("Eredivisie: PSV - Willem II PSV - Willem II") == "Eredivisie: PSV - Willem II"
    assert ar.match_key("Eredivisie: PSV - Willem II PSV - Willem II") == ar.match_key("Eredivisie: PSV - Willem II")
    assert ar.match_key("Formule 1: GP van Brazilië Race") == "formule 1 gp van brazilie race"


def test_channel_rank_prefers_fhd():
    names = ["NL: ESPN 2 4K", "NL: ESPN 2", "NL: ESPN 2 FHD", "NL: ESPN 2 HD", "NL: ESPN 2 SD"]
    assert sorted(names, key=ar.channel_rank)[0] == "NL: ESPN 2 FHD"


def test_psv_live_only_once_on_fhd_with_margins(db):
    add_rule(db, PSV)
    live = NOW + 2 * H
    for ch in (3577, 3578, 3579):                       # same broadcast on three channels
        prog(db, ch, live, live + 2 * H, "Eredivisie: PSV - Willem II PSV - Willem II")
        prog(db, ch, live + 5.5 * H, live + 5.7 * H, "Eredivisie samenvatting PSV - Willem II (samenvatting)")
        prog(db, ch, live + 6.3 * H, live + 8 * H, "Eredivisie: PSV - Willem II")      # night rerun
    prog(db, 3578, live + 30 * H, live + 31 * H, "Voetbalpraat: PSV na de zege")       # no match shape
    report = ar.run(db_path=db.execute("PRAGMA database_list").fetchone()[2], now=NOW)
    recs = recordings(db)
    assert len(recs) == 1
    r = recs[0]
    assert r["channel_id"] == 3578                     # FHD
    assert r["title"] == "Eredivisie: PSV - Willem II"
    assert r["start_at"] == live - 10 * 60 and r["end_at"] == live + 2 * H + 20 * 60
    assert r["viewer_id"] == 3 and r["status"] == "scheduled"
    assert any(line.startswith("rerun skipped") for line in report)


def test_rerun_after_live_has_passed_is_still_a_rerun(db):
    add_rule(db, PSV)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    live = NOW + 1 * H
    prog(db, 3578, live, live + 2 * H, "Eredivisie: PSV - Willem II")
    prog(db, 3578, live + 6 * H, live + 8 * H, "Eredivisie: PSV - Willem II")
    ar.run(db_path=path, now=NOW)
    db.execute("DELETE FROM epg_programs WHERE start_at = ?", (live,))   # guide forgot the live one
    db.commit()
    ar.run(db_path=path, now=live + 4 * H)
    assert len(recordings(db)) == 1


def test_formula1_race_and_qualifying_not_magazines(db):
    add_rule(db, F1)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 3 * H
    prog(db, 3548, t, t + 2.75 * H, "Formule 1: GP van Mexico Kwalificatie")
    prog(db, 3548, t + 24 * H, t + 27.75 * H, "Formule 1: GP van Mexico Race")
    prog(db, 3547, t + 48 * H, t + 48.5 * H, "Formule 1 - Faster food Grand Prix van Mexico")
    prog(db, 3548, t + 30 * H, t + 31 * H, "Vrooooom Grand Prix van Mexico")
    prog(db, 3547, t + 70 * H, t + 72.5 * H, "Formule 1: GP van Brazilië Race")
    prog(db, 3548, t + 70 * H, t + 72 * H, "Formule 1: GP van Brazilië Race")      # same, other channel
    ar.run(db_path=path, now=NOW)
    titles = [r["title"] for r in recordings(db)]
    assert titles == ["Formule 1: GP van Mexico Kwalificatie", "Formule 1: GP van Mexico Race",
                      "Formule 1: GP van Brazilië Race"]
    race = recordings(db)[1]
    assert race["end_at"] == t + 27.75 * H + 30 * 60   # +30 min for F1


def test_conflict_higher_priority_wins(db):
    add_rule(db, PSV); add_rule(db, F1)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 5 * H
    prog(db, 3578, t, t + 2 * H, "Eredivisie: PSV - Ajax")
    prog(db, 3548, t + 1 * H, t + 3 * H, "Formule 1: GP van Las Vegas Race")
    report = ar.run(db_path=path, now=NOW)
    titles = [r["title"] for r in recordings(db)]
    assert titles == ["Formule 1: GP van Las Vegas Race"]
    assert any(line.startswith("CONFLICT  Eredivisie: PSV - Ajax") for line in report)
    state = db.execute("SELECT state FROM autorecord_log WHERE match_key LIKE '%ajax%'").fetchone()[0]
    assert state == "conflict"


def test_run_is_idempotent_and_follows_time_changes(db):
    add_rule(db, PSV)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 10 * H
    prog(db, 3578, t, t + 2 * H, "UEFA Champions League: PSV Eindhoven - Sjachtar Donetsk")
    ar.run(db_path=path, now=NOW)
    assert ar.run(db_path=path, now=NOW + 60) == []        # nothing new the second time
    assert len(recordings(db)) == 1
    db.execute("UPDATE epg_programs SET start_at=start_at+900, end_at=end_at+900")   # kick-off moved
    db.commit()
    report = ar.run(db_path=path, now=NOW + 120)
    r = recordings(db)[0]
    assert r["start_at"] == t + 900 - 600 and r["end_at"] == t + 2 * H + 900 + 1200
    assert any(line.startswith("moved") for line in report)


def test_excludes_jong_psv_and_vrouwen(db):
    add_rule(db, PSV)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 4 * H
    prog(db, 3578, t, t + 2 * H, "Keuken Kampioen Divisie: Jong PSV - Jong Ajax")
    prog(db, 3578, t + 24 * H, t + 26 * H, "Eredivisie Vrouwen: PSV - FC Twente")
    ar.run(db_path=path, now=NOW)
    assert recordings(db) == []


def test_dry_run_changes_nothing(db):
    add_rule(db, PSV)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    prog(db, 3578, NOW + H, NOW + 3 * H, "Eredivisie: PSV - Willem II")
    report = ar.run(db_path=path, now=NOW, dry_run=True)
    assert report and report[0].startswith("scheduled")
    assert recordings(db) == []


# ---- schedule-based rules -----------------------------------------------------

import json as _json
from datetime import datetime, timezone


def iso(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.strftime("%Y-%m-%d"), d.strftime("%H:%M:%SZ")


def add_kind_rule(db, name, kind, params, before, after, priority, exclude=""):
    db.execute("INSERT INTO autorecord_rules (name, include_re, exclude_re, before_min, after_min, priority, "
               "viewer_id, created_at, kind, params) VALUES (?, '', ?, ?, ?, ?, 3, 0, ?, ?)",
               (name, exclude, before, after, priority, kind, _json.dumps(params)))
    db.commit()


def jolpica(races):
    return {"MRData": {"RaceTable": {"season": "2026", "Races": races}}}


def f1_race(rnd, name, race_ts, quali_ts, sprint_ts=None):
    d, t = iso(race_ts); qd, qt = iso(quali_ts)
    r = {"round": str(rnd), "raceName": name, "date": d, "time": t, "Qualifying": {"date": qd, "time": qt}}
    if sprint_ts:
        sd, st = iso(sprint_ts); r["Sprint"] = {"date": sd, "time": st}
    return r


def test_f1_calendar_records_real_sessions_on_viaplay_f1(db):
    db.execute("INSERT INTO channels (id, display_name) VALUES (3546, 'NL: Viaplay F1 FHD ( Ziggo )')")
    add_kind_rule(db, "Formule 1 live", "f1", {"channel_id": 3546}, 10, 30, 10)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    race, quali = NOW + 30 * H, NOW + 6 * H
    old = NOW - 20 * H                                   # last weekend: already over
    doc = jolpica([f1_race(19, "Mexico City Grand Prix", old, old - 24 * H),
                   f1_race(20, "Brazilian Grand Prix", race, quali, sprint_ts=NOW + 26 * H)])
    # a guide rerun on another channel must not matter
    prog(db, 3548, NOW + 50 * H, NOW + 52 * H, "Formule 1: GP van Brazilië Race")
    ar.run(db_path=path, now=NOW, fetch=lambda url: doc)
    recs = recordings(db)
    assert [r["title"] for r in recs] == ["Formule 1: Brazilian Grand Prix – Kwalificatie",
                                          "Formule 1: Brazilian Grand Prix – Sprint",
                                          "Formule 1: Brazilian Grand Prix – Race"]
    assert {r["channel_id"] for r in recs} == {3546}
    race_rec = recs[2]
    assert race_rec["start_at"] == race - 10 * 60
    assert race_rec["end_at"] == race + 120 * 60 + 30 * 60
    # second run: nothing new; calendar moves the race 1 h -> recording moves
    assert ar.run(db_path=path, now=NOW + 60, fetch=lambda url: doc) == []
    doc2 = jolpica([f1_race(20, "Brazilian Grand Prix", race + H, quali, sprint_ts=NOW + 26 * H)])
    report = ar.run(db_path=path, now=NOW + 120, fetch=lambda url: doc2)
    assert any(line.startswith("moved") for line in report)
    assert recordings(db)[2]["start_at"] == race + H - 10 * 60


def espn(events):
    return {"events": events}


def fixture(eid, kickoff, opponent, home=True, competition="UEFA Champions League", completed=False):
    psv = {"team": {"id": "148", "displayName": "PSV Eindhoven"}, "homeAway": "home" if home else "away"}
    opp = {"team": {"id": "999", "displayName": opponent}, "homeAway": "away" if home else "home"}
    return {"id": eid, "date": datetime.fromtimestamp(kickoff, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "name": f"{opponent} at PSV Eindhoven", "league": {"name": competition},
            "competitions": [{"competitors": [psv, opp], "status": {"type": {"completed": completed}}}]}


def test_team_fixture_uses_live_broadcast_not_reruns(db):
    add_kind_rule(db, "PSV live", "team", {"espn_team_id": "148", "guide_word": "PSV"}, 10, 20, 20,
                  exclude=r"\bJong PSV\b|\bVrouwen\b")
    path = db.execute("PRAGMA database_list").fetchone()[2]
    ko = NOW + 20 * H                                     # Wed 21:00
    # live on ESPN 3 FHD (now mapped), studio from 20:30; reruns on Ziggo Sport later
    db.execute("INSERT INTO channels (id, display_name) VALUES (3600, 'NL: ESPN 3 FHD'), (3620, 'NL: Ziggo Sport FHD')")
    prog(db, 3600, ko - 30 * 60, ko + 2 * H, "Live UEFA Champions League: PSV - Club Brugge")
    prog(db, 3620, ko + 5 * H, ko + 7 * H, "UEFA Champions League: PSV - Club Brugge")     # Thu 02:00
    prog(db, 3620, ko + 24 * H, ko + 26 * H, "UEFA Champions League: PSV - Club Brugge")   # Thu 21:00
    prog(db, 3600, ko + 4 * H, ko + 4.25 * H, "Voetbal samenvatting PSV - Club Brugge")
    doc = espn([fixture("401", ko, "Club Brugge"),
                fixture("400", NOW - 30 * H, "Willem II", competition="Eredivisie", completed=True)])
    ar.run(db_path=path, now=NOW, fetch=lambda url: doc)
    recs = recordings(db)
    assert len(recs) == 1
    r = recs[0]
    assert r["channel_id"] == 3600
    assert r["start_at"] == ko - 30 * 60 - 10 * 60        # broadcast start -10 min
    assert r["end_at"] == ko + 2 * H + 20 * 60            # broadcast end +20 min


def test_team_fixture_without_channel_is_reported_and_picked_up_later(db):
    add_kind_rule(db, "PSV live", "team", {"espn_team_id": "148", "guide_word": "PSV"}, 10, 20, 20)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    ko = NOW + 50 * H
    doc = espn([fixture("402", ko, "NEC Nijmegen", home=False, competition="Eredivisie")])
    report = ar.run(db_path=path, now=NOW, fetch=lambda url: doc)
    assert recordings(db) == []
    assert any(line.startswith("no channel yet: PSV – NEC Nijmegen") for line in report)
    prog(db, 3578, ko - 15 * 60, ko + 1.9 * H, "Eredivisie: NEC - PSV")    # guide filled in later
    ar.run(db_path=path, now=NOW + H, fetch=lambda url: doc)
    recs = recordings(db)
    assert len(recs) == 1 and recs[0]["channel_id"] == 3578
    assert recs[0]["end_at"] == ko + 115 * 60 + 20 * 60   # at least kick-off + 115 min, +20


def test_broken_source_does_not_stop_other_rules(db):
    add_kind_rule(db, "PSV live", "team", {"espn_team_id": "148", "guide_word": "PSV"}, 10, 20, 20)
    add_rule(db, F1 | {"name": "F1 guide"})
    path = db.execute("PRAGMA database_list").fetchone()[2]
    prog(db, 3548, NOW + 3 * H, NOW + 5 * H, "Formule 1: GP van Mexico Race")
    def broken(url):
        raise OSError("network down")
    report = ar.run(db_path=path, now=NOW, fetch=broken)
    assert len(recordings(db)) == 1
    assert any("skipped this run: OSError" in line for line in report)


def test_opponent_tokens():
    assert ar.opponent_tokens("Club Brugge") == {"brugge"}
    assert "nec" in ar.opponent_tokens("NEC Nijmegen")
    assert ar.opponent_tokens("Atlético Madrid") == {"atletico", "madrid"}



def test_skipped_programme_is_never_scheduled(db):
    db.execute("INSERT INTO channels (id, display_name) VALUES (3546, 'NL: Viaplay F1 FHD ( Ziggo )')")
    add_kind_rule(db, "Formule 1 live", "f1", {"channel_id": 3546}, 10, 30, 10)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    race = NOW - 0.5 * H                                  # live right now
    doc = jolpica([f1_race(20, "Mexico City Grand Prix", race, race - 24 * H)])
    db.execute("INSERT INTO autorecord_log (rule_id, match_key, first_air_at, state, note, created_at) "
               "VALUES (1, 'f1:2026:20:race', ?, 'skipped', 'user', 0)", (race,))
    db.commit()
    assert ar.run(db_path=path, now=NOW, fetch=lambda url: doc) == []
    assert recordings(db) == []
