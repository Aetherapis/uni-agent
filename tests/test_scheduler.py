import random
import pytest
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from uniagent.config import Rules
from uniagent.models import Kind, Placement, Priority, Task, overlaps
from uniagent.scheduler import schedule, split_task
from uniagent.timetable import Timetable, busy_blocks

from conftest import MON, SUN


def T(i, minutes=30, **kw):
    return Task(id=str(i), title=f"task {i}", duration_min=minutes, seq=int(i) if str(i).isdigit() else 0, **kw)


def all_slots(result):
    return sorted(result.placements.items(), key=lambda kv: kv[1].start)


def assert_invariants(result, tasks, tt, rules):
    """No overlaps, nothing outside day hours, caps respected unless flagged."""
    by_id = {t.id: t for t in tasks}
    slots = [(tid, p) for tid, p in result.placements.items()]
    for i, (a, pa) in enumerate(slots):
        for b, pb in slots[i + 1:]:
            if by_id[a].kind is Kind.FIXED and by_id[b].kind is Kind.FIXED:
                continue
            assert not overlaps(pa.start, pa.end, pb.start, pb.end), (a, b)
    flagged = {w.task_id for w in result.warnings if w.kind in ("rest", "heavy")}
    per_day: dict[date, int] = {}
    for tid, p in slots:
        t = by_id[tid]
        if t.kind is Kind.FLEXIBLE and tid not in flagged:
            d = p.start.date()
            assert datetime.combine(d, rules.day_start) <= p.start and p.end <= datetime.combine(d, rules.day_end)
            blocks, _ = busy_blocks(tt, d, rules)
            assert not any(overlaps(p.start, p.end, b.start, b.end) for b in blocks), tid
        per_day[p.start.date()] = per_day.get(p.start.date(), 0) + p.minutes
    for d, minutes in per_day.items():
        if not flagged:
            assert minutes <= rules.task_cap(d.weekday()) or any(
                by_id[tid].kind is Kind.FIXED for tid, p in slots if p.start.date() == d)


def test_flexible_task_gets_a_free_slot_not_overlapping_classes(tt, rules):
    tasks = [T(1, 60)]
    r = schedule(tasks, tt, rules, SUN)
    assert "1" in r.placements and not r.unplaced
    assert_invariants(r, tasks, tt, rules)


def test_never_scheduled_in_the_past_or_too_soon(tt, rules):
    now = datetime(2026, 10, 27, 10, 3)
    r = schedule([T(1, 30)], tt, rules, now)
    assert r.placements["1"].start >= now + timedelta(minutes=rules.min_lead_min)


def test_fixed_task_is_never_moved_and_overlap_is_reported(tt, rules):
    fixed = T(1, 60, kind=Kind.FIXED, fixed_start=datetime(2026, 10, 26, 10, 30))   # inside the lecture
    r = schedule([fixed], tt, rules, SUN)
    assert r.placements["1"].start == datetime(2026, 10, 26, 10, 30)
    assert any(w.kind == "overlap" for w in r.warnings)


def test_flexible_tasks_route_around_fixed_ones(tt, rules):
    fixed = T(1, 120, kind=Kind.FIXED, fixed_start=datetime(2026, 10, 25, 20, 30))
    flex = T(2, 30)
    r = schedule([fixed, flex], tt, rules, SUN)
    p, f = r.placements["2"], r.placements["1"]
    assert not overlaps(p.start, p.end, f.start, f.end)


def test_daily_cap_is_respected_and_load_is_spread(tt, rules):
    tasks = [T(i, 60) for i in range(1, 8)]               # 7 hours of work
    r = schedule(tasks, tt, rules, SUN)
    assert not r.unplaced
    per_day: dict[date, int] = {}
    for p in r.placements.values():
        per_day[p.start.date()] = per_day.get(p.start.date(), 0) + p.minutes
    assert max(per_day.values()) <= rules.max_task_minutes_per_day
    assert len(per_day) >= 3
    assert_invariants(r, tasks, tt, rules)


def test_deadline_is_respected_and_early_finish_preferred(tt, rules):
    deadline = datetime(2026, 10, 30, 17, 0)
    tasks = [T(1, 90, deadline=deadline)]
    r = schedule(tasks, tt, rules, SUN)
    p = r.placements["1"]
    assert p.end <= deadline - timedelta(days=rules.deadline_buffer_days)


