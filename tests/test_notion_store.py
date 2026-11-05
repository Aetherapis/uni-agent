from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from uniagent.config import load_rules
from uniagent.loaders import load_toml, timetable_from_dict
from uniagent.models import Kind, Placement, Priority, Status, Task, TaskType
from uniagent.notion import NotionClient, discover
from uniagent.notion_setup import setup
from uniagent.notion_store import NotionStore, new_key, task_from_page, task_properties
from uniagent.ordering import build_items
from uniagent.scheduler import schedule, split_task
from uniagent.timetable import sessions_on

from fake_habitica import FakeClock
from fake_notion import FakeNotion

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config.toml")                    # Africa/Cairo
SEED = SEED_PATH
NOW = datetime(2026, 10, 26, 20, 0)


@pytest.fixture
def env():
    fake = FakeNotion()
    clock = FakeClock()
    client = NotionClient(fake.token, transport=fake, clock=clock.now, sleep=clock.sleep)
    setup(client, "parent-page", RULES, seed_path=SEED)
    store = NotionStore(client, discover(client, "parent-page"), RULES, clock=lambda: 1_790_000_000.0)
    return fake, store


def T(title, **kw):
    return Task(id=new_key(), title=title, duration_min=kw.pop("minutes", 30), **kw)


# ---------------------------------------------------------------- round trips
def test_every_task_type_survives_a_round_trip(env):
    fake, store = env
    todo = T("Lab report", minutes=60, priority=Priority.HIGH, deadline=datetime(2026, 10, 30, 17, 0),
             not_before=datetime(2026, 10, 27, 9, 0), notes="chapter 2 too")
    fixed = T("Advisor", kind=Kind.FIXED, fixed_start=datetime(2026, 10, 28, 14, 30))
    dated = T("Quiz prep", minutes=90, on_date=date(2026, 11, 2))
    daily = T("Review", type=TaskType.DAILY, repeat_days=(1, 6), daily_time=time(21, 0), daily_time_fixed=True)
    habit = T("Phone", type=TaskType.HABIT, habit_up=False, habit_down=True, habit_reset="weekly")
    store.add_tasks([todo, fixed, dated, daily, habit], source="typed: ...")

    fresh = NotionStore(store.client, store.ws, RULES)
    loaded = {t.id: t for t in fresh.load_tasks()}
    assert fresh.problems == []
    got = loaded[todo.id]
    assert (got.title, got.duration_min, got.priority, got.deadline, got.not_before, got.notes) == (
        "Lab report", 60, Priority.HIGH, datetime(2026, 10, 30, 17, 0), datetime(2026, 10, 27, 9, 0), "chapter 2 too")
    assert loaded[fixed.id].kind is Kind.FIXED and loaded[fixed.id].fixed_start == datetime(2026, 10, 28, 14, 30)
    assert loaded[dated.id].on_date == date(2026, 11, 2)
    d = loaded[daily.id]
    assert d.type is TaskType.DAILY and d.repeat_days == (1, 6) and d.daily_time == time(21, 0) and d.daily_time_fixed
    h = loaded[habit.id]
    assert h.type is TaskType.HABIT and h.habit_down and not h.habit_up and h.habit_reset == "weekly"


def test_times_are_stored_with_the_cairo_offset(env):
    fake, store = env
    t = T("Advisor", kind=Kind.FIXED, fixed_start=datetime(2026, 10, 28, 14, 30))      # Cairo = UTC+3 (DST ends 29 Oct)
    store.add_tasks([t])
    stored = fake.rows("Tasks")[0]["properties"]["Fixed start"]["date"]["start"]
    assert stored == "2026-10-28T14:30:00+03:00"
    t2 = T("Winter", kind=Kind.FIXED, fixed_start=datetime(2026, 11, 3, 14, 30))       # after DST ends: UTC+2
    store.add_tasks([t2])
    assert fake.rows("Tasks")[1]["properties"]["Fixed start"]["date"]["start"] == "2026-11-03T14:30:00+02:00"


@pytest.mark.parametrize("utc,local", [
    ("2026-10-28T14:00:00.000+00:00", datetime(2026, 10, 28, 17, 0)),      # Cairo is UTC+3 until 29 Oct
    ("2026-10-30T14:00:00.000+00:00", datetime(2026, 10, 30, 16, 0)),      # then UTC+2 (clocks go back)
    ("2026-10-30T17:00:00.000+03:00", datetime(2026, 10, 30, 16, 0)),
])
def test_notion_may_return_other_offsets_and_we_convert_back(utc, local):
    page = {"id": "p1", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "x"}]},
        "Deadline": {"type": "date", "date": {"start": utc}}}}
    assert task_from_page(page, RULES.tz).deadline == local


