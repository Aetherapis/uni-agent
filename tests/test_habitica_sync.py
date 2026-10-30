import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from uniagent.habitica import Budget, HabiticaAuthError, HabiticaClient, RemoteTask
from uniagent.habitica_sync import (build_plan, create_payload, desired_fields, is_managed, sync,
                                    update_payload)
from uniagent.ordering import HabiticaItem

from fake_habitica import FakeHabitica

TZ = ZoneInfo("Africa/Cairo")
TODAY = date(2026, 10, 26)


def todo(key, title, start, difficulty=1.0, notes=""):
    return HabiticaItem(key=key, title=title, due=start.date(), start=start, notes=notes,
                        difficulty=difficulty, group=0, kind="todo")


def daily(key, title, days=(), start=time(20, 30)):
    return HabiticaItem(key=key, title=title, due=None, start=datetime.combine(date(2000, 1, 1), start),
                        notes="", difficulty=1.0, group=0, kind="daily", repeat_days=tuple(days))


def habit(key, title, up=True, down=False):
    return HabiticaItem(key=key, title=title, due=None, start=None, notes="", difficulty=1.0, group=0,
                        kind="habit", up=up, down=down)


def mk(fake, delay=0.0):
    return HabiticaClient(fake.user_id, fake.api_key, call_delay_s=delay, transport=fake,
                          clock=fake.clock.now, sleep=fake.clock.sleep)


def run(fake, items, **kw):
    return sync(mk(fake), items, TZ, today=TODAY, **kw)


D = lambda day, h, m=0: datetime(2026, 10, day, h, m)       # noqa: E731


# ------------------------------------------------------------------ payloads
def test_todo_due_date_is_converted_from_cairo_to_utc():
    item = todo("t-a", "10:00-11:00 x", D(28, 10))
    assert desired_fields(item, TZ)["date"] == "2026-10-28T07:00:00.000Z"        # Cairo is UTC+3 on 28 Oct 2026


def test_daily_weekly_repeat_map_and_start_date():
    payload = create_payload(daily("t-d", "x", days=(1, 6)), TZ, TODAY)           # Tue + Sun
    assert payload["frequency"] == "weekly" and payload["everyX"] == 1
    assert payload["repeat"] == {"m": False, "t": True, "w": False, "th": False, "f": False, "s": False, "su": True}
    assert payload["startDate"].endswith("T21:00:00.000Z") or payload["startDate"].startswith("2026-10-2")


def test_every_day_daily_uses_daily_frequency():
    payload = create_payload(daily("t-d", "x"), TZ, TODAY)
    assert payload["frequency"] == "daily" and all(payload["repeat"].values())


def test_habit_buttons():
    p = create_payload(habit("t-h", "Phone", up=False, down=True), TZ, TODAY)
    assert p["type"] == "habit" and p["up"] is False and p["down"] is True


def test_only_our_aliases_are_managed():
    assert is_managed("t-123") and is_managed("c-20261026-math-1000")
    assert not is_managed(None) and not is_managed("my-own-task") and not is_managed("")


# ------------------------------------------------------------------ the plan
def remote_from(fake):
    from uniagent.habitica import RemoteTask
    return [RemoteTask.from_api(t) for t in fake.tasks.values()]


def test_update_payload_contains_only_what_changed():
    fake = FakeHabitica()
    run(fake, [todo("t-a", "10:00-11:00 x", D(28, 10), notes="n")])
    remote = remote_from(fake)[0]
    same = todo("t-a", "10:00-11:00 x", D(28, 10), notes="n")
    assert update_payload(same, remote, TZ) == {}
    moved_time = todo("t-a", "14:00-15:00 x", D(28, 14), notes="n")
    assert update_payload(moved_time, remote, TZ) == {"text": "14:00-15:00 x"}      # same DAY -> no date update
    next_day = todo("t-a", "10:00-11:00 x", D(29, 10), notes="n")
    assert set(update_payload(next_day, remote, TZ)) == {"date"}


def test_plan_never_touches_foreign_tasks():
    fake = FakeHabitica()
    fake.add_foreign("todo", "my own task")
    plan = build_plan([], remote_from(fake), TZ)
    assert plan.deletes == [] and plan.creates == [] and plan.blocked is None


def test_type_change_is_delete_plus_create():
    fake = FakeHabitica()
    run(fake, [todo("t-a", "x", D(28, 10))])
    plan = build_plan([habit("t-a", "x")], remote_from(fake), TZ)
    assert [t.alias for t in plan.deletes] == ["t-a"] and [i.key for i in plan.creates] == ["t-a"]


