from datetime import date, datetime, time

import pytest

from uniagent.config import ConfigError, Rules
from uniagent.timetable import BusTrip, ClassSession, Timetable, busy_blocks, commute_blocks, sessions_on

MON = date(2026, 10, 26)


def test_sessions_only_on_their_weekday(tt):
    assert [s.course for s in sessions_on(tt, MON)] == ["Calculus"]
    assert sessions_on(tt, date(2026, 10, 28)) == []          # Wednesday


def test_term_dates_and_cancellations(tt):
    tt.sessions[0] = ClassSession("Calculus", "Lecture", 0, time(10, 0), time(11, 30),
                                  valid_from=date(2026, 11, 1))
    assert sessions_on(tt, MON) == []
    tt.sessions[0] = ClassSession("Calculus", "Lecture", 0, time(10, 0), time(11, 30))
    tt.cancelled.append((MON, "calculus"))
    assert sessions_on(tt, MON) == []


def test_alternating_weeks():
    lab = ClassSession("Prog", "Lab", 3, time(11, 0), time(13, 0), weeks="odd")
    tt = Timetable(sessions=[lab], term_start=date(2026, 9, 20))     # week 1 = 20-26 Sep
    assert sessions_on(tt, date(2026, 9, 24))                         # week 1 -> odd -> yes
    assert not sessions_on(tt, date(2026, 10, 1))                     # week 2 -> even -> no


def test_odd_even_needs_term_start():
    with pytest.raises(ConfigError):
        Timetable(sessions=[ClassSession("X", "Lab", 0, time(9), time(10), weeks="odd")])


def test_commute_picks_latest_bus_in_time_and_first_bus_home(tt):
    blocks, warns = commute_blocks(tt, MON, Rules())
    assert not warns
    to_campus, home = blocks
    assert "08:30" in to_campus.label                      # latest bus that arrives before 09:50
    assert "13:45" in home.label                           # first bus after 11:40
    assert to_campus.start == datetime(2026, 10, 26, 8, 20)    # 10 min pad before departure


def test_commute_warns_when_no_bus_fits(tt):
    tt.buses = [b for b in tt.buses if b.direction == "to_home"]
    _, warns = commute_blocks(tt, MON, Rules())
    assert any("no bus gets you to campus" in w for w in warns)


def test_no_classes_no_commute(tt):
    assert commute_blocks(tt, date(2026, 10, 28), Rules()) == ([], [])


def test_busy_blocks_are_sorted(tt):
    blocks, _ = busy_blocks(tt, MON, Rules())
    assert [b.start for b in blocks] == sorted(b.start for b in blocks)
    assert {b.kind for b in blocks} == {"class", "commute"}


def test_biweekly_class_alternates_from_its_anchor():
    from datetime import timedelta
    physics = ClassSession("Physics", "Lecture", 6, time(13, 45), time(15, 15),
                           every_weeks=2, anchor=date(2026, 10, 25))          # a Sunday
    tt = Timetable(sessions=[physics])
    anchor = date(2026, 10, 25)
    assert sessions_on(tt, anchor)                                            # this week: yes
    assert not sessions_on(tt, anchor + timedelta(days=7))                    # next week: no
    assert sessions_on(tt, anchor + timedelta(days=14))                       # the week after: yes
    assert sessions_on(tt, anchor - timedelta(days=14))                       # and backwards in time
    assert not sessions_on(tt, anchor - timedelta(days=7))


def test_biweekly_needs_a_matching_anchor():
    with pytest.raises(ConfigError, match="anchor"):
        ClassSession("P", "Lecture", 6, time(13, 45), time(15, 15), every_weeks=2)
    with pytest.raises(ConfigError, match="weekday"):
        ClassSession("P", "Lecture", 6, time(13, 45), time(15, 15), every_weeks=2, anchor=date(2026, 10, 26))
