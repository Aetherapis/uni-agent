"""Everything the bot says back. Pure functions, plain text, phone-friendly.

Facts (times, what moved, what didn't fit) come from the scheduler's real result - never from
the language model - so the bot cannot claim something was scheduled when it wasn't.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable, Sequence

from .actions import ApplyResult
from .config import Rules
from .interpreter import Interpretation
from .models import Placement, Task, TaskType
from .scheduler import ScheduleResult
from .timetable import Timetable, busy_blocks

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
IMPORTANT = ("no_slot", "overlap", "rest", "heavy", "away", "daily")      # worth a proactive heads-up
MAX_LINES = 40


def slot(p: Placement) -> str:
    return f"{p.start:%a %d %b %H:%M}-{p.end:%H:%M}"


def _base(title: str) -> str:
    return title.rsplit(" (", 1)[0] if title.endswith(")") and "/" in title.rsplit(" (", 1)[-1] else title


def _groups(tasks: Iterable[Task]) -> list[list[Task]]:
    order: dict[str, list[Task]] = defaultdict(list)
    for t in tasks:
        order[t.group or t.id].append(t)
    return [sorted(v, key=lambda t: t.id) for v in order.values()]


def _days(repeat: tuple[int, ...]) -> str:
    return "every day" if not repeat else ", ".join(DAY_NAMES[d] for d in repeat)


def describe(group: list[Task], plan: ScheduleResult, tasks_by_id: dict[str, Task] | None = None) -> str:
    """One line about where a task (or all sessions of a split task) ended up."""
    t = group[0]
    if t.type is TaskType.DAILY:
        tod = plan.daily_times.get(t.id) or t.daily_time
        return f"daily at {tod:%H:%M} ({_days(t.repeat_days)})" if tod else f"daily ({_days(t.repeat_days)}) - no time found"
    if t.type is TaskType.HABIT:
        return "habit (" + ("+" if t.habit_up else "") + ("-" if t.habit_down else "") + ")"
    slots = [plan.placements.get(x.id) for x in group]
    placed = [s for s in slots if s]
    if not placed:
        return "no free slot found"
    if len(group) == 1:
        return ("fixed " if t.kind.value == "fixed" else "") + slot(placed[0]) + (
            f", due {t.deadline:%a %d %b}" if t.deadline else "")
    first, last = min(placed, key=lambda p: p.start), max(placed, key=lambda p: p.start)
    mins = round(sum(x.duration_min for x in group) / len(group))
    missing = f" ({len(group) - len(placed)} not placed)" if len(placed) < len(group) else ""
    return (f"{len(group)} sessions of ~{mins} min, {first.start:%a %d %b %H:%M} to "
            f"{last.start:%a %d %b %H:%M}{missing}")


def task_warnings(plan: ScheduleResult, ids: set[str]) -> list[str]:
    seen = dict.fromkeys(w.message for w in plan.warnings if w.task_id in ids and w.kind != "long")
    return list(seen)


def compose_reply(interp: Interpretation, applied: ApplyResult, tasks: Sequence[Task], plan: ScheduleResult,
                  tt: Timetable, rules: Rules, now: datetime) -> str:
    by_id = {t.id: t for t in tasks}
    lines: list[str] = []
    if interp.ack:
        lines.append(interp.ack)

    touched: set[str] = set()
    for group in _groups(applied.added):
        live = [by_id.get(t.id, t) for t in group]
        touched |= {t.id for t in group}
        title = _base(group[0].title)
        mark = "!" if all(plan.placements.get(t.id) is None for t in live) and live[0].type is TaskType.TODO else "+"
        lines.append(f"{mark} {title} - {describe(live, plan)}")
    for group in _groups(t for t in applied.updated if t.id not in touched):
        live = [by_id.get(t.id, t) for t in group]
        touched |= {t.id for t in group}
        lines.append(f"~ {_base(group[0].title)} - now {describe(live, plan)}")
    for t in applied.completed:
        lines.append(f"Done: {_base(t.title)}")
    for t in applied.cancelled:
        if t.id not in touched:
            lines.append(f"Cancelled: {_base(t.title)}")
    for t, direction in applied.check_ins:
        lines.append(f"{'+1' if direction == 'up' else '-1'} {t.title}")
    for label, start, end in applied.away_added:
        lines.append(f"Away ({label}): {start:%a %d %b %H:%M} -> {end:%a %d %b %H:%M} - I'll keep your workload light")
    for course, dates in applied.classes_cancelled:
        what = "classes" if course == "*" else f"{course}"
        span = f"{dates[0]:%a %d %b}" + (f" - {dates[-1]:%a %d %b}" if len(dates) > 1 else "")
        lines.append(f"No {what} on {span}")
    for title in applied.events_added:
        lines.append(f"Event added: {title}")

    for w in task_warnings(plan, touched):
        lines.append(f"Heads up: {w}")
    lines += [f"Note: {n}" for n in dict.fromkeys(applied.notes)]
    lines += [f"Couldn't do: {e}" for e in interp.errors + applied.errors]
    lines += [f"Question: {q}" for q in interp.questions]

    if not lines or (lines == [interp.ack] and not applied.agenda):
        lines.append("I didn't find anything to schedule in that.")
    for day, count in applied.agenda:
        lines += ["", format_agenda(day, count, tasks, plan, tt, rules)]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... and {len(lines) - MAX_LINES} more lines"]
    return "\n".join(lines)


def heads_up(plan: ScheduleResult) -> list[str]:
    """Proactive warnings worth sending even without a new message."""
    return list(dict.fromkeys(w.message for w in plan.warnings if w.kind in IMPORTANT))[:6]


def format_agenda(day: date, days: int, tasks: Sequence[Task], plan: ScheduleResult, tt: Timetable,
                  rules: Rules) -> str:
    by_id = {t.id: t for t in tasks}
    out: list[str] = []
    for i in range(days):
        d = day + timedelta(days=i)
        blocks, _ = busy_blocks(tt, d, rules)
        rows: list[tuple[datetime, str]] = []
        classes = 0
        for b in blocks:
            if b.kind == "class":
                classes += 1
            rows.append((b.start, f"{b.start:%H:%M}-{b.end:%H:%M}  {b.label}"))
        task_min = 0
        for tid, p in plan.placements.items():
            t = by_id.get(tid)
            if t is not None and p.start.date() == d:
                task_min += p.minutes
                rows.append((p.start, f"{p.start:%H:%M}-{p.end:%H:%M}  [ ] {t.title}"))
        for t in tasks:
            if t.type is TaskType.DAILY and t.active and t.repeats_on(d):
                tod = plan.daily_times.get(t.id) or t.daily_time
                if tod:
                    start = datetime.combine(d, tod)
                    task_min += t.duration_min
                    rows.append((start, f"{tod:%H:%M}-{(start + timedelta(minutes=t.duration_min)):%H:%M}  [ ] {t.title} (daily)"))
        tags = [f"{classes} classes" if classes else "no classes"]
        if any(a.start.date() <= d <= a.end.date() for a in tt.away):
            tags.append("away")
        if task_min:
            tags.append(f"{task_min} min of tasks")
        out.append(f"{d:%A %d %b} ({', '.join(tags)})")
        out += [f"  {text}" for _, text in sorted(rows)] or ["  nothing planned"]
    return "\n".join(out)


def format_status(tasks: Sequence[Task], plan: ScheduleResult, now: datetime, pending: int = 0,
                  last_sync: datetime | None = None) -> str:
    active = [t for t in tasks if t.active]
    todos = [t for t in active if t.type is TaskType.TODO]
    upcoming = sorted(((plan.placements[t.id], t) for t in todos if t.id in plan.placements
                       and plan.placements[t.id].end > now), key=lambda x: x[0].start)[:3]
    lines = [f"{len(todos)} to-dos, {sum(t.type is TaskType.DAILY for t in active)} dailies, "
             f"{sum(t.type is TaskType.HABIT for t in active)} habits"]
    if upcoming:
        lines.append("Next up:")
        lines += [f"  {slot(p)}  {t.title}" for p, t in upcoming]
    unplaced = [t for t in todos if t.id not in plan.placements]
    if unplaced:
        lines.append(f"Not scheduled ({len(unplaced)}): " + ", ".join(_base(t.title) for t in unplaced[:5]))
    if pending:
        lines.append(f"{pending} message(s) waiting to be processed")
    lines.append(f"Habitica last synced: {last_sync:%a %d %b %H:%M}" if last_sync else "Habitica not synced yet")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "Send me tasks as text or voice notes, for example:\n"
        "- \"remind me to email the TA tomorrow\"\n"
        "- \"physics report due Tuesday 5pm, about 4 hours\"\n"
        "- \"revise German every Sunday and Tuesday\"\n"
        "- \"drink more water\" (a habit)\n"
        "- \"move the report to Friday\" / \"I finished the quiz prep\"\n"
        "- \"I'm away from Wednesday after uni until late Friday\"\n"
        "- \"no physics lecture on the 8th\"\n\n"
        "Commands: /agenda (today), /agenda tomorrow, /agenda 3, /digest (tomorrow's plan), /review (weekly review),\n"
        "/status, /replan, /sync, /help")