def test_date_only_deadline_means_end_of_that_day():
    page = {"id": "p1", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "x"}]},
        "Deadline": {"type": "date", "date": {"start": "2026-10-30"}}}}
    assert task_from_page(page, RULES.tz).deadline == datetime(2026, 10, 30, 23, 59)


def test_rows_added_by_hand_in_notion_just_work(env):
    fake, store = env
    ds = store.ws["Tasks"]
    store.client.create_page(ds, {"Name": {"title": [{"text": {"content": "Buy USB cable"}}]}})        # nothing else filled in
    tasks = store.load_tasks()
    t = next(t for t in tasks if t.title == "Buy USB cable")
    assert t.status is Status.ACTIVE and t.type is TaskType.TODO and t.duration_min == 30
    assert t.id == fake.rows("Tasks")[0]["id"]                      # no Key -> the page id is used


def test_unusable_rows_are_skipped_and_reported_not_fatal(env):
    fake, store = env
    ds = store.ws["Tasks"]
    store.client.create_page(ds, {"Name": {"title": [{"text": {"content": "Fixed but no time"}}]},
                                  "Kind": {"select": {"name": "Fixed"}}})
    store.client.create_page(ds, {"Name": {"title": [{"text": {"content": "Fine"}}]}})
    store.client.create_page(ds, {"Key": {"rich_text": [{"text": {"content": "abc"}}]}})            # no name at all
    tasks = store.load_tasks()
    assert [t.title for t in tasks] == ["Fine"] and len(store.problems) == 2


def test_duplicate_keys_are_ignored(env):
    fake, store = env
    ds = store.ws["Tasks"]
    for _ in range(2):
        store.client.create_page(ds, {"Name": {"title": [{"text": {"content": "dup"}}]},
                                      "Key": {"rich_text": [{"text": {"content": "same"}}]}})
    assert len(store.load_tasks()) == 1 and any("duplicate" in why for _, why in store.problems)


def test_cancelled_and_done_are_not_loaded_as_active(env):
    fake, store = env
    a, b, c = T("a"), T("b"), T("c")
    store.add_tasks([a, b, c])
    store.set_status(b, Status.DONE)
    store.set_status(c, Status.CANCELLED)
    assert [t.title for t in store.load_tasks()] == ["a"]


def test_recently_done_keys_are_reported_for_habitica(env):
    fake, store = env
    a, b = T("a"), T("b")
    store.add_tasks([a, b])
    store.set_status(b, Status.DONE)
    assert store.load_done_keys() == [b.id]


def test_update_writes_only_changed_columns(env):
    fake, store = env
    t = T("Report", minutes=60)
    store.add_tasks([t])
    store.load_tasks()
    fake.log.clear()
    t.deadline = datetime(2026, 10, 31, 12, 0)
    assert store.update_task(t) is True
    (_, _, body), = fake.writes()
    assert set(body["properties"]) == {"Deadline"}
    fake.log.clear()
    assert store.update_task(t) is False and fake.writes() == []          # nothing changed -> no call


def test_new_keys_are_unique_and_collisions_are_resolved(env):
    fake, store = env
    a, b = T("a"), T("b")
    b.id = a.id
    store.add_tasks([a, b])
    assert a.id != b.id


# -------------------------------------------------------------- the schedule
def plan(store, now=NOW):
    tt = store.load_timetable()
    tasks = store.load_tasks()
    return tt, tasks, schedule(tasks, tt, RULES, now)


def test_schedule_is_written_back_and_read_identically(env):
    fake, store = env
    store.add_tasks(split_task(T("Physics lab report", minutes=300, deadline=datetime(2026, 11, 3, 17, 0),
                                  priority=Priority.HIGH), RULES))
    store.add_tasks([T("Read ch3", minutes=60, deadline=datetime(2026, 10, 29, 23, 59)),
                     T("Review", type=TaskType.DAILY, minutes=30)])
    tt, tasks, result = plan(store)
    assert store.save_schedule(tasks, result) > 0
    fresh = NotionStore(store.client, store.ws, RULES)
    reloaded = {t.id: t for t in fresh.load_tasks()}
    for t in tasks:
        if t.type is TaskType.TODO:
            assert reloaded[t.id].placement == result.placements[t.id], t.title
        if t.type is TaskType.DAILY:
            assert reloaded[t.id].daily_time == result.daily_times[t.id]