def test_mass_delete_is_refused():
    fake = FakeHabitica()
    run(fake, [todo(f"t-{i}", f"task {i}", D(28, 8 + i)) for i in range(8)])
    plan = build_plan([todo("t-0", "task 0", D(28, 8))], remote_from(fake), TZ, max_deletes=15)
    assert plan.blocked and plan.deletes == []                                       # 7 of 8 > 60%
    assert build_plan([], remote_from(fake), TZ).blocked                             # nothing desired at all


def test_small_deletions_are_allowed():
    fake = FakeHabitica()
    run(fake, [todo(f"t-{i}", f"task {i}", D(28, 8 + i)) for i in range(8)])
    plan = build_plan([todo(f"t-{i}", f"task {i}", D(28, 8 + i)) for i in range(7)], remote_from(fake), TZ)
    assert plan.blocked is None and [t.alias for t in plan.deletes] == ["t-7"]


# --------------------------------------------------------------- end to end
def test_first_run_creates_everything_in_the_right_order_per_list():
    fake = FakeHabitica()
    fake.add_foreign("todo", "my own todo")
    fake.add_foreign("habit", "my own habit")
    items = [todo("t-1", "09:00-10:00 first", D(27, 9)), todo("t-2", "13:00-14:00 second", D(27, 13)),
             todo("t-3", "08:00-09:00 third", D(28, 8)),
             daily("t-4", "20:30-21:00 review", days=(1, 6)), habit("t-5", "Drink water")]
    report = run(fake, items)
    assert report.created == ["t-1", "t-2", "t-3", "t-4", "t-5"] and not report.failed
    assert fake.texts("todo") == ["09:00-10:00 first", "13:00-14:00 second", "08:00-09:00 third", "my own todo"]
    assert fake.texts("daily") == ["20:30-21:00 review"]
    assert fake.texts("habit") == ["Drink water", "my own habit"]                    # our habit sits above yours


def test_second_run_changes_nothing():
    fake = FakeHabitica()
    items = [todo("t-1", "09:00-10:00 a", D(27, 9)), todo("t-2", "13:00-14:00 b", D(27, 13))]
    run(fake, items)
    before = len(fake.writes())
    report = run(fake, items)
    assert len(fake.writes()) == before and not report.changed and report.calls == 2   # just two reads


def test_rescheduling_costs_one_update_and_the_minimum_moves():
    fake = FakeHabitica()
    run(fake, [todo("t-1", "09:00-10:00 a", D(27, 9)), todo("t-2", "13:00-14:00 b", D(27, 13)),
               todo("t-3", "15:00-16:00 c", D(27, 15))])
    fake.log.clear()
    changed = [todo("t-1", "09:00-10:00 a", D(27, 9)), todo("t-2", "17:00-18:00 b", D(27, 17)),
               todo("t-3", "15:00-16:00 c", D(27, 15))]                              # b moved to the evening
    report = run(fake, sorted(changed, key=lambda i: i.sort_key))
    assert report.updated == ["t-2"] and report.moves == 1 and not report.created
    assert fake.texts("todo") == ["09:00-10:00 a", "15:00-16:00 c", "17:00-18:00 b"]


def test_cancelled_tasks_are_removed():
    fake = FakeHabitica()
    run(fake, [todo("t-1", "a", D(27, 9)), todo("t-2", "b", D(27, 13))])
    report = run(fake, [todo("t-1", "a", D(27, 9))])
    assert report.deleted == ["t-2"] and fake.by_alias("t-2") is None


def test_tasks_finished_in_chat_are_completed_in_habitica_to_award_the_reward():
    fake = FakeHabitica()
    run(fake, [todo("t-1", "a", D(27, 9)), todo("t-2", "b", D(27, 13))])
    report = run(fake, [todo("t-1", "a", D(27, 9))], done_keys=["t-2"])
    assert report.scored == ["t-2"] and not report.deleted
    assert fake.completed and fake.by_alias("t-2") is None


def test_tasks_ticked_off_in_habitica_are_reported_not_recreated():
    fake = FakeHabitica()
    items = [todo("t-1", "a", D(27, 9)), todo("t-2", "b", D(27, 13))]
    run(fake, items)
    client = mk(fake)
    client.score_task(fake.by_alias("t-2")["_id"], "up")                             # user ticks it in the app
    report = run(fake, items)
    assert report.completed_on_habitica == ["t-2"] and report.created == [] and not report.failed


