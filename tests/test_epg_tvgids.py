"""tvgids.nl day pages: the first entry is the programme running at midnight,
which started the evening before. Regression for the guide being one day late."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.epg import parse_tvgids

PAGE = Path(__file__).parent / "fixtures" / "tvgids_ziggosport_2026-11-04.html"
AMS = ZoneInfo("Europe/Amsterdam")


def _at(ts):
    return datetime.fromtimestamp(ts, AMS).strftime("%a %d-%m %H:%M")


def test_day_page_is_not_shifted_a_day():
    day = datetime(2026, 11, 4, tzinfo=AMS)
    progs = parse_tvgids(PAGE.read_text(encoding="utf-8"), day)
    by_title = {}
    for p in progs:                                   # first airing of each title
        by_title.setdefault(p["title"], _at(p["start"]))
    assert by_title["UEFA Champions League: PSV - Club Brugge"] == "Wed 04-11 21:00"   # live, not Thursday
    assert by_title["UEFA Champions League: Fenerbahçe - Liverpool"] == "Wed 04-11 18:45"
    assert _at(progs[0]["start"]) == "Tue 03-11 23:35"          # began the evening before
    assert _at(progs[1]["start"]) == "Wed 04-11 00:35"
    starts = [p["start"] for p in progs]
    assert starts == sorted(starts)
    # the page's last entries run past midnight into Thursday
    assert _at(progs[-1]["start"]).startswith("Thu 05-11")
