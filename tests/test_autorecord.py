"""Tests for app.autorecord, using real guide titles seen on 2026-11-01."""

import sqlite3
import time

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


def test_overlap_lower_priority_is_trimmed_not_dropped(db):
    add_rule(db, PSV); add_rule(db, F1)                 # in this fixture F1 has priority 10
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 5 * H
    prog(db, 3578, t, t + 2 * H, "Eredivisie: PSV - Ajax")
    prog(db, 3548, t + 1 * H, t + 3 * H, "Formule 1: GP van Las Vegas Race")
    ar.run(db_path=path, now=NOW)
    recs = recordings(db)
    f1 = next(r for r in recs if r["title"].startswith("Formule 1"))
    psv = next(r for r in recs if "PSV" in r["title"])
    assert (f1["start_at"], f1["end_at"]) == (t + H - 600, t + 3 * H + 1800)      # in full
    assert psv["start_at"] == t - 600 and psv["end_at"] == f1["start_at"] - 60   # trimmed before F1
    assert not _overlaps(psv, f1)


def _overlaps(a, b):
    return a["start_at"] < b["end_at"] and b["start_at"] < a["end_at"]


PSV_LEAD = PSV | {"priority": 10}
F1_SECOND = F1 | {"priority": 20}


def test_psv_in_full_then_f1_after_the_match(db):
    """The user's rule: PSV always leads; F1 starts after the PSV recording."""
    add_rule(db, PSV_LEAD); add_rule(db, F1_SECOND)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 5 * H                                         # PSV 16:45-18:40, F1 race 18:00-20:00
    prog(db, 3578, t, t + 1.9 * H, "Eredivisie: SC Cambuur - PSV")
    prog(db, 3548, t + 1.25 * H, t + 3.25 * H, "Formule 1: GP van Brazilië Race")
    ar.run(db_path=path, now=NOW)
    psv, f1 = recordings(db)
    assert (psv["start_at"], psv["end_at"]) == (t - 600, t + 1.9 * H + 1200)      # full, -10/+20
    assert f1["start_at"] == psv["end_at"] + 60                                    # right after PSV
    assert f1["end_at"] == t + 3.25 * H + 1800                                     # F1 end +30 kept
    hhmm = lambda x: time.strftime("%H:%M", time.localtime(x))
    assert f1["title"] == (f"Formule 1: GP van Brazilië Race [gedeeltelijk {hhmm(f1['start_at'])}-"
                           f"{hhmm(f1['end_at'])}, voorrang SC Cambuur - PSV]")
    assert psv["title"] == "Eredivisie: SC Cambuur - PSV"                          # full: no label


def test_f1_split_around_a_psv_match_in_the_middle(db):
    add_rule(db, PSV_LEAD); add_rule(db, F1_SECOND)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 5 * H
    prog(db, 3548, t, t + 4 * H, "Formule 1: GP van Japan Race")                   # long F1 window
    prog(db, 3578, t + 1 * H, t + 2.5 * H, "KNVB Beker: PSV - Vitesse")
    ar.run(db_path=path, now=NOW)
    recs = recordings(db)
    titles = [r["title"] for r in recs]
    assert titles[1] == "KNVB Beker: PSV - Vitesse"
    assert titles[0].startswith("Formule 1: GP van Japan Race [deel 1/2: ")
    assert titles[0].endswith(", onderbroken voor PSV - Vitesse]")
    assert titles[2].startswith("Formule 1: GP van Japan Race [deel 2/2: ")
    for a, b in zip(recs, recs[1:]):
        assert a["end_at"] + 60 <= b["start_at"]                                   # never two at once


def test_f1_fully_inside_psv_is_a_conflict(db):
    add_rule(db, PSV_LEAD); add_rule(db, F1_SECOND | {"before_min": 0, "after_min": 0})
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 5 * H
    prog(db, 3578, t, t + 3 * H, "Eredivisie: PSV - Feyenoord")
    prog(db, 3548, t + H, t + 1.5 * H, "Formule 1: GP van Monaco Kwalificatie")
    report = ar.run(db_path=path, now=NOW)
    assert [r["title"] for r in recordings(db)] == ["Eredivisie: PSV - Feyenoord"]
    assert any(line.startswith("CONFLICT  Formule 1: GP van Monaco Kwalificatie") for line in report)


def test_psv_appearing_later_trims_already_scheduled_f1(db):
    add_rule(db, PSV_LEAD); add_rule(db, F1_SECOND)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 50 * H
    prog(db, 3548, t + 1.25 * H, t + 3.25 * H, "Formule 1: GP van Brazilië Race")
    ar.run(db_path=path, now=NOW)
    f1 = recordings(db)[0]
    assert f1["start_at"] == t + 1.25 * H - 600                                    # nothing in the way yet
    prog(db, 3578, t, t + 1.9 * H, "Eredivisie: SC Cambuur - PSV")                 # guide fills in later
    report = ar.run(db_path=path, now=NOW + 3 * H)
    psv, f1 = recordings(db)
    assert psv["title"] == "Eredivisie: SC Cambuur - PSV"
    assert f1["start_at"] == psv["end_at"] + 60
    assert any(line.startswith("moved     Formule 1") and "trimmed" in line for line in report)
    assert len(recordings(db)) == 2                                                # moved, not duplicated