def test_a_failing_task_does_not_stop_the_rest():
    fake = FakeHabitica()
    fake.add_foreign("todo", "x")
    run(fake, [todo("t-1", "a", D(27, 9))])
    # a second sync where creating t-2 collides with a completed task that reuses the alias
    fake.completed["old"] = {"_id": "old", "alias": "t-2", "type": "todo"}
    report = run(fake, [todo("t-1", "a", D(27, 9)), todo("t-3", "c", D(27, 12))])
    assert report.created == ["t-3"] and not report.failed


def test_bad_credentials_abort_the_run():
    fake = FakeHabitica()
    bad = HabiticaClient(fake.user_id, "nope", call_delay_s=0, transport=fake,
                         clock=fake.clock.now, sleep=fake.clock.sleep)
    with pytest.raises(HabiticaAuthError):
        sync(bad, [todo("t-1", "a", D(27, 9))], TZ, today=TODAY)


def test_dry_run_reads_but_never_writes():
    fake = FakeHabitica()
    report = run(fake, [todo("t-1", "a", D(27, 9))], dry_run=True)
    assert report.created == ["t-1"] and fake.writes() == [] and fake.tasks == {}


def test_budget_cut_off_leaves_the_rest_for_the_next_run():
    fake = FakeHabitica()
    items = [todo(f"t-{i}", f"task {i}", D(27, 8 + i)) for i in range(6)]
    c = HabiticaClient(fake.user_id, fake.api_key, call_delay_s=30, transport=fake, clock=fake.clock.now,
                       sleep=fake.clock.sleep)
    r1 = sync(c, items, TZ, today=TODAY, budget=Budget(100, fake.clock.now))
    assert r1.stopped_early and 0 < len(r1.created) < 6 and r1.remaining_writes > 0
    c2 = HabiticaClient(fake.user_id, fake.api_key, call_delay_s=0, transport=fake, clock=fake.clock.now,
                        sleep=fake.clock.sleep)
    r2 = sync(c2, items, TZ, today=TODAY)
    assert not r2.stopped_early and len(r1.created) + len(r2.created) == 6
    assert fake.texts("todo") == [f"task {i}" for i in range(6)]


@pytest.mark.parametrize("semantics", ["after_removal", "before_removal"])
@pytest.mark.parametrize("create_position", ["top", "bottom"])
def test_ordering_converges_whatever_habitica_does_with_positions(semantics, create_position):
    fake = FakeHabitica(move_semantics=semantics, create_position=create_position)
    fake.add_foreign("todo", "mine 1")
    fake.add_foreign("todo", "mine 2", position="bottom")
    items = [todo(f"t-{i}", f"task {i}", D(27, 8 + i)) for i in (5, 2, 4, 1, 3)]
    items.sort(key=lambda i: i.start)
    report = run(fake, items)
    assert fake.texts("todo")[:5] == [i.title for i in items], semantics
    assert fake.texts("todo")[5:] == ["mine 1", "mine 2"] or set(fake.texts("todo")[5:]) == {"mine 1", "mine 2"}
    assert not report.failed


def test_habit_frequency_rejection_is_handled():
    fake = FakeHabitica(reject_habit_frequency=True)
    report = run(fake, [habit("t-h", "Drink water")])
    assert report.created == ["t-h"] and not report.failed and "frequency" not in fake.by_alias("t-h")


def test_random_edit_sequences_always_converge():
    rng = random.Random(11)
    for semantics in ("after_removal", "before_removal"):
        fake = FakeHabitica(move_semantics=semantics)
        for k in range(3):
            fake.add_foreign("todo", f"mine {k}", position=rng.choice(["top", "bottom"]))
        foreign_order = fake.texts("todo")                                           # your own tasks, as you left them
        for _ in range(12):
            ids = rng.sample(range(20), rng.randint(0, 9))
            items = sorted((todo(f"t-{i}", f"task {i}", D(27 + rng.randint(0, 3), rng.randint(8, 21))) for i in ids),
                           key=lambda i: i.sort_key)
            report = run(fake, items, allow_mass_delete=True)
            assert not report.failed
            texts = fake.texts("todo")
            assert texts[:len(items)] == [i.title for i in items]                    # ours on top, in display order
            assert texts[len(items):] == [t for t in foreign_order]                  # yours untouched, same order
