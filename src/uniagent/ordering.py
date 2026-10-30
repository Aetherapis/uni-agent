"""Turn a schedule into what Habitica should show, and plan the reordering.

Habitica to-dos only have a due DATE, so the time of day goes in the title
("16:00-17:00 Lab report") and the list order does the rest: soonest at the top.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from .config import Rules
from .models import Placement, Priority, Task, TaskType, alias_safe, slug
from .timetable import Timetable, class_blocks

UNSCHEDULED_PREFIX = "(unscheduled) "

# Habitica "priority" (difficulty) values: trivial 0.1, easy 1, medium 1.5, hard 2
HABITICA_DIFFICULTY = {
    Priority.URGENT: 2.0,
    Priority.HIGH: 1.5,
    Priority.NORMAL: 1.0,
    Priority.LOW: 0.1,
}


@dataclass(frozen=True)
class HabiticaItem:
    key: str                      # stable id; used as the Habitica task "alias"
    title: str
    due: date | None
    start: datetime | None
    notes: str
    difficulty: float
    group: int                    # 0 = has a time slot, 1 = unscheduled (goes last)
    kind: str = "todo"            # "todo" | "daily" | "habit"  -> which Habitica list it lives in
    repeat_days: tuple[int, ...] = ()     # daily only (empty = every day)
    up: bool = True               # habit only
    down: bool = False            # habit only
    reset: str = "daily"          # habit only: counter reset

    @property
    def sort_key(self) -> tuple:
        # dailies sort by time of day, habits alphabetically, to-dos by (date, time)
        return (self.group, self.due or date.max, self.start or datetime.max, -self.difficulty,
                self.title.lower() if self.kind == "habit" else "", self.key)


def task_key(task_id: str) -> str:
    return "t-" + alias_safe(task_id)


def time_label(p: Placement) -> str:
    return f"{p.start:%H:%M}-{p.end:%H:%M}"


_DAILY_EPOCH = date(2000, 1, 1)


def build_items(
    tasks: Sequence[Task],
    placements: Mapping[str, Placement],
    tt: Timetable,
    rules: Rules,
    now: datetime,
    daily_times: Mapping[str, time] | None = None,
) -> list[HabiticaItem]:
    """What Habitica *should* contain right now.

    One flat list in display order within each kind. Use ``items_by_kind`` to split it into
    the three Habitica lists (to-dos, dailies, habits) - each list has its own order.
    """
    horizon_end = now.date() + timedelta(days=rules.habitica_horizon_days)
    daily_times = daily_times or {}
    items: list[HabiticaItem] = []

    for t in tasks:
        if not t.active:
            continue
        if t.type is TaskType.HABIT:
            items.append(HabiticaItem(
                key=task_key(t.id), title=t.title, due=None, start=None, notes=t.notes.strip(),
                difficulty=HABITICA_DIFFICULTY[t.priority], group=0, kind="habit",
                up=t.habit_up, down=t.habit_down, reset=t.habit_reset))
            continue
        if t.type is TaskType.DAILY:
            tod = daily_times.get(t.id) or t.daily_time
            if tod is not None:
                end = (datetime.combine(_DAILY_EPOCH, tod) + timedelta(minutes=t.duration_min)).time()
                title, start, group = f"{tod:%H:%M}-{end:%H:%M} {t.title}", datetime.combine(_DAILY_EPOCH, tod), 0
            else:
                title, start, group = t.title, None, 1
            items.append(HabiticaItem(
                key=task_key(t.id), title=title, due=None, start=start, notes=t.notes.strip(),
                difficulty=HABITICA_DIFFICULTY[t.priority], group=group, kind="daily",
                repeat_days=t.repeat_days))
            continue
        p = placements.get(t.id)
        notes = t.notes.strip()
        if t.deadline is not None:
            notes = (notes + "\n" if notes else "") + f"Deadline: {t.deadline:%a %d %b %H:%M}"
        if p is not None:
            if p.start.date() > horizon_end:
                continue                       # too far ahead; appears once it comes within range
            items.append(HabiticaItem(
                key=task_key(t.id),
                title=f"{time_label(p)} {t.title}",
                due=p.start.date(), start=p.start, notes=notes,
                difficulty=HABITICA_DIFFICULTY[t.priority], group=0))
        else:
            items.append(HabiticaItem(
                key=task_key(t.id),
                title=f"{UNSCHEDULED_PREFIX}{t.title}",
                due=t.deadline.date() if t.deadline else None, start=None, notes=notes,
                difficulty=HABITICA_DIFFICULTY[t.priority], group=1))

    if rules.include_classes_in_habitica:
        for offset in range(rules.class_lookahead_days + 1):
            day = now.date() + timedelta(days=offset)
            for b in class_blocks(tt, day):
                course_kind = b.label.split(" @ ")[0]
                key = f"c-{day:%Y%m%d}-{slug(course_kind)}-{b.start:%H%M}"
                items.append(HabiticaItem(
                    key=key, title=f"{b.start:%H:%M}-{b.end:%H:%M} {b.label}",
                    due=day, start=b.start, notes="Class (from your timetable)",
                    difficulty=0.1, group=0))

    return sorted(items, key=lambda i: (i.kind, i.sort_key))


def items_by_kind(items: Sequence[HabiticaItem]) -> dict[str, list[HabiticaItem]]:
    """Split into Habitica's three lists; each stays in display order (top = first)."""
    out: dict[str, list[HabiticaItem]] = {"todo": [], "daily": [], "habit": []}
    for item in items:
        out[item.kind].append(item)
    return out


Move = tuple[str, int]


def plan_moves(current: Sequence[str], desired_front: Sequence[str]) -> list[Move]:
    """Fewest moves to put ``desired_front`` (in order) at the top of ``current``.

    ``current`` is Habitica's full to-do order. Items we don't manage keep their
    relative order underneath ours. Each move is ``(id, position)`` where position
    is the index the item should have *after it has been removed from its old spot*
    (so 0 = top). Applying the moves in order yields the final list.
    """
    cur = list(current)
    present = set(cur)
    front: list[str] = []
    for k in desired_front:
        if k in present and k not in front:
            front.append(k)
    front_set = set(front)
    target = front + [k for k in cur if k not in front_set]
    rank = {k: i for i, k in enumerate(target)}

    keep = _lis_indices([rank[k] for k in cur])
    settled = {cur[i] for i in keep}

    moves: list[Move] = []
    for k in target:
        if k in settled:
            continue
        cur.remove(k)
        pred_idx = -1
        for j, x in enumerate(cur):
            if x in settled and rank[x] < rank[k]:
                pred_idx = j
        pos = pred_idx + 1
        cur.insert(pos, k)
        settled.add(k)
        moves.append((k, pos))
    return moves


def apply_moves(order: Sequence[str], moves: Sequence[Move]) -> list[str]:
    """Simulate moves on a list (used by tests and for dry runs)."""
    cur = list(order)
    for k, pos in moves:
        cur.remove(k)
        cur.insert(pos, k)
    return cur


def _lis_indices(seq: Sequence[int]) -> list[int]:
    """Indices of one longest strictly increasing subsequence (n is small -> O(n^2))."""
    n = len(seq)
    if n == 0:
        return []
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
                prev[i] = j
    end = max(range(n), key=lambda i: best[i])
    out: list[int] = []
    while end != -1:
        out.append(end)
        end = prev[end]
    return out[::-1]