def test_split_parts_merge_back_when_overlap_disappears(db):
    add_rule(db, PSV_LEAD); add_rule(db, F1_SECOND)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    t = NOW + 5 * H
    prog(db, 3548, t, t + 4 * H, "Formule 1: GP van Japan Race")
    prog(db, 3578, t + 1 * H, t + 2.5 * H, "KNVB Beker: PSV - Vitesse")
    ar.run(db_path=path, now=NOW)
    assert len(recordings(db)) == 3
    db.execute("DELETE FROM epg_programs WHERE title LIKE '%Vitesse%'")             # match moved away
    db.commit()
    report = ar.run(db_path=path, now=NOW + 60)
    titles = [r["title"] for r in recordings(db) if "Japan" in r["title"]]
    assert titles == ["Formule 1: GP van Japan Race"]                              # one piece again
    assert any(line.startswith("cancelled") and "deel 2/2" in line for line in report)
    assert "[" not in titles[0]                                                    # label gone


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



def test_changing_the_rule_channel_moves_scheduled_recordings(db):
    db.execute("INSERT INTO channels (id, display_name) VALUES (3546, 'NL: Viaplay F1 FHD ( Ziggo )')")
    add_kind_rule(db, "Formule 1 live", "f1", {"channel_id": 3546}, 10, 30, 10)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    race = NOW + 30 * H
    doc = jolpica([f1_race(20, "Brazilian Grand Prix", race, race - 23 * H)])
    ar.run(db_path=path, now=NOW, fetch=lambda url: doc)
    assert {r["channel_id"] for r in recordings(db)} == {3546}
    db.execute("UPDATE autorecord_rules SET params=? WHERE id=1", (_json.dumps({"channel_id": 3548}),))
    db.commit()
    report = ar.run(db_path=path, now=NOW + 60, fetch=lambda url: doc)
    assert {r["channel_id"] for r in recordings(db)} == {3548}
    assert len(recordings(db)) == 2
    assert all("on NL: Viaplay +TV" in line for line in report if line.startswith("moved"))



def test_part_title_labels():
    assert ar.part_title("X", 1, 1, 0, 0, False, "") == "X"
    assert ar._short("UEFA Champions League: PSV - Club Brugge") == "PSV - Club Brugge"
    assert ar._short("Formule 1: GP Race [gedeeltelijk 19:01-20:30, voorrang PSV - Ajax]") == "GP Race"



def test_f1_plans_the_rest_of_the_season(db):
    add_kind_rule(db, "Formule 1 live", "f1", {"channel_id": 3548}, 10, 30, 20)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    days = 24 * H
    doc = jolpica([f1_race(21, "Brazilian Grand Prix", NOW + 7 * days, NOW + 6 * days),
                   f1_race(22, "Las Vegas Grand Prix", NOW + 21 * days, NOW + 20 * days),
                   f1_race(24, "Abu Dhabi Grand Prix", NOW + 35 * days, NOW + 34 * days)])
    ar.run(db_path=path, now=NOW, fetch=lambda url: doc)
    assert len(recordings(db)) == 6                     # 3 weekends x (qualifying + race)
    assert recordings(db)[-1]["title"] == "Formule 1: Abu Dhabi Grand Prix – Race"



SINT = dict(name="Sinterklaasjournaal", include_re=r"Sinterklaasjournaal", exclude_re="",
            before_min=5, after_min=10, priority=15)


def test_daily_show_records_every_new_episode_not_reruns(db):
    db.execute("INSERT INTO channels (id, display_name) VALUES (3555, 'NL: NPO 3 FHD'), (3556, 'NL: NPO 3 HD'), (3560, 'NL: NPO Zapp Extra')")
    add_kind_rule(db, SINT["name"], "epg", {"rerun_window_hours": 20, "channel_re": r"NPO 3\b"},
                  5, 10, 15)
    db.execute("UPDATE autorecord_rules SET include_re=? WHERE name=?", (SINT["include_re"], SINT["name"]))
    db.commit()
    path = db.execute("PRAGMA database_list").fetchone()[2]
    day = 24 * H
    first = NOW + 2 * H                                           # Mon 18:00
    for d in range(4):                                            # Mon..Thu
        t = first + d * day
        for ch in (3555, 3556):                                   # same airing, two variants
            prog(db, ch, t, t + 0.25 * H, "Het Sinterklaasjournaal")
        prog(db, 3555, t + 13 * H, t + 13.25 * H, "Het Sinterklaasjournaal")   # next-morning rerun
        prog(db, 3560, t + 1 * H, t + 1.25 * H, "Het Sinterklaasjournaal")     # other channel
    ar.run(db_path=path, now=NOW)
    recs = recordings(db)
    assert len(recs) == 4                                         # one per day
    assert {r["channel_id"] for r in recs} == {3555}              # FHD, NPO 3 only
    assert [r["start_at"] for r in recs] == [first + d * day - 300 for d in range(4)]
    assert all(r["end_at"] - r["start_at"] == 0.25 * H + 900 for r in recs)
    again = ar.run(db_path=path, now=NOW + 60)                    # idempotent
    assert not [line for line in again if line.startswith(("scheduled", "moved", "cancelled"))]
    assert len(recordings(db)) == 4


