"""Apply validated commands: create/change/finish tasks, vacations, cancelled classes, events.

Write semantics are *at most once*: if one command fails (e.g. Notion hiccup) it is reported and
skipped - never silently retried, because a retry could create a duplicate. Authentication errors
are the exception: they abort the whole run.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from .config import Rules
from .interpreter import Command
from .models import Block, Kind, Priority, Status, Task, TaskType
from .notion import NotionAuthError
from .notion_store import NotionStore, new_key
from .scheduler import split_task
from .timetable import AwayPeriod, Timetable

log = logging.getLogger("uniagent.actions")
_PART_SUFFIX = re.compile(r"\s*\((\d+)/(\d+)\)\s*$")


@dataclass
class ApplyResult:
    added: list[Task] = field(default_factory=list)
    updated: list[Task] = field(default_factory=list)
    completed: list[Task] = field(default_factory=list)
    cancelled: list[Task] = field(default_factory=list)
    check_ins: list[tuple[Task, str]] = field(default_factory=list)        # (daily/habit, "up"/"down")
    away_added: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    classes_cancelled: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)
    events_added: list[str] = field(default_factory=list)
    agenda: list[tuple[date, int]] = field(default_factory=list)
    replan_all: bool = False
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    per_message: dict[int, list[str]] = field(default_factory=dict)       # update id -> what happened

    @property
    def needs_replan(self) -> bool:
        return bool(self.added or self.updated or self.completed or self.cancelled or self.away_added
                    or self.classes_cancelled or self.events_added or self.replan_all)


def _title_base(title: str) -> str:
    return _PART_SUFFIX.sub("", title).strip()


def _retitle(old: str, new: str) -> str:
    m = _PART_SUFFIX.search(old)
    return f"{new.strip()} ({m.group(1)}/{m.group(2)})" if m else new.strip()


def apply_commands(commands: Sequence[Command], tasks: list[Task], tt: Timetable, store: NotionStore,
                   rules: Rules, now: datetime, sources: Mapping[int, str] | None = None) -> ApplyResult:
    out = ApplyResult()
    sources = sources or {}
    for cmd in commands:
        log_line: str | None = None
        try:
            log_line = _apply_one(cmd, tasks, tt, store, rules, now, out, sources.get(cmd.message, ""))
        except NotionAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad command must not sink the others
            out.errors.append(f"Couldn't {cmd.op.replace('_', ' ')}: {exc}")
            log.warning("command %s failed (%s)", cmd.op, type(exc).__name__)
        if log_line:
            out.per_message.setdefault(cmd.message, []).append(log_line)
        if cmd.note:
            out.notes.append(cmd.note)
    return out


def _apply_one(cmd: Command, tasks: list[Task], tt: Timetable, store: NotionStore, rules: Rules,
               now: datetime, out: ApplyResult, source: str) -> str | None:
    if cmd.op == "add_task":
        return _add_task(cmd, tasks, store, rules, out, source)
    if cmd.op == "update_task":
        return _update_task(cmd, tasks, store, rules, out, source)
    if cmd.op in ("complete_task", "cancel_task", "check_in"):
        return _finish_tasks(cmd, store, out)
    if cmd.op == "add_away":
        assert cmd.start and cmd.end
        if any(a.start == cmd.start and a.end == cmd.end for a in tt.away):
            out.notes.append(f"'{cmd.label}' was already noted.")
            return None
        period = AwayPeriod(cmd.start, cmd.end, cmd.load if cmd.load is not None else rules.away_load_factor,
                            cmd.label or "Away")
        store.add_away(period.label, period.start, period.end, period.load_factor)
        tt.away.append(period)
        out.away_added.append((period.label, period.start, period.end))
        return f"away {period.start:%a %d %b %H:%M} -> {period.end:%a %d %b %H:%M}"
    if cmd.op == "cancel_class":
        course = cmd.course or "*"
        for d in cmd.dates:
            tt.cancelled.append((d, course))
        first, last = cmd.dates[0], cmd.dates[-1]
        store.add_exception("cancel", "No classes" if course == "*" else f"{course} off", first,
                            end=last if last != first else None, course="" if course == "*" else course)  # type: ignore[arg-type]
        out.classes_cancelled.append((course, cmd.dates))
        return f"class off {first:%a %d %b}" + (f" - {last:%a %d %b}" if last != first else "")
    if cmd.op == "add_event":
        assert cmd.start and cmd.end and cmd.title
        store.add_exception("event", cmd.title, cmd.start, cmd.end)
        tt.events.append(Block(cmd.start, cmd.end, cmd.title, "event"))
        out.events_added.append(cmd.title)
        return f"event {cmd.title}"
    if cmd.op == "show_agenda":
        out.agenda.append((cmd.dates[0], cmd.days))
        return None
    if cmd.op == "replan_all":
        out.replan_all = True
        return "replan"
    return None


def _add_task(cmd: Command, tasks: list[Task], store: NotionStore, rules: Rules, out: ApplyResult,
              source: str) -> str:
    ttype = cmd.task_type or TaskType.TODO
    fixed = ttype is TaskType.TODO and cmd.at is not None
    task = Task(
        id=new_key(), title=cmd.title or "Untitled",
        duration_min=cmd.minutes or (60 if fixed else 20 if ttype is TaskType.DAILY else 30),
        kind=Kind.FIXED if fixed else Kind.FLEXIBLE, type=ttype,
        priority=cmd.priority or Priority.NORMAL,
        fixed_start=cmd.at if fixed else None, on_date=cmd.on_date, not_before=cmd.not_before,
        deadline=cmd.deadline, notes=cmd.notes or "",
        repeat_days=cmd.repeat_days or () if ttype is TaskType.DAILY else (),
        daily_time=cmd.daily_time if ttype is TaskType.DAILY else None,
        daily_time_fixed=ttype is TaskType.DAILY and cmd.daily_time is not None,
        habit_up=True if cmd.habit_up is None else cmd.habit_up, habit_down=bool(cmd.habit_down),
        habit_reset=cmd.habit_reset or "daily")
    parts = split_task(task, rules)
    store.add_tasks(parts, source=source)
    tasks.extend(parts)
    out.added.extend(parts)
    return f"added {task.title}" + (f" ({len(parts)} sessions)" if len(parts) > 1 else "")


def _update_task(cmd: Command, tasks: list[Task], store: NotionStore, rules: Rules, out: ApplyResult,
                 source: str) -> str | None:
    group = cmd.targets
    if cmd.minutes is not None and len(group) > 1:
        return _retotal(cmd, group, tasks, store, rules, out, source)
    names = []
    for task in group:
        changes: dict = {}
        if cmd.new_title:
            changes["title"] = _retitle(task.title, cmd.new_title)
        if cmd.priority is not None:
            changes["priority"] = cmd.priority
        if cmd.minutes is not None:
            changes["duration_min"] = cmd.minutes
        if cmd.task_type is not None and cmd.task_type is not task.type:
            changes.update(_conversion(task, cmd))
        else:
            changes.update(_field_changes(task, cmd))
        for name in cmd.clear:
            if name == "deadline":
                changes["deadline"] = None
            elif name == "on_date":
                changes["on_date"] = None
            elif name == "not_before":
                changes["not_before"] = None
            elif name == "at" and task.kind is Kind.FIXED:
                changes.update(kind=Kind.FLEXIBLE, fixed_start=None)
            elif name == "daily_time" and task.type is TaskType.DAILY:
                changes.update(daily_time=None, daily_time_fixed=False)
        if not changes:
            continue
        if "placement" not in changes and set(changes) & {"on_date", "deadline", "not_before", "duration_min",
                                                          "kind", "fixed_start"}:
            changes["placement"] = None                    # the old slot may no longer be valid
        new = dataclasses.replace(task, **changes)         # re-runs the Task validation
        task.__dict__.update(vars(new))
        store.update_task(task)
        out.updated.append(task)
        names.append(task.title)
    return f"updated {_title_base(group[0].title)}" if names else None


def _field_changes(task: Task, cmd: Command) -> dict:
    changes: dict = {}
    if task.type is TaskType.TODO:
        if cmd.at is not None:
            changes.update(kind=Kind.FIXED, fixed_start=cmd.at, on_date=None)
        elif cmd.on_date is not None:
            changes.update(on_date=cmd.on_date)
            if task.kind is Kind.FIXED:                    # "do it Friday" (no time) frees a fixed appointment
                changes.update(kind=Kind.FLEXIBLE, fixed_start=None)
        if cmd.deadline is not None:
            changes["deadline"] = cmd.deadline
        if cmd.not_before is not None:
            changes["not_before"] = cmd.not_before
    elif task.type is TaskType.DAILY:
        if cmd.repeat_days is not None:
            changes["repeat_days"] = cmd.repeat_days
        if cmd.daily_time is not None:
            changes.update(daily_time=cmd.daily_time, daily_time_fixed=True)
    else:
        if cmd.habit_up is not None or cmd.habit_down is not None:
            up = task.habit_up if cmd.habit_up is None else cmd.habit_up
            down = task.habit_down if cmd.habit_down is None else cmd.habit_down
            changes.update(habit_up=up or not down, habit_down=down)
        if cmd.habit_reset in ("daily", "weekly", "monthly"):
            changes["habit_reset"] = cmd.habit_reset
    if cmd.notes is not None:
        changes["notes"] = cmd.notes
    return changes


def _conversion(task: Task, cmd: Command) -> dict:
    """Change a task's type ("make it a daily"). Date fields don't apply to dailies/habits."""
    new_type = cmd.task_type
    base = dict(type=new_type, kind=Kind.FLEXIBLE, fixed_start=None, on_date=None, deadline=None,
                not_before=None, placement=None, repeat_days=(), daily_time=None, daily_time_fixed=False,
                habit_up=True, habit_down=False)
    if new_type is TaskType.DAILY:
        base.update(repeat_days=cmd.repeat_days or (), daily_time=cmd.daily_time,
                    daily_time_fixed=cmd.daily_time is not None)
    elif new_type is TaskType.HABIT:
        base.update(habit_up=True if cmd.habit_up is None else cmd.habit_up, habit_down=bool(cmd.habit_down))
    else:
        base.update(deadline=cmd.deadline, on_date=cmd.on_date, not_before=cmd.not_before)
    return base


def _retotal(cmd: Command, group: list[Task], tasks: list[Task], store: NotionStore, rules: Rules,
             out: ApplyResult, source: str) -> str:
    """"I only need 2 hours for it": replace a split task's sessions with a freshly split total."""
    first = sorted(group, key=lambda t: t.id)[0]
    for t in group:
        store.set_status(t, Status.CANCELLED)
        out.cancelled.append(t)
    replacement = Task(
        id=new_key(), title=cmd.new_title or _title_base(first.title), duration_min=cmd.minutes or first.duration_min,
        priority=cmd.priority or first.priority, on_date=cmd.on_date if cmd.on_date else first.on_date,
        not_before=cmd.not_before or first.not_before, deadline=cmd.deadline or first.deadline,
        notes=first.notes, seq=first.seq)
    parts = split_task(replacement, rules)
    store.add_tasks(parts, source=source)
    tasks.extend(parts)
    out.added.extend(parts)
    return f"re-planned {replacement.title} ({replacement.duration_min} min)"


def _finish_tasks(cmd: Command, store: NotionStore, out: ApplyResult) -> str | None:
    lines = []
    for task in cmd.targets:
        routine = task.type is not TaskType.TODO
        if cmd.op == "check_in" or (cmd.op == "complete_task" and routine):
            if routine:
                out.check_ins.append((task, cmd.direction))
                lines.append(f"checked in {task.title}")
                continue
            store.set_status(task, Status.DONE)            # check_in on a to-do means "I finished it"
            out.completed.append(task)
            lines.append(f"done {task.title}")
        elif cmd.op == "complete_task":
            store.set_status(task, Status.DONE)
            out.completed.append(task)
            lines.append(f"done {task.title}")
        else:
            store.set_status(task, Status.CANCELLED)
            out.cancelled.append(task)
            lines.append(f"cancelled {task.title}")
    return "; ".join(lines) or None