def test_impossible_deadline_is_reported_never_silently_missed(tt, rules):
    deadline = datetime(2026, 10, 25, 22, 0)              # 2 hours from now, task needs 5
    big = T(1, 300, deadline=deadline)
    r = schedule([big], tt, rules, SUN)
    assert "1" in r.unplaced
    assert any(w.kind == "no_slot" for w in r.warnings)


def test_overload_is_a_flagged_last_resort(tt):
    rules = Rules(max_task_minutes_per_day=120, overload_factor=1.5)
    deadline = datetime(2026, 10, 25, 23, 59)             # only tonight is available
    r = schedule([T(1, 60, deadline=deadline), T(2, 60, deadline=deadline)], tt, rules, datetime(2026, 10, 25, 14, 0))
    assert not r.unplaced      # 120 fits in the 120 cap exactly


def test_friday_evening_is_protected(tt, rules):
    friday = date(2026, 10, 30)
    tasks = [T(i, 60, on_date=None, deadline=datetime(2026, 10, 30, 23, 0)) for i in range(1, 5)]
    r = schedule(tasks, tt, rules, datetime(2026, 10, 29, 12, 0))
    for p in r.placements.values():
        if p.start.date() == friday:
            assert p.end <= datetime.combine(friday, rules.rest_evening_start)


def test_explicit_day_can_use_rest_time_but_it_is_flagged(tt, rules):
    friday = date(2026, 10, 30)
    # fill Friday's protected-hours window so only the evening is left
    tasks = [T(1, 30, on_date=friday)]
    r = schedule(tasks, tt, rules, SUN)
    assert r.placements["1"].start.date() == friday          # honours the user's day


def test_stability_adding_a_task_does_not_move_existing_ones(tt, rules):
    tasks = [T(i, 45) for i in range(1, 5)]
    first = schedule(tasks, tt, rules, SUN)
    for t in tasks:
        t.placement = first.placements[t.id]
    tasks.append(T(5, 45))
    second = schedule(tasks, tt, rules, SUN)
    for t in tasks[:4]:
        assert second.placements[t.id] == first.placements[t.id]


def test_changing_the_date_moves_only_that_task(tt, rules):
    tasks = [T(i, 45) for i in range(1, 5)]
    first = schedule(tasks, tt, rules, SUN)
    for t in tasks:
        t.placement = first.placements[t.id]
    wanted = date(2026, 10, 29)
    tasks[1].on_date = wanted                              # "actually, do it Thursday"
    second = schedule(tasks, tt, rules, SUN)
    assert second.placements["2"].start.date() == wanted
    for tid in ("1", "3", "4"):
        assert second.placements[tid] == first.placements[tid]


def test_giving_an_exact_time_turns_it_into_a_fixed_task(tt, rules):
    t = T(1, 30)
    first = schedule([t], tt, rules, SUN)
    t.placement = first.placements["1"]
    t.kind, t.fixed_start = Kind.FIXED, datetime(2026, 10, 29, 17, 0)
    second = schedule([t], tt, rules, SUN)
    assert second.placements["1"].start == datetime(2026, 10, 29, 17, 0)


def test_missed_task_rolls_over_to_the_future(tt, rules):
    t = T(1, 30)
    t.placement = Placement(datetime(2026, 10, 26, 15, 0), datetime(2026, 10, 26, 15, 30))
    later = datetime(2026, 10, 26, 18, 0)
    r = schedule([t], tt, rules, later)
    assert r.placements["1"].start >= later


def test_task_in_progress_is_not_moved(tt, rules):
    t = T(1, 60)
    t.placement = Placement(datetime(2026, 10, 28, 14, 0), datetime(2026, 10, 28, 15, 0))
    r = schedule([t], tt, rules, datetime(2026, 10, 28, 14, 20))
    assert r.placements["1"] == t.placement


def test_done_and_cancelled_tasks_are_ignored(tt, rules):
    from uniagent.models import Status
    a, b = T(1), T(2)
    a.status, b.status = Status.DONE, Status.CANCELLED
    r = schedule([a, b], tt, rules, SUN)
    assert r.placements == {} and r.unplaced == []


def test_priority_urgent_goes_before_low(tt, rules):
    low, urgent = T(1, 60, priority=Priority.LOW), T(2, 60, priority=Priority.URGENT)
    r = schedule([low, urgent], tt, rules, SUN)
    assert r.placements["2"].start <= r.placements["1"].start


