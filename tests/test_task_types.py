"""One-time vs daily vs habit: classification, scheduling and Habitica mapping."""
from datetime import date, datetime, time, timedelta

import pytest

from uniagent.classify import classify_text
from uniagent.config import Rules
from uniagent.models import Kind, Priority, Task, TaskType, overlaps
from uniagent.ordering import build_items, items_by_kind
from uniagent.scheduler import schedule, split_task
from uniagent.timetable import Timetable

NOW = datetime(2026, 10, 26, 9, 0)


def daily(i, minutes=30, **kw):
    return Task(id=str(i), title=f"daily {i}", duration_min=minutes, seq=int(i), type=TaskType.DAILY, **kw)


# ------------------------------------------------------------- classification
@pytest.mark.parametrize("text,expected", [
    ("submit the physics report by Thursday", TaskType.TODO),
    ("buy a notebook", TaskType.TODO),
    ("don't forget to email the TA", TaskType.TODO),
    ("review my notes every night", TaskType.DAILY),
    ("German vocab daily", TaskType.DAILY),
    ("practice programming every Sunday and Tuesday", TaskType.DAILY),
    ("drink more water", TaskType.HABIT),
    ("stop checking my phone while studying", TaskType.HABIT),
    ("stretch whenever I study for a long time", TaskType.HABIT),
    ("كل يوم راجع المحاضرات", TaskType.DAILY),
])
def test_classification(text, expected):
    assert classify_text(text).type is expected


def test_named_weekdays_are_extracted():
    c = classify_text("practice programming every Sunday and Tuesday")
    assert c.repeat_days == (1, 6)                       # Tue, Sun


def test_clock_time_is_extracted():
    assert classify_text("review lectures every night at 9pm").daily_time == time(21, 0)
    assert classify_text("revise every day at 21:30").daily_time == time(21, 30)


def test_negative_habits_get_a_minus_button():
    c = classify_text("avoid junk food")
    assert c.type is TaskType.HABIT and c.habit_down and not c.habit_up


def test_unsure_cases_have_low_confidence_so_the_agent_can_ask():
    assert classify_text("go to the gym twice a week").confidence < 0.6
    assert classify_text("drink water every day").confidence < 0.6          # habit or daily?
    assert classify_text("think about the project").confidence < 0.7        # no date, no repetition


# ------------------------------------------------------------------ validation
def test_only_todos_can_have_a_fixed_time_or_date():
    with pytest.raises(ValueError):
        Task(id="x", title="x", type=TaskType.DAILY, kind=Kind.FIXED, fixed_start=NOW)
    with pytest.raises(ValueError):
        Task(id="x", title="x", type=TaskType.HABIT, on_date=date(2026, 10, 27))
    with pytest.raises(ValueError):
        Task(id="x", title="x", type=TaskType.HABIT, habit_up=False, habit_down=False)
    with pytest.raises(ValueError):
        Task(id="x", title="x", type=TaskType.DAILY, repeat_days=(7,))


def test_repeat_days_are_normalised():
    assert daily(1, repeat_days=(6, 1, 1)).repeat_days == (1, 6)


# ----------------------------------------------------------------- scheduling
def test_habits_are_never_scheduled():
    habit = Task(id="h", title="drink water", type=TaskType.HABIT)
    r = schedule([habit], Timetable(), Rules(), NOW)
    assert r.placements == {} and r.daily_times == {} and r.unplaced == []


def test_daily_gets_one_stable_time_reserved_on_every_repeat_day():
    rules = Rules()
    d = daily(1, 30, repeat_days=(1, 6))                     # Tue + Sun
    r = schedule([d], Timetable(), rules, NOW)
    assert r.daily_times["1"] == rules.daily_preferred_time
    assert {day: load.task_min for day, load in r.day_loads.items()} == {
        day: 30 for day in r.day_loads}                      # reserved on each repeat day only
    assert all(day.weekday() in (1, 6) for day in r.day_loads)


def test_daily_time_requested_by_the_user_is_respected():
    d = daily(1, 20, daily_time=time(21, 30), daily_time_fixed=True)
    r = schedule([d], Timetable(), Rules(), NOW)
    assert r.daily_times["1"] == time(21, 30)


