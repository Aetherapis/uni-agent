from datetime import date, datetime, time
from pathlib import Path

import pytest

from uniagent.actions import ApplyResult
from uniagent.config import load_rules
from uniagent.interpreter import Interpretation
from uniagent.loaders import load_toml, timetable_from_dict
from uniagent.models import Kind, Placement, Priority, Task, TaskType
from uniagent.replies import (compose_reply, describe, format_agenda, format_status, heads_up, help_text,
                              slot)
from uniagent.scheduler import Conflict, ScheduleResult

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config.toml")
TT = timetable_from_dict(load_toml(SEED_PATH), RULES)
NOW = datetime(2026, 10, 26, 20, 5)


def P(day, h, m, mins=60):
    s = datetime(2026, 10, day, h, m)
    from datetime import timedelta
    return Placement(s, s + timedelta(minutes=mins))


def reply(applied=None, interp=None, tasks=(), plan=None):
    return compose_reply(interp or Interpretation(), applied or ApplyResult(), tasks, plan or ScheduleResult(), TT, RULES, NOW)


def test_added_task_shows_the_real_slot_and_deadline():
    t = Task(id="a", title="Read chapter 3", duration_min=60, deadline=datetime(2026, 10, 29, 23, 59))
    plan = ScheduleResult(placements={"a": P(27, 20, 20)})
    text = reply(ApplyResult(added=[t]), tasks=[t], plan=plan)
    assert "+ Read chapter 3 - Tue 27 Oct 20:20-21:20, due Thu 29 Oct" in text


def test_split_tasks_are_summarised_on_one_line():
    parts = [Task(id=f"g.0{i}", title=f"Lab report ({i}/3)", duration_min=100, group="g") for i in (1, 2, 3)]
    def at(month, day, h, m):
        start = datetime(2026, month, day, h, m)
        return Placement(start, start + __import__("datetime").timedelta(minutes=100))
    plan = ScheduleResult(placements={"g.01": at(10, 31, 18, 50), "g.02": at(11, 1, 16, 50), "g.03": at(11, 2, 9, 0)})
    text = reply(ApplyResult(added=parts), tasks=parts, plan=plan)
    assert text.count("\n") == 0 and "+ Lab report - 3 sessions of ~100 min, Sat 31 Oct 18:50 to Mon 02 Nov 09:00" in text


def test_unscheduled_tasks_are_flagged_with_the_reason():
    t = Task(id="a", title="Huge", duration_min=600)
    plan = ScheduleResult(warnings=[Conflict("no_slot", "No room for 'Huge' (600 min) before Mon 26 Oct 22:00", "a")])
    text = reply(ApplyResult(added=[t]), tasks=[t], plan=plan)
    assert "! Huge - no free slot found" in text and "Heads up: No room for 'Huge'" in text


def test_dailies_and_habits():
    d = Task(id="d", title="Review", type=TaskType.DAILY, repeat_days=(5, 6), duration_min=30)
    h = Task(id="h", title="Phone", type=TaskType.HABIT, habit_up=False, habit_down=True)
    plan = ScheduleResult(daily_times={"d": time(20, 30)})
    text = reply(ApplyResult(added=[d, h]), tasks=[d, h], plan=plan)
    assert "+ Review - daily at 20:30 (Sat, Sun)" in text and "+ Phone - habit (-)" in text


def test_everything_else_is_reported():
    t = Task(id="a", title="Quiz prep"); u = Task(id="b", title="Email TA"); w = Task(id="c", title="Water", type=TaskType.HABIT)
    applied = ApplyResult(updated=[t], completed=[u], check_ins=[(w, "up")],
                          away_added=[("Trip", datetime(2026, 10, 28, 15, 15), datetime(2026, 10, 30, 22, 0))],
                          classes_cancelled=[("Physics", (date(2026, 11, 8),))], events_added=["Math midterm"],
                          notes=["watch out"], errors=["Couldn't add task: boom"])
    text = reply(applied, Interpretation(ack="Done!", questions=["Which day?"]), tasks=[t, u, w],
                 plan=ScheduleResult(placements={"a": P(29, 10, 0)}))
    for expected in ("Done!", "~ Quiz prep - now Thu 29 Oct 10:00-11:00", "Done: Email TA", "+1 Water",
                     "Away (Trip): Wed 28 Oct 15:15 -> Fri 30 Oct 22:00", "No Physics on Sun 08 Nov",
                     "Event added: Math midterm", "Note: watch out", "Couldn't do: Couldn't add task: boom",
                     "Question: Which day?"):
        assert expected in text, expected


def test_empty_requests_get_a_friendly_answer():
    assert "didn't find anything" in reply()


def test_long_replies_are_capped():
    tasks = [Task(id=str(i), title=f"T{i}") for i in range(80)]
    assert reply(ApplyResult(added=tasks), tasks=tasks).count("\n") <= 41


def test_agenda_lists_classes_buses_tasks_and_dailies_in_time_order():
    t = Task(id="a", title="Read ch3", duration_min=60)
    d = Task(id="d", title="Review", type=TaskType.DAILY, repeat_days=(1,), duration_min=30)
    plan = ScheduleResult(placements={"a": P(27, 19, 0)}, daily_times={"d": time(20, 30)})
    text = format_agenda(date(2026, 10, 27), 1, [t, d], plan, TT, RULES)          # Tuesday
    lines = text.splitlines()
    assert lines[0].startswith("Tuesday 27 Oct (4 classes") and "tasks" in lines[0]
    times = [l.strip()[:5] for l in lines[1:]]
    assert times == sorted(times) and any("Bus to campus" in l for l in lines) and any("Read ch3" in l for l in lines)
    assert any("Review (daily)" in l for l in lines)


def test_agenda_for_a_free_day_and_a_vacation_day():
    from uniagent.timetable import AwayPeriod
    tt = timetable_from_dict(load_toml(SEED_PATH), RULES)
    tt.away.append(AwayPeriod(datetime(2026, 10, 29, 0, 0), datetime(2026, 10, 30, 22, 0), 0.3, "Trip"))
    text = format_agenda(date(2026, 10, 29), 2, [], ScheduleResult(), tt, RULES)           # Thu + Fri
    assert "Thursday 29 Oct (no classes, away)" in text and text.count("nothing planned") == 2


def test_status_summary():
    a = Task(id="a", title="Soon"); b = Task(id="b", title="Unplanned")
    text = format_status([a, b], ScheduleResult(placements={"a": P(27, 9, 0)}), NOW, pending=2,
                         last_sync=datetime(2026, 10, 26, 19, 0))
    assert "2 to-dos" in text and "Soon" in text and "Not scheduled (1): Unplanned" in text
    assert "2 message(s) waiting" in text and "Habitica last synced: Mon 26 Oct 19:00" in text


def test_heads_up_only_reports_important_warnings_once():
    plan = ScheduleResult(warnings=[Conflict("no_slot", "No room for A"), Conflict("no_slot", "No room for A"),
                                    Conflict("long", "A is long", "a", "info"), Conflict("heavy", "Heavy day")])
    assert heads_up(plan) == ["No room for A", "Heavy day"]


def test_help_mentions_the_commands():
    for word in ("/agenda", "/status", "/replan", "/sync", "voice"):
        assert word in help_text()


def test_slot_format():
    assert slot(P(27, 9, 5, 90)) == "Tue 27 Oct 09:05-10:35"