def test_split_long_task_into_ordered_sessions(tt, rules):
    parts = split_task(T(9, 300), rules)
    assert len(parts) == 3 and sum(p.duration_min for p in parts) == 300
    assert all(p.duration_min <= rules.max_chunk_min for p in parts)
    assert parts[1].after_id == parts[0].id and parts[0].group == "9"
    r = schedule(parts, tt, rules, SUN)
    ends = [r.placements[p.id] for p in parts]
    assert ends[0].end < ends[1].start < ends[2].start


def test_short_and_fixed_tasks_are_not_split(rules):
    assert len(split_task(T(1, 60), rules)) == 1
    fixed = T(2, 300, kind=Kind.FIXED, fixed_start=datetime(2026, 10, 26, 9, 0))
    assert split_task(fixed, rules) == [fixed]


def test_deterministic(tt, rules):
    tasks = [T(i, 30 + 15 * (i % 3)) for i in range(1, 9)]
    assert schedule(tasks, tt, rules, SUN).placements == schedule(tasks, tt, rules, SUN).placements


def test_randomised_invariants(tt, rules):
    rng = random.Random(7)
    for round_ in range(25):
        tasks = []
        for i in range(1, rng.randint(3, 14)):
            kw = {}
            roll = rng.random()
            if roll < 0.25:
                kw["deadline"] = SUN + timedelta(days=rng.randint(1, 9), hours=rng.randint(0, 20))
            elif roll < 0.35:
                kw["on_date"] = (SUN + timedelta(days=rng.randint(1, 6))).date()
            tasks += split_task(T(i, rng.choice([15, 30, 45, 60, 90, 150, 240]),
                                  priority=rng.choice(list(Priority)), **kw), rules)
        now = SUN + timedelta(hours=rng.randint(0, 60))
        r = schedule(tasks, tt, rules, now)
        assert_invariants(r, tasks, tt, rules)
        assert set(r.placements) | set(r.unplaced) == {t.id for t in tasks}
        for t in tasks:
            if t.id in r.placements and t.deadline and not any(
                    w.task_id == t.id and w.kind in ("rest", "heavy") for w in r.warnings):
                assert r.placements[t.id].end <= t.deadline


def test_overload_is_a_flagged_last_resort(tt):                       # noqa: F811 (replaces weak version)
    rules = Rules(max_task_minutes_per_day=120, overload_factor=1.5)
    now, deadline = datetime(2026, 10, 25, 8, 0), datetime(2026, 10, 25, 23, 59)
    tasks = [T(i, 60, deadline=deadline) for i in (1, 2, 3)]          # 180 min, cap 120
    r = schedule(tasks, tt, rules, now)
    assert not r.unplaced and any(w.kind == "heavy" for w in r.warnings)
    strict = schedule(tasks, tt, Rules(max_task_minutes_per_day=120, overload_factor=1.0), now)
    assert len(strict.unplaced) == 1                                  # no silent overload


def test_explicit_friday_evening_is_allowed_but_flagged(tt, rules):
    from uniagent.models import Block
    tt.events.append(Block(datetime(2026, 10, 30, 8, 0), datetime(2026, 10, 30, 18, 0), "Exam day", "event"))
    ask = T(2, 30, on_date=date(2026, 10, 30))                    # user insists on Friday
    r = schedule([ask], tt, rules, SUN)
    assert r.placements["2"].start >= datetime(2026, 10, 30, 18, 0)
    assert any(w.kind == "rest" and w.task_id == "2" for w in r.warnings)


def test_protected_evening_is_only_a_flagged_last_resort_for_deadlines(tt, rules):
    from uniagent.models import Block
    tt.events.append(Block(datetime(2026, 10, 30, 8, 0), datetime(2026, 10, 30, 18, 0), "Exam day", "event"))
    due_tonight = T(3, 30, deadline=datetime(2026, 10, 30, 23, 0))
    r = schedule([due_tonight], tt, rules, datetime(2026, 10, 30, 7, 0))
    # the only way to meet the deadline is the protected evening -> allowed, but never silently
    assert r.placements["3"].start >= datetime(2026, 10, 30, 18, 0)
    assert any(w.kind == "rest" and w.task_id == "3" for w in r.warnings)

    relaxed = T(4, 30, deadline=datetime(2026, 10, 31, 23, 0))        # a day later: no need to touch it
    r2 = schedule([relaxed], tt, rules, datetime(2026, 10, 30, 7, 0))
    assert r2.placements["4"].start >= datetime(2026, 10, 31, 8, 0) or r2.placements["4"].start < datetime(2026, 10, 30, 8, 0)
    assert not any(w.kind == "rest" for w in r2.warnings)


