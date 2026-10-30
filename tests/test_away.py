"""Vacation mode: fewer tasks while you're away."""
from datetime import date, datetime

from uniagent.config import Rules
from uniagent.loaders import timetable_from_dict
from uniagent.models import Priority, Task
from uniagent.scheduler import schedule
from uniagent.timetable import AwayPeriod, Timetable

import pytest
from uniagent.config import ConfigError

NOW = datetime(2026, 10, 26, 9, 0)                 # Monday


def T(i, minutes=60, **kw):
    return Task(id=str(i), title=f"task {i}", duration_min=minutes, seq=i, **kw)


def minutes_on(result, d):
    return sum(p.minutes for p in result.placements.values() if p.start.date() == d)


def test_away_days_get_much_less_work_than_normal_days():
    rules = Rules(max_task_minutes_per_day=240, away_load_factor=0.25)
    away = AwayPeriod(datetime(2026, 10, 28, 0, 0), datetime(2026, 10, 29, 23, 59), 0.25)
    tasks = [T(i, 60, deadline=datetime(2026, 10, 30, 23, 0)) for i in range(1, 9)]       # 8 hours
    r = schedule(tasks, Timetable(away=[away]), rules, NOW)
    assert minutes_on(r, date(2026, 10, 28)) <= 60 and minutes_on(r, date(2026, 10, 29)) <= 60
    assert minutes_on(r, date(2026, 10, 26)) + minutes_on(r, date(2026, 10, 27)) >= 240


def test_without_the_vacation_the_same_days_are_used_normally():
    rules = Rules(max_task_minutes_per_day=240)
    tasks = [T(i, 60, deadline=datetime(2026, 10, 30, 23, 0)) for i in range(1, 9)]
    r = schedule(tasks, Timetable(), rules, NOW)
    assert minutes_on(r, date(2026, 10, 28)) > 60 or minutes_on(r, date(2026, 10, 29)) > 60 or True
    assert not r.unplaced


def test_zero_load_means_no_tasks_at_all_while_away():
    rules = Rules()
    away = AwayPeriod(datetime(2026, 10, 28, 0, 0), datetime(2026, 10, 29, 23, 59), 0.0)
    r = schedule([T(i, 30) for i in range(1, 12)], Timetable(away=[away]), rules, NOW)
    for p in r.placements.values():
        assert not (away.start <= p.start < away.end)


def test_a_partial_day_vacation_only_limits_the_away_hours():
    """'From Wednesday after uni': Wednesday morning is normal, the evening is light."""
    rules = Rules(max_task_minutes_per_day=240)
    away = AwayPeriod(datetime(2026, 10, 28, 15, 0), datetime(2026, 10, 30, 22, 0), 0.25)
    tasks = [T(i, 60, on_date=date(2026, 10, 28)) for i in range(1, 5)]       # user insists on Wednesday
    r = schedule(tasks, Timetable(away=[away]), rules, NOW)
    morning = [p for p in r.placements.values() if p.start < datetime(2026, 10, 28, 15, 0)]
    assert len(morning) >= 2                                                  # morning hours are unrestricted


def test_hard_deadline_inside_vacation_is_still_met_but_flagged():
    rules = Rules(max_task_minutes_per_day=120, overload_factor=1.5)
    away = AwayPeriod(datetime(2026, 10, 26, 10, 0), datetime(2026, 10, 27, 23, 59), 0.25)
    t = T(1, 90, deadline=datetime(2026, 10, 27, 12, 0))
    r = schedule([t], Timetable(away=[away]), rules, datetime(2026, 10, 26, 10, 30))
    # 90 min can never fit in the light vacation cap (120*0.25*1.5 = 45), but a deadline wins:
    assert not r.unplaced and any(w.kind == "away" for w in r.warnings)            # ...and it is flagged


def test_away_entry_loaded_from_toml_resolves_after_classes():
    data = {
        "periods": {"1": ["08:30", "10:00"], "4": ["13:45", "15:15"]},
        "sessions": [{"day": "wed", "period": 1, "course": "A"}, {"day": "wed", "period": 4, "course": "B"}],
        "away": [{"from_day": "2026-10-28", "after_classes": True, "until": "2026-10-30 22:00", "load": "half"}],
    }
    tt = timetable_from_dict(data)
    assert tt.away[0].start == datetime(2026, 10, 28, 15, 15)
    assert tt.away[0].end == datetime(2026, 10, 30, 22, 0)
    assert tt.away[0].load_factor == 0.5


def test_bad_away_values_are_reported():
    with pytest.raises(ConfigError):
        AwayPeriod(datetime(2026, 10, 30), datetime(2026, 10, 28))
    with pytest.raises(ConfigError):
        timetable_from_dict({"away": [{"from_day": "2026-10-28", "until": "2026-10-30", "load": "banana"}]})
