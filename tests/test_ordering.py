import random
from datetime import datetime

from uniagent.models import Placement, Priority, Task
from uniagent.ordering import (UNSCHEDULED_PREFIX, apply_moves, build_items, plan_moves, task_key)
from uniagent.scheduler import schedule

from conftest import SUN


def mk(i, title, start, minutes=30, **kw):
    t = Task(id=str(i), title=title, duration_min=minutes, seq=i, **kw)
    return t, (Placement(start, start + __import__("datetime").timedelta(minutes=minutes)) if start else None)


def test_items_are_ordered_by_date_then_time(tt, rules):
    a, pa = mk(1, "late", datetime(2026, 10, 27, 17, 0))
    b, pb = mk(2, "early", datetime(2026, 10, 27, 8, 30))
    c, pc = mk(3, "tomorrow", datetime(2026, 10, 26, 22, 0))
    items = build_items([a, b, c], {"1": pa, "2": pb, "3": pc}, tt, rules, SUN)
    assert [i.title.split(" ", 1)[1] for i in items] == ["tomorrow", "early", "late"]
    assert items[1].title.startswith("08:30-09:00 ")


def test_unscheduled_items_go_last_and_are_marked(tt, rules):
    a, pa = mk(1, "has slot", datetime(2026, 10, 27, 8, 30))
    b, _ = mk(2, "no slot", None)
    items = build_items([b, a], {"1": pa}, tt, rules, SUN)
    assert items[-1].title == UNSCHEDULED_PREFIX + "no slot"


def test_items_beyond_the_habitica_horizon_are_left_out(tt, rules):
    far, pf = mk(1, "far", datetime(2026, 11, 20, 10, 0))
    assert build_items([far], {"1": pf}, tt, rules, SUN) == []


def test_done_tasks_are_not_pushed(tt, rules):
    from uniagent.models import Status
    a, pa = mk(1, "x", datetime(2026, 10, 27, 8, 30))
    a.status = Status.DONE
    assert build_items([a], {"1": pa}, tt, rules, SUN) == []


def test_keys_are_valid_habitica_aliases():
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]+", task_key("3f2a1b9c-aaaa-bbbb-cccc-1234567890ab"))
    assert re.fullmatch(r"[A-Za-z0-9_-]+", task_key("weird id/with*chars"))


def test_priority_maps_to_habitica_difficulty(tt, rules):
    a, pa = mk(1, "x", datetime(2026, 10, 27, 8, 30), priority=Priority.URGENT)
    assert build_items([a], {"1": pa}, tt, rules, SUN)[0].difficulty == 2.0


def test_classes_can_be_listed_too(tt):
    from dataclasses import replace
    from uniagent.config import Rules
    rules = Rules(include_classes_in_habitica=True, class_lookahead_days=1)
    items = build_items([], {}, tt, rules, SUN)
    assert any("Calculus Lecture" in i.title for i in items)


def test_end_to_end_schedule_to_habitica_order(tt, rules):
    tasks = [Task(id=str(i), title=f"t{i}", duration_min=45, seq=i) for i in range(1, 6)]
    r = schedule(tasks, tt, rules, SUN)
    items = build_items(tasks, r.placements, tt, rules, SUN)
    starts = [i.start for i in items]
    assert starts == sorted(starts)


# ---- reordering -----------------------------------------------------------

def test_no_moves_when_already_in_order():
    assert plan_moves(["a", "b", "c", "x"], ["a", "b", "c"]) == []


def test_new_item_that_habitica_put_on_top_costs_one_move():
    current = ["new", "a", "b", "c", "d", "mine"]
    moves = plan_moves(current, ["a", "b", "new", "c", "d"])
    assert len(moves) == 1
    assert apply_moves(current, moves) == ["a", "b", "new", "c", "d", "mine"]


def test_foreign_tasks_stay_below_and_keep_their_order():
    current = ["mine1", "b", "mine2", "a", "mine3"]
    out = apply_moves(current, plan_moves(current, ["a", "b"]))
    assert out == ["a", "b", "mine1", "mine2", "mine3"]


def test_unknown_ids_are_ignored():
    assert apply_moves(["a", "b"], plan_moves(["a", "b"], ["b", "ghost", "a"])) == ["b", "a"]


def test_random_orders_always_converge_with_few_moves():
    rng = random.Random(3)
    for _ in range(300):
        n = rng.randint(0, 12)
        ids = [f"i{k}" for k in range(n)]
        current = ids[:]
        rng.shuffle(current)
        extra = [f"mine{k}" for k in range(rng.randint(0, 4))]
        current = current + extra
        rng.shuffle(current)
        desired = ids[:]
        rng.shuffle(desired)
        moves = plan_moves(current, desired)
        final = apply_moves(current, moves)
        assert final[:n] == desired
        assert [x for x in final if x in extra] == [x for x in current if x in extra]
        assert len(moves) <= max(0, n)