def test_replanning_unchanged_data_writes_nothing(env):
    fake, store = env
    store.add_tasks([T("Read ch3", minutes=60), T("Email TA", minutes=15, priority=Priority.URGENT),
                     T("Review", type=TaskType.DAILY, minutes=30)])
    tt, tasks, result = plan(store)
    store.save_schedule(tasks, result)
    fresh = NotionStore(store.client, store.ws, RULES)                   # a brand-new run
    tt2, tasks2, result2 = plan(fresh)
    fake.log.clear()
    assert fresh.save_schedule(tasks2, result2) == 0 and fake.writes() == []
    assert {k: v for k, v in result2.placements.items()} == {k: v for k, v in result.placements.items()}


def test_editing_one_task_in_notion_moves_only_that_task(env):
    fake, store = env
    ts = [T(f"task {i}", minutes=45) for i in range(1, 5)]
    store.add_tasks(ts)
    tt, tasks, result = plan(store)
    store.save_schedule(tasks, result)

    fresh = NotionStore(store.client, store.ws, RULES)
    tasks = fresh.load_tasks()
    target = next(t for t in tasks if t.title == "task 2")
    target.on_date = date(2026, 10, 29)                                   # "actually, do it Thursday"
    fresh.update_task(target)
    tt = fresh.load_timetable()
    result2 = schedule(tasks, tt, RULES, NOW)
    assert result2.placements[target.id].start.date() == date(2026, 10, 29)
    for t in tasks:
        if t is not target:
            assert result2.placements[t.id] == result.placements[t.id]


def test_unplaceable_tasks_get_an_agent_note_in_notion(env):
    fake, store = env
    store.add_tasks([T("Huge", minutes=600, deadline=datetime(2026, 10, 26, 22, 0))])
    tt, tasks, result = plan(store)
    store.save_schedule(tasks, result)
    row = fake.rows("Tasks")[0]["properties"]
    assert "No room" in "".join(x["plain_text"] for x in row["Agent note"]["rich_text"])
    assert row["Scheduled"]["date"] is None


def test_agent_chosen_daily_time_is_saved_but_a_fixed_one_is_not_overwritten(env):
    fake, store = env
    free = T("Free daily", type=TaskType.DAILY, minutes=30)
    fixed = T("Fixed daily", type=TaskType.DAILY, minutes=20, daily_time=time(21, 30), daily_time_fixed=True)
    store.add_tasks([free, fixed])
    tt, tasks, result = plan(store)
    store.save_schedule(tasks, result)
    rows = {"".join(x["plain_text"] for x in r["properties"]["Name"]["title"]): r["properties"] for r in fake.rows("Tasks")}
    assert rows["Free daily"]["Daily time"]["rich_text"][0]["plain_text"] == f"{result.daily_times[free.id]:%H:%M}"
    assert rows["Fixed daily"]["Daily time"]["rich_text"][0]["plain_text"] == "21:30"


def test_scheduled_tasks_flow_into_habitica_items(env):
    fake, store = env
    store.add_tasks([T("Read ch3", minutes=60), T("Review", type=TaskType.DAILY),
                     T("Drink water", type=TaskType.HABIT)])
    tt, tasks, result = plan(store)
    store.save_schedule(tasks, result)
    fresh = NotionStore(store.client, store.ws, RULES)
    tasks2 = fresh.load_tasks()
    items = build_items(tasks2, {t.id: t.placement for t in tasks2 if t.placement}, fresh.load_timetable(), RULES, NOW,
                        {t.id: t.daily_time for t in tasks2 if t.daily_time})
    assert {i.kind for i in items} == {"todo", "daily", "habit"}


# ---------------------------------------------------------------- timetable
def test_seeded_timetable_matches_the_toml(env):
    fake, store = env
    original = timetable_from_dict(load_toml(SEED), RULES)
    tt = store.load_timetable()
    assert store.problems == []
    assert len(tt.sessions) == len(original.sessions) == 16 and len(tt.buses) == 4
    assert tt.periods == original.periods
    for d in (date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26), date(2026, 10, 27), date(2026, 11, 1), date(2026, 11, 8)):
        assert [(s.course, s.kind, s.start) for s in sessions_on(tt, d)] == \
               [(s.course, s.kind, s.start) for s in sessions_on(original, d)], d


def test_setup_does_not_import_the_timetable_twice(env):
    fake, store = env
    again = setup(store.client, "parent-page", RULES, seed_path=SEED)
    assert again["seeded"] is False and len(fake.rows("Timetable")) == 16


