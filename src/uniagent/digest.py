"""Messages the bot sends on its own: tomorrow/today previews and the weekly review.

Each kind is sent at most once per day, only within a few hours after its time (so a delayed
run never sends a stale "tomorrow" at 3 a.m.), and is skipped when there is nothing to say.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from .config import Rules
from .models import Task, TaskType
from .replies import DAY_NAMES, format_agenda, slot
from .scheduler import ScheduleResult
from .timetable import Timetable, busy_blocks

WINDOW = timedelta(hours=3)
KINDS = ("morning", "evening", "weekly")


def _base(title: str) -> str:
    return title.rsplit(" (", 1)[0] if title.endswith(")") and "/" in title.rsplit(" (", 1)[-1] else title


def _is_due(now: datetime, at: time | None, last: str | None) -> bool:
    if at is None:
        return False
    start = datetime.combine(now.date(), at)
    return start <= now < start + WINDOW and last != now.date().isoformat()


def due_digests(now: datetime, rules: Rules, sent: Mapping[str, str]) -> list[str]:
    due = []
    if _is_due(now, rules.digest_morning, sent.get("morning")):
        due.append("morning")
    if _is_due(now, rules.digest_evening, sent.get("evening")):
        due.append("evening")
    if rules.weekly_day is not None and now.weekday() == rules.weekly_day and _is_due(now, rules.weekly_time, sent.get("weekly")):
        due.append("weekly")
    return due


def upcoming_deadlines(tasks: Sequence[Task], plan: ScheduleResult, now: datetime, days: int) -> list[str]:
    horizon = now + timedelta(days=days)
    groups: dict[str, list[Task]] = {}
    for t in tasks:
        if t.active and t.type is TaskType.TODO and t.deadline and now <= t.deadline <= horizon:
            groups.setdefault(t.group or t.id, []).append(t)
    lines = []
    for parts in sorted(groups.values(), key=lambda g: g[0].deadline):
        slots = [plan.placements[t.id] for t in parts if t.id in plan.placements]
        when = ""
        if slots:
            first = min(slots, key=lambda p: p.start)
            when = f" (planned {slot(first)}" + (f", {len(parts)} sessions" if len(parts) > 1 else "") + ")"
        else:
            when = " (not scheduled yet!)"
        lines.append(f"  {parts[0].deadline:%a %d %b %H:%M}  {_base(parts[0].title)}{when}")
    return lines


def _day_is_empty(day: date, tasks: Sequence[Task], plan: ScheduleResult, tt: Timetable, rules: Rules) -> bool:
    if busy_blocks(tt, day, rules)[0]:
        return False
    if any(p.start.date() == day for p in plan.placements.values()):
        return False
    return not any(t.active and t.type is TaskType.DAILY and t.repeats_on(day) and (plan.daily_times.get(t.id) or t.daily_time)
                   for t in tasks)


def preview(kind: str, now: datetime, tasks: Sequence[Task], plan: ScheduleResult, tt: Timetable, rules: Rules,
            force: bool = False) -> str | None:
    """"morning" -> today, "evening" -> tomorrow. None when there is nothing worth saying."""
    day = now.date() + timedelta(days=0 if kind == "morning" else 1)
    ahead = upcoming_deadlines(tasks, plan, now, 2 if kind == "morning" else 3)
    if _day_is_empty(day, tasks, plan, tt, rules) and not ahead and not force:
        return None
    head = "Good morning - today:" if kind == "morning" else "Tomorrow:"
    parts = [head, format_agenda(day, 1, tasks, plan, tt, rules)]
    if ahead:
        parts += ["", "Due soon:"] + ahead
    return "\n".join(parts)


def weekly_review(now: datetime, tasks: Sequence[Task], done: Sequence[Task], cancelled: Sequence[Task],
                  plan: ScheduleResult, tt: Timetable, rules: Rules) -> str:
    def titles(items: Sequence[Task]) -> list[str]:
        return list(dict.fromkeys(_base(t.title) for t in items))

    week_start = now.date() - timedelta(days=6)
    lines = [f"Weekly review ({week_start:%a %d %b} - {now.date():%a %d %b})"]
    finished = titles(done)
    lines.append(f"Done ({len(finished)}): " + (", ".join(finished[:8]) + (" ..." if len(finished) > 8 else "")) if finished
                 else "Done: nothing marked done this week")
    if cancelled:
        lines.append(f"Cancelled: {len(titles(cancelled))}")
    open_todos = [t for t in tasks if t.active and t.type is TaskType.TODO]
    unscheduled = [t for t in open_todos if t.id not in plan.placements]
    lines.append(f"Still open: {len(titles(open_todos))} to-dos" + (f" ({len(titles(unscheduled))} not scheduled)" if unscheduled else ""))

    lines += ["", "Next 7 days:"]
    full: list[str] = []
    for i in range(1, 8):
        d = now.date() + timedelta(days=i)
        blocks, _ = busy_blocks(tt, d, rules)
        classes = sum(1 for b in blocks if b.kind == "class")
        mins = sum(p.minutes for p in plan.placements.values() if p.start.date() == d)
        mins += sum(t.duration_min for t in tasks if t.active and t.type is TaskType.DAILY and t.repeats_on(d)
                    and (plan.daily_times.get(t.id) or t.daily_time))
        cap = rules.task_cap(d.weekday())
        away = " (away)" if any(a.start.date() <= d <= a.end.date() for a in tt.away) else ""
        lines.append(f"  {DAY_NAMES[d.weekday()]} {d:%d %b}: {classes} classes, {mins}/{cap} min of tasks{away}")
        if mins >= cap * 0.9 and not away:
            full.append(f"{DAY_NAMES[d.weekday()]} {d:%d %b}")
    ahead = upcoming_deadlines(tasks, plan, now, 7)
    if ahead:
        lines += ["", "Deadlines this coming week:"] + ahead
    if full:
        lines += ["", "Heads up - nearly full: " + ", ".join(full) + ". Say so if you want something moved."]
    return "\n".join(lines)