def test_moving_a_deadline_earlier_replans_the_task(tt, rules):
    t = T(1, 60, deadline=datetime(2026, 11, 6, 12, 0))
    first = schedule([t], tt, rules, SUN)
    t.placement = first.placements["1"]
    t.deadline = t.placement.start                         # now due before its slot starts
    second = schedule([t], tt, rules, SUN)
    assert "1" in second.unplaced or second.placements["1"].end <= t.deadline


def test_buffers_are_kept_around_everything(tt, rules):
    tasks = [T(i, 45) for i in range(1, 9)]
    r = schedule(tasks, tt, rules, SUN)
    gap = timedelta(minutes=rules.buffer_min)
    slots = sorted(r.placements.values(), key=lambda p: p.start)
    for a, b in zip(slots, slots[1:]):
        if a.start.date() == b.start.date():
            assert b.start - a.end >= gap
    for p in slots:
        blocks, _ = busy_blocks(tt, p.start.date(), rules)
        for blk in blocks:
            assert not overlaps(p.start - gap, p.end + gap, blk.start, blk.end)


def test_split_sessions_are_repacked_early_when_balancing_would_strand_one():
    """Regression: balancing the load pushed session 1 to tomorrow, so session 3 (which must come
    after session 2) had no day left before the deadline. The planner must undo and pack early."""
    rules = Rules(max_task_minutes_per_day=150, max_chunk_min=120, deadline_buffer_days=0)
    big = Task(id="big", title="Report", duration_min=300, seq=1, deadline=datetime(2026, 10, 28, 23, 0))
    meeting = Task(id="f", title="Meeting", duration_min=50, seq=2, kind=Kind.FIXED,
                   fixed_start=datetime(2026, 10, 26, 21, 0))
    parts = split_task(big, rules)
    r = schedule([meeting, *parts], Timetable(), rules, datetime(2026, 10, 26, 8, 0))
    assert r.unplaced == []
    slots = [r.placements[p.id] for p in parts]
    assert all(a.end < b.start for a, b in zip(slots, slots[1:]))            # still in order
    assert slots[-1].end <= big.deadline
    assert slots[0].start.date() == datetime(2026, 10, 26).date()            # packed from today


# ------------------------------------------------------------ rolling-window burnout guard
def _stretches(placements, window_min):
    """Worst amount of task time inside any stretch of window_min, per day."""
    worst = 0.0
    items = sorted(placements, key=lambda p: p.start)
    for p0 in items:
        hi = p0.start + timedelta(minutes=window_min)
        total = sum(max(0.0, (min(q.end, hi) - max(q.start, p0.start)).total_seconds()) for q in items) / 60
        worst = max(worst, total)
    return worst


SAME_DAY = dict(deadline=datetime(2026, 10, 26, 21, 0))          # forces everything into one day


def test_long_free_days_get_real_breaks():
    rules = Rules(max_task_minutes_per_day=360, max_rolling_task_min=150, rolling_window_min=240)
    tasks = [T(i, 60, **SAME_DAY) for i in range(1, 7)]                   # 6 hours of work due the same day
    r = schedule(tasks, Timetable(), rules, datetime(2026, 10, 26, 8, 0))
    assert not r.unplaced and not any(w.kind in ("heavy", "rest") for w in r.warnings)
    assert _stretches(list(r.placements.values()), 240) <= 150


def test_without_the_guard_the_same_work_is_packed_back_to_back():
    loose = Rules(max_task_minutes_per_day=360, max_rolling_task_min=360, rolling_window_min=240)
    r = schedule([T(i, 60, **SAME_DAY) for i in range(1, 7)], Timetable(), loose, datetime(2026, 10, 26, 8, 0))
    assert _stretches(list(r.placements.values()), 240) > 150


def test_the_guard_never_makes_a_possible_schedule_impossible_by_itself(tt):
    rules = Rules(max_task_minutes_per_day=240, max_rolling_task_min=150)
    tasks = [T(i, 60, deadline=datetime(2026, 10, 30, 23, 0)) for i in range(1, 6)]
    r = schedule(tasks, tt, rules, SUN)
    assert not r.unplaced


def test_config_rejects_a_guard_smaller_than_one_work_session():
    from uniagent.config import ConfigError
    with pytest.raises(ConfigError, match="rolling"):
        Rules(max_chunk_min=120, max_rolling_task_min=90)
