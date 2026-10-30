"""The weekly shape of the real timetable + bus data (checked on the fictional seed/timetable.example.toml)."""
from datetime import date, datetime, time
from pathlib import Path

import pytest

from uniagent.config import load_rules
from uniagent.loaders import load_toml, timetable_from_dict
from uniagent.timetable import commute_blocks, end_of_uni_day, sessions_on

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
SAT, SUN, MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (24, 25, 26, 27, 28, 29, 30))


@pytest.fixture(scope="module")
def rules():
    return load_rules(ROOT / "config.toml")


@pytest.fixture(scope="module")
def tt(rules):
    return timetable_from_dict(load_toml(SEED_PATH), rules)


def test_your_week_has_four_class_days(tt):
    counts = {d.strftime("%a"): len(sessions_on(tt, d)) for d in (SAT, SUN, MON, TUE, WED, THU, FRI)}
    assert counts == {"Sat": 4, "Sun": 4, "Mon": 0, "Tue": 4, "Wed": 4, "Thu": 0, "Fri": 0}


def test_physics_lecture_on_sunday_is_biweekly(tt):
    """You attended it Sun 25 Oct, so: no lecture Sun 1 Nov, lecture again Sun 8 Nov."""
    def physics(day):
        return [s for s in sessions_on(tt, day) if s.course == "Physics" and s.kind == "Lecture"]
    assert physics(date(2026, 10, 25)) and not physics(date(2026, 11, 1))
    assert physics(date(2026, 11, 8)) and not physics(date(2026, 11, 15))
    assert len(sessions_on(tt, date(2026, 11, 1))) == 3 and len(sessions_on(tt, date(2026, 11, 8))) == 4
    # Wednesday's Physics lecture is weekly and must not be affected
    assert any(s.kind == "Lecture" for s in sessions_on(tt, date(2026, 11, 4)) if s.course == "Physics")


def test_bus_home_is_unchanged_on_an_off_week_sunday(tt, rules):
    blocks, warnings = commute_blocks(tt, date(2026, 11, 1), rules)           # last class ends 13:30
    assert not warnings and "15:30" in blocks[1].label


def test_periods_are_resolved_to_times(tt):
    sat = sessions_on(tt, SAT)
    assert (sat[0].start, sat[0].end) == (time(10, 15), time(11, 45))      # period 2
    assert (sat[-1].start, sat[-1].end) == (time(15, 45), time(17, 15))    # period 5
    assert tt.periods[3] == (time(12, 0), time(13, 30))


def test_free_days_have_no_commute(tt, rules):
    for d in (MON, THU, FRI):
        assert commute_blocks(tt, d, rules) == ([], [])


@pytest.mark.parametrize("day,out_bus,back_bus", [
    (SAT, "08:40", "17:30"),     # first class 10:15, last ends 17:15
    (SUN, "06:40", "15:30"),     # first class 08:30, last ends 15:15
    (TUE, "08:40", "17:30"),
    (WED, "06:40", "15:30"),
])
def test_right_bus_is_chosen_each_day(tt, rules, day, out_bus, back_bus):
    blocks, warnings = commute_blocks(tt, day, rules)
    assert not warnings
    assert out_bus in blocks[0].label and back_bus in blocks[1].label


def test_return_bus_times_match_what_you_told_me(tt):
    arrivals = sorted(b.arrive for b in tt.buses if b.direction == "to_home")
    departures = sorted(b.depart for b in tt.buses if b.direction == "to_campus")
    assert arrivals == [time(16, 30), time(18, 30)]          # "arrives at the dorms 4:30 PM / 6:30 PM"
    leaving_campus = sorted(b.depart for b in tt.buses if b.direction == "to_home")
    assert leaving_campus == [time(15, 30), time(17, 30)]    # "I leave from campus at 3:30 PM or 5:30 PM"
    assert departures == [time(6, 40), time(8, 40)]          # "leaves the dorms 6:40 AM / 8:40 AM"


def test_end_of_uni_day(tt):
    assert end_of_uni_day(tt, WED) == datetime(2026, 10, 28, 15, 15)
    assert end_of_uni_day(tt, THU) is None


def test_cancelling_one_lecture_for_a_week(tt):
    tt.cancelled.append((SUN, "Physics"))
    assert len(sessions_on(tt, SUN)) == 3
    # and the bus home moves earlier because the last class is now over at 13:30
    rules = load_rules(ROOT / "config.toml")
    assert "15:30" in commute_blocks(tt, SUN, rules)[0][1].label
