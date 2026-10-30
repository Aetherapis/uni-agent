from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from uniagent.config import load_rules, rules_from_dict
from uniagent.digest import due_digests, preview, upcoming_deadlines, weekly_review
from uniagent.loaders import load_toml, timetable_from_dict
from uniagent.models import Placement, Status, Task, TaskType
from uniagent.scheduler import schedule

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config.toml")
TT = timetable_from_dict(load_toml(SEED_PATH), RULES)
MON_NIGHT = datetime(2026, 10, 26, 21, 5)


def plan_for(tasks, now=MON_NIGHT):
    return schedule(tasks, TT, RULES, now)


def T(i, title, **kw):
    return Task(id=str(i), title=title, seq=i, duration_min=kw.pop("minutes", 45), **kw)


# --------------------------------------------------------------------- timing
def test_defaults_send_an_evening_preview_and_a_friday_review_but_no_morning_message():
    assert RULES.digest_morning is None and RULES.digest_evening == time(21, 0)
    assert (RULES.weekly_day, RULES.weekly_time) == (4, time(10, 0))


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 26, 20, 59), []),                        # too early
    (datetime(2026, 10, 26, 21, 0), ["evening"]),
    (datetime(2026, 10, 26, 23, 59), ["evening"]),               # a late run still sends it
    (datetime(2026, 10, 27, 0, 30), []),                         # too stale: never at night
    (datetime(2026, 10, 30, 10, 5), ["weekly"]),                 # Friday morning
    (datetime(2026, 10, 29, 10, 5), []),                         # Thursday: no weekly review
    (datetime(2026, 10, 30, 21, 5), ["evening"]),
])
def test_when_digests_are_due(now, expected):
    assert due_digests(now, RULES, {}) == expected


def test_each_digest_is_sent_once_per_day():
    now = MON_NIGHT
    assert due_digests(now, RULES, {"evening": now.date().isoformat()}) == []
    assert due_digests(now + timedelta(days=1), RULES, {"evening": now.date().isoformat()}) == ["evening"]


def test_morning_digest_can_be_switched_on_and_everything_off():
    on = rules_from_dict({"digest": {"morning": "07:30", "evening": "", "weekly_day": ""}})
    assert due_digests(datetime(2026, 10, 27, 7, 45), on, {}) == ["morning"]
    assert due_digests(datetime(2026, 10, 27, 21, 5), on, {}) == []
    assert due_digests(datetime(2026, 10, 30, 11, 0), on, {}) == []                 # weekly review is off too


# ------------------------------------------------------------------- content
def test_the_evening_preview_shows_tomorrows_buses_classes_tasks_and_deadlines():
    tasks = [T(1, "Read chapter 3", deadline=datetime(2026, 10, 29, 12, 0)),
             T(2, "Review", type=TaskType.DAILY, repeat_days=(1,), minutes=30)]
    plan = plan_for(tasks)
    text = preview("evening", MON_NIGHT, tasks, plan, TT, RULES)
    assert text.startswith("Tomorrow:") and "Tuesday 27 Oct (4 classes" in text
    assert "Bus to campus (Dorms bus 08:40" in text and "Mathematics I Lecture" in text and "Review (daily)" in text
    assert "Due soon:" in text and "Read chapter 3" in text and "(planned" in text


def test_nothing_to_say_means_no_message():
    thursday_eve = datetime(2026, 10, 28, 21, 5)                  # tomorrow = Thursday, a free day
    assert preview("evening", thursday_eve, [], plan_for([], thursday_eve), TT, RULES) is None
    forced = preview("evening", thursday_eve, [], plan_for([], thursday_eve), TT, RULES, force=True)
    assert "nothing planned" in forced                              # /digest always answers


def test_a_deadline_alone_is_enough_to_send_a_preview_even_on_a_free_day():
    now = datetime(2026, 10, 28, 21, 5)
    tasks = [T(1, "Submit form", deadline=datetime(2026, 10, 30, 12, 0), minutes=15)]
    text = preview("evening", now, tasks, plan_for(tasks, now), TT, RULES)
    assert text and "Submit form" in text


def test_unscheduled_deadlines_are_called_out():
    huge = T(1, "Huge", minutes=900, deadline=datetime(2026, 10, 27, 12, 0))
    lines = upcoming_deadlines([huge], plan_for([huge]), MON_NIGHT, 3)
    assert lines and "not scheduled yet" in lines[0]


def test_split_tasks_appear_once_in_the_deadline_list():
    parts = [T(f"1.0{i}", f"Lab report ({i}/3)", group="g", minutes=90, deadline=datetime(2026, 10, 31, 17, 0)) for i in (1, 2, 3)]
    lines = upcoming_deadlines(parts, plan_for(parts), MON_NIGHT, 7)
    assert len(lines) == 1 and "Lab report" in lines[0] and "3 sessions" in lines[0] and "(1/3)" not in lines[0]


def test_the_morning_preview_is_about_today():
    now = datetime(2026, 10, 27, 7, 40)
    text = preview("morning", now, [], plan_for([], now), TT, RULES)
    assert text.startswith("Good morning - today:") and "Tuesday 27 Oct" in text


# ---------------------------------------------------------------- weekly review
def test_the_weekly_review_summarises_the_week_and_warns_about_full_days():
    now = datetime(2026, 10, 30, 10, 5)
    done = [T(1, "Email TA", status=Status.DONE), T(2, "Lab report (1/2)", status=Status.DONE, group="g"),
            T(3, "Lab report (2/2)", status=Status.DONE, group="g")]
    cancelled = [T(4, "Old idea", status=Status.CANCELLED)]
    open_tasks = [T(i, f"Chore {i}", minutes=60, deadline=datetime(2026, 11, 3, 12, 0)) for i in range(10, 14)]
    plan = plan_for(open_tasks, now)
    text = weekly_review(now, open_tasks, done, cancelled, plan, TT, RULES)
    assert "Weekly review (Sat 24 Oct - Fri 30 Oct)" in text
    assert "Done (2): Email TA, Lab report" in text and "Cancelled: 1" in text and "Still open: 4 to-dos" in text
    assert "Next 7 days:" in text and "Sat 31 Oct: 4 classes" in text and "Deadlines this coming week:" in text


def test_an_empty_week_is_reported_gently():
    text = weekly_review(datetime(2026, 10, 30, 10, 5), [], [], [], plan_for([]), TT, RULES)
    assert "nothing marked done" in text and "Still open: 0 to-dos" in text