def test_daily_avoids_classes_and_picks_a_time_that_fits_every_day():
    from uniagent.models import Block
    rules = Rules(daily_preferred_time=time(20, 0))
    tt = Timetable(events=[Block(datetime(2026, 10, 28, 19, 30), datetime(2026, 10, 28, 21, 0), "Club", "event")])
    r = schedule([daily(1, 30)], tt, rules, NOW)
    chosen = r.daily_times["1"]
    for offset in range(0, 15):
        day = NOW.date() + timedelta(days=offset)
        start = datetime.combine(day, chosen)
        if start >= NOW:
            for b in tt.events:
                assert not overlaps(start, start + timedelta(minutes=30), b.start, b.end)
    assert not any(w.kind == "daily" for w in r.warnings)


def test_daily_time_is_sticky_between_runs():
    d = daily(1, 30)
    first = schedule([d], Timetable(), Rules(), NOW)
    d.daily_time = first.daily_times["1"]
    second = schedule([d], Timetable(), Rules(), NOW + timedelta(hours=3))
    assert second.daily_times["1"] == d.daily_time


def test_dailies_count_towards_the_daily_load_so_todos_get_less_room():
    rules = Rules(max_task_minutes_per_day=120)
    dailies = [daily(1, 60), daily(2, 40)]
    todo = Task(id="t", title="todo", duration_min=60, seq=9, deadline=datetime(2026, 10, 26, 23, 59))
    r = schedule(dailies + [todo], Timetable(), rules, datetime(2026, 10, 26, 8, 0))
    # 100 min of dailies on Monday leaves 20 min of the 120 cap: the 60-min todo can't be on Monday
    assert "t" in r.unplaced or r.placements["t"].start.date() != date(2026, 10, 26)


def test_split_does_not_touch_dailies_or_habits():
    rules = Rules()
    assert len(split_task(daily(1, 600), rules)) == 1
    assert len(split_task(Task(id="h", title="h", type=TaskType.HABIT, duration_min=600), rules)) == 1


# --------------------------------------------------------------- Habitica map
def test_each_type_lands_in_its_own_habitica_list(tt, rules):
    todo = Task(id="1", title="Write report", duration_min=60, seq=1)
    d = daily(2, 30, repeat_days=(1,))
    h = Task(id="3", title="Drink water", type=TaskType.HABIT, habit_up=True, habit_down=False)
    now = datetime(2026, 10, 25, 20, 0)
    r = schedule([todo, d, h], tt, rules, now)
    groups = items_by_kind(build_items([todo, d, h], r.placements, tt, rules, now, r.daily_times))
    assert [i.kind for i in groups["todo"]] == ["todo"] and groups["todo"][0].due is not None
    assert groups["daily"][0].repeat_days == (1,) and groups["daily"][0].due is None
    assert groups["daily"][0].title[:5].count(":") == 1                      # "20:00-20:30 daily 2"
    assert groups["habit"][0].up and not groups["habit"][0].down


def test_dailies_are_ordered_by_time_of_day(tt, rules):
    late = daily(1, 20, daily_time=time(22, 0), daily_time_fixed=True)
    early = daily(2, 20, daily_time=time(9, 0), daily_time_fixed=True)
    r = schedule([late, early], tt, rules, datetime(2026, 10, 25, 8, 0))
    items = items_by_kind(build_items([late, early], r.placements, tt, rules,
                                      datetime(2026, 10, 25, 8, 0), r.daily_times))["daily"]
    assert [i.title.split(" ", 1)[0] for i in items] == ["09:00-09:20", "22:00-22:20"]


def test_habits_are_listed_alphabetically(tt, rules):
    a = Task(id="a", title="Walk", type=TaskType.HABIT)
    b = Task(id="b", title="Drink water", type=TaskType.HABIT)
    items = items_by_kind(build_items([a, b], {}, tt, rules, NOW))["habit"]
    assert [i.title for i in items] == ["Drink water", "Walk"]


def test_task_types_load_from_toml():
    from uniagent.loaders import tasks_from_dicts
    tasks = tasks_from_dicts([
        {"title": "Review", "type": "daily", "repeat": ["sat", "tue"], "time": "21:00", "minutes": 25},
        {"title": "Phone", "type": "habit", "direction": "down", "reset": "weekly"},
        {"title": "Report", "deadline": "2026-10-30"},
    ])
    assert tasks[0].type is TaskType.DAILY and tasks[0].repeat_days == (1, 5) and tasks[0].daily_time_fixed
    assert tasks[1].habit_down and not tasks[1].habit_up and tasks[1].habit_reset == "weekly"
    assert tasks[2].type is TaskType.TODO