# ---- F1 rerun when the live session is cut -----------------------------------

def brazil(race_ts, quali_ts):
    r = f1_race(21, "Brazilian Grand Prix", race_ts, quali_ts)
    r["Circuit"] = {"Location": {"country": "Brazil", "locality": "São Paulo"}}
    return r


def _f1_psv_setup(db, psv_overlaps=True):
    db.execute("INSERT INTO channels (id, display_name) VALUES (3546, 'NL: Viaplay F1 FHD ( Ziggo )')")
    add_kind_rule(db, "PSV live", "team", {"espn_team_id": "148", "guide_word": "PSV"}, 10, 20, 10)
    add_kind_rule(db, "Formule 1 live", "f1", {"channel_id": 3548, "rerun_if_cut": True}, 10, 30, 20)
    race = NOW + 30 * H                                   # Sun 18:00
    ko = race - 1.25 * H if psv_overlaps else race - 6 * H
    doc_f1 = jolpica([brazil(race, race - 23 * H)])
    doc_psv = espn([fixture("402", ko, "SC Cambuur", home=False, competition="Eredivisie")])
    prog(db, 3578, ko - 15 * 60, ko + 1.9 * H, "Eredivisie: SC Cambuur - PSV")
    prog(db, 3548, race - 3 * 24 * H, race - 3 * 24 * H + 2 * H, "Formule 1: GP van Brazilië Race")  # last year's
    prog(db, 3548, race - 1 * H, race + 2.5 * H, "Formule 1: GP van Brazilië Race")                 # live
    prog(db, 3547, race + 3 * H, race + 3.5 * H, "Formule 1 - Faster food Grand Prix van Brazilië")  # magazine
    prog(db, 3548, race + 24 * H, race + 26 * H, "Formule 1: GP van Brazilië Race")                 # Mon rerun
    prog(db, 3547, race + 26 * H, race + 28 * H, "Formule 1: GP van Brazilië Race")                 # later rerun
    fetch = lambda url: doc_f1 if "jolpi" in url else doc_psv
    return race, fetch


def test_f1_rerun_recorded_when_live_race_is_cut(db):
    race, fetch = _f1_psv_setup(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    ar.run(db_path=path, now=NOW, fetch=fetch)
    titles = {r["title"]: r for r in recordings(db)}
    rerun = next(r for t, r in titles.items() if "[herhaling" in t)
    assert rerun["channel_id"] == 3548
    assert rerun["start_at"] == race + 24 * H - 300          # first rerun after the live race, Monday
    assert rerun["end_at"] == race + 26 * H + 600
    live = next(r for t, r in titles.items() if t.startswith("Formule 1") and "[gedeeltelijk" in t)
    assert live["start_at"] > race                           # trimmed live part is kept
    assert any("PSV" in t for t in titles)
    assert len(titles) == 4                                   # PSV, F1 quali, F1 race (part), rerun


def test_no_rerun_when_live_race_is_complete(db):
    race, fetch = _f1_psv_setup(db, psv_overlaps=False)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    ar.run(db_path=path, now=NOW, fetch=fetch)
    assert not [r for r in recordings(db) if "[herhaling" in r["title"]]
    assert any(r["title"] == "Formule 1: Brazilian Grand Prix – Race" for r in recordings(db))


def test_rerun_cancelled_when_the_clash_goes_away(db):
    race, fetch = _f1_psv_setup(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    ar.run(db_path=path, now=NOW, fetch=fetch)
    assert [r for r in recordings(db) if "[herhaling" in r["title"]]
    db.execute("DELETE FROM epg_programs WHERE title LIKE '%Cambuur%'")   # match moved off the guide
    db.commit()
    fetch2 = lambda url: (jolpica([brazil(race, race - 23 * H)]) if "jolpi" in url else espn([]))
    report = ar.run(db_path=path, now=NOW + 60, fetch=fetch2)
    assert not [r for r in recordings(db) if "[herhaling" in r["title"]]
    assert any(r["title"] == "Formule 1: Brazilian Grand Prix – Race" for r in recordings(db))
    assert any("recorded in full" in line for line in report)


def test_f1_place_tokens_match_dutch_titles():
    toks = ar.f1_place_tokens(brazil(NOW, NOW))
    assert "brazilie" in toks and "sao paulo" in toks
    vegas = f1_race(22, "Las Vegas Grand Prix", NOW, NOW)
    vegas["Circuit"] = {"Location": {"country": "USA", "locality": "Las Vegas"}}
    assert "las vegas" in ar.f1_place_tokens(vegas)