def test_class_rows_may_use_a_period_number_instead_of_times(env):
    fake, store = env
    store.client.create_page(store.ws["Timetable"], {
        "Course": {"title": [{"text": {"content": "Chemistry"}}]}, "Day": {"select": {"name": "Mon"}},
        "Period": {"number": 6}})
    tt = store.load_timetable()
    chem = next(s for s in tt.sessions if s.course == "Chemistry")
    assert (chem.start, chem.end) == (time(17, 45), time(19, 15))


def test_a_bad_timetable_row_is_reported_and_the_rest_still_loads(env):
    fake, store = env
    store.client.create_page(store.ws["Timetable"], {"Course": {"title": [{"text": {"content": "No day"}}]}})
    store.client.create_page(store.ws["Buses"], {"Route": {"title": [{"text": {"content": "?"}}]}})
    tt = store.load_timetable()
    assert len(tt.sessions) == 16 and len(tt.buses) == 4 and len(store.problems) == 2


def test_vacation_and_cancellations_added_through_the_store_are_used(env):
    fake, store = env
    store.add_away("Trip home", datetime(2026, 10, 28, 15, 15), datetime(2026, 10, 30, 22, 0))
    store.add_exception("cancel", "Physics off", date(2026, 10, 25), course="Physics")
    store.add_exception("event", "Midterm", datetime(2026, 11, 3, 10, 0), datetime(2026, 11, 3, 12, 0))
    tt = store.load_timetable()
    assert tt.away[0].start == datetime(2026, 10, 28, 15, 15) and tt.away[0].load_factor == RULES.away_load_factor
    assert not any(s.course == "Physics" for s in sessions_on(tt, date(2026, 10, 25)))
    assert tt.events[0].label == "Midterm" and tt.events[0].end == datetime(2026, 11, 3, 12, 0)


def test_the_biweekly_physics_lecture_survives_the_notion_round_trip(env):
    fake, store = env
    tt = store.load_timetable()
    physics_sun = lambda d: [s for s in sessions_on(tt, d) if s.course == "Physics" and s.weekday == 6]   # noqa: E731
    assert physics_sun(date(2026, 10, 25)) and not physics_sun(date(2026, 11, 1)) and physics_sun(date(2026, 11, 8))


# ------------------------------------------------------------ inbox + state
def test_inbox_stores_dedupes_and_orders_messages(env):
    fake, store = env
    assert store.inbox_add(text="second", source="text", update_id=11, chat_id=5, message_id=2)
    assert store.inbox_add(text="", source="voice", update_id=10, chat_id=5, message_id=1, file_id="AAA")
    assert not store.inbox_add(text="second", source="text", update_id=11, chat_id=5, message_id=2)   # same update
    pending = store.inbox_pending()
    assert [m.update_id for m in pending] == [10, 11]
    assert pending[0].source == "voice" and pending[0].file_id == "AAA"
    assert pending[0].text == ""                      # the "(voice note)" placeholder is not real text
    store.inbox_set_text(pending[0], "remind me to email the TA")
    store.inbox_mark(pending[0], "Done", "added 1 task")
    assert [m.update_id for m in store.inbox_pending()] == [11]


def test_state_values_can_be_set_updated_and_read(env):
    fake, store = env
    assert store.get_state("telegram_offset") is None and store.get_state("x", "dflt") == "dflt"
    store.set_state("telegram_offset", "100")
    store.set_state("telegram_offset", "101")
    assert store.get_state("telegram_offset") == "101" and len(fake.rows("State")) == 2        # periods + offset


def test_recent_done_and_cancelled_tasks_can_be_listed_for_the_weekly_review(env):
    fake, store = env
    store = NotionStore(store.client, store.ws, RULES, clock=lambda: fake.now.timestamp())      # time follows the fake
    old, fresh, gone, open_ = T("Old"), T("Fresh"), T("Gone"), T("Open")
    store.add_tasks([old, fresh, gone, open_])
    store.set_status(old, Status.DONE)
    fake.tick(days=10)                                                # 'Old' was finished long ago
    store.set_status(fresh, Status.DONE)
    store.set_status(gone, Status.CANCELLED)
    assert [t.title for t in store.load_recent_tasks(Status.DONE, days=7)] == ["Fresh"]
    assert [t.title for t in store.load_recent_tasks(Status.CANCELLED, days=7)] == ["Gone"]


def test_recent_tasks_can_use_explicit_now_instead_of_store_clock(env):
    fake, store = env
    old, fresh = T("Old"), T("Fresh")
    store.add_tasks([old, fresh])
    store.set_status(old, Status.DONE)
    fake.tick(days=10)
    store.set_status(fresh, Status.DONE)
    assert [t.title for t in store.load_recent_tasks(Status.DONE, days=7, now=fake.now)] == ["Fresh"]
