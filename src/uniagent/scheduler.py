"""The scheduler: plain, deterministic code (no LLM involved).

Design goals
* Never double-book: classes, buses, fixed tasks, dailies and flexible tasks never overlap.
* Burnout protection: daily caps, buffers, protected evenings, nothing outside day hours,
  and far fewer tasks while you're away (vacation periods).
* Stability: a task that already has a valid slot is NOT moved when something else changes.
* Honesty: if something can't fit, say so (``warnings`` / ``unplaced``) instead of
  silently breaking a rule or a deadline.

Task types: one-time to-dos get a dated slot; DAILY tasks get one stable time of day that
is reserved on every day they repeat; HABITS are not scheduled at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Sequence

from .config import Rules
from .models import Block, Kind, Placement, Priority, Task, TaskType, overlaps
from .timetable import AwayPeriod, Timetable, away_overlapping, busy_blocks

# Cost weights used when choosing between candidate days (lower cost wins).
W_TASK_LOAD = 3.0      # how full the day's task budget gets
W_TOTAL_LOAD = 1.5     # how full the day gets overall (classes + tasks)
AWAY_PENALTY = 1.0     # prefer slots outside vacation periods when there is a choice
DAY_DELAY_WEIGHT = {   # per day of waiting: urgent things go ASAP, low priority spreads out
    Priority.URGENT: 10.0,
    Priority.HIGH: 3.0,
    Priority.NORMAL: 1.0,
    Priority.LOW: 0.5,
}
IMPLICIT_DUE_DAYS = {  # tasks without a deadline still get a soft "due" for ordering
    Priority.URGENT: 1,
    Priority.HIGH: 3,
    Priority.NORMAL: 7,
    Priority.LOW: 14,
}


@dataclass(frozen=True)
class Conflict:
    kind: str                  # overlap | no_slot | tight | rest | heavy | away | commute | long | past_date | daily
    message: str
    task_id: str | None = None
    severity: str = "warn"     # "info" | "warn"


@dataclass(frozen=True)
class DayLoad:
    class_min: int
    task_min: int
    away_min: int = 0


@dataclass
class ScheduleResult:
    placements: dict[str, Placement] = field(default_factory=dict)  # one-time task id -> slot
    daily_times: dict[str, time] = field(default_factory=dict)      # daily task id -> time of day
    unplaced: list[str] = field(default_factory=list)               # task ids with no slot
    warnings: list[Conflict] = field(default_factory=list)
    day_loads: dict[date, DayLoad] = field(default_factory=dict)


@dataclass
class _Day:
    date: date
    fixed: list[Block]
    class_min: int
    tasks: list[tuple[datetime, datetime]] = field(default_factory=list)
    task_min: int = 0
    away_min: int = 0


def ceil_to_step(dt: datetime, step_min: int) -> datetime:
    minutes = dt.hour * 60 + dt.minute + (1 if (dt.second or dt.microsecond) else 0)
    aligned = -(-minutes // step_min) * step_min
    return datetime(dt.year, dt.month, dt.day) + timedelta(minutes=aligned)


def effective_minutes(duration_min: int, rules: Rules) -> int:
    step = rules.slot_step_min
    return -(-duration_min // step) * step


def split_task(task: Task, rules: Rules) -> list[Task]:
    """Split a long one-time task into balanced sessions (call once, when creating it).

    Returns ``[task]`` unchanged when it is short enough, fixed, a daily or a habit.
    """
    if (task.type is not TaskType.TODO or task.kind is Kind.FIXED
            or task.duration_min <= rules.max_chunk_min):
        return [task]
    step = rules.slot_step_min
    units = -(-task.duration_min // step)
    n = -(-task.duration_min // rules.max_chunk_min)
    q, r = divmod(units, n)
    sizes = [(q + 1) * step if i < r else q * step for i in range(n)]
    parts: list[Task] = []
    prev_id: str | None = None
    for i, size in enumerate(sizes, start=1):
        part = Task(
            id=f"{task.id}.{i:02d}",
            title=f"{task.title} ({i}/{n})",
            duration_min=size,
            kind=task.kind,
            status=task.status,
            priority=task.priority,
            on_date=task.on_date,
            not_before=task.not_before,
            deadline=task.deadline,
            after_id=prev_id,
            group=task.id,
            notes=task.notes,
            seq=task.seq,
        )
        parts.append(part)
        prev_id = part.id
    return parts


def schedule(tasks: Sequence[Task], tt: Timetable, rules: Rules, now: datetime) -> ScheduleResult:
    return _Planner(tasks, tt, rules, now).run()


class _Planner:
    def __init__(self, tasks: Sequence[Task], tt: Timetable, rules: Rules, now: datetime) -> None:
        self.tasks = [t for t in tasks if t.active]
        self.tt = tt
        self.rules = rules
        self.now = now.replace(microsecond=0)
        self.today = now.date()
        self.earliest = ceil_to_step(now + timedelta(minutes=rules.min_lead_min), rules.slot_step_min)
        self.days: dict[date, _Day] = {}
        self.result = ScheduleResult()
        self.end_of: dict[str, datetime] = {}   # task id -> end of its slot
        self.buffer = timedelta(minutes=rules.buffer_min)
        self._journal: list[tuple[_Day, datetime, int, str]] | None = None   # for undoing a failed group

    # ------------------------------------------------------------------ days
    def _day(self, d: date) -> _Day:
        existing = self.days.get(d)
        if existing is not None:
            return existing
        blocks, warns = busy_blocks(self.tt, d, self.rules)
        if 0 <= (d - self.today).days <= 7:
            for w in warns:
                self.result.warnings.append(Conflict("commute", w))
        day = _Day(d, blocks, sum(b.minutes for b in blocks if b.kind == "class"))
        self.days[d] = day
        return day

    def _window(self, d: date, ignore_rest: bool) -> tuple[datetime, datetime]:
        r = self.rules
        lo = datetime.combine(d, r.day_start)
        hi = datetime.combine(d, r.day_end)
        if not ignore_rest:
            if d.weekday() in r.rest_days:
                return lo, lo
            if d.weekday() in r.rest_evenings:
                hi = min(hi, datetime.combine(d, r.rest_evening_start))
        return lo, hi

    def _free_intervals(self, day: _Day, lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
        occupied = [(b.start - self.buffer, b.end + self.buffer) for b in day.fixed]
        occupied += [(s - self.buffer, e + self.buffer) for s, e in day.tasks]
        occupied.sort()
        free: list[tuple[datetime, datetime]] = []
        cursor = lo
        for s, e in occupied:
            if s >= hi:
                break
            if s > cursor:
                free.append((cursor, s))
            cursor = max(cursor, e)
            if cursor >= hi:
                break
        if cursor < hi:
            free.append((cursor, hi))
        return [(a, b) for a, b in free if a < b]

    def _away(self, start: datetime, end: datetime) -> list[AwayPeriod]:
        return away_overlapping(self.tt, start, end)

    def _rolling_ok(self, day: _Day, start: datetime, end: datetime) -> bool:
        """Burnout guard: in ANY stretch of ``rolling_window_min`` there must not be more than
        ``max_rolling_task_min`` of task time (so long work days get real breaks)."""
        r = self.rules
        items = day.tasks + [(start, end)]
        window = timedelta(minutes=r.rolling_window_min)
        for t0, _ in items:                          # the worst stretch always begins where some task begins
            hi = t0 + window
            total = sum(max(0.0, (min(e, hi) - max(s, t0)).total_seconds()) for s, e in items) / 60
            if total > r.max_rolling_task_min + 1e-9:
                return False
        return True

    def _find_slot(self, day: _Day, minutes: int, earliest: datetime, latest: datetime | None,
                   ignore_rest: bool, cap_scale: float = 1.0,
                   ignore_away: bool = False) -> tuple[datetime, bool] | None:
        """Earliest slot of ``minutes`` on ``day``; returns (start, inside_away_period)."""
        lo, hi = self._window(day.date, ignore_rest)
        lo = max(lo, earliest)
        if latest is not None:
            hi = min(hi, latest)
        need = timedelta(minutes=minutes)
        step = self.rules.slot_step_min
        base_cap = self.rules.task_cap(day.date.weekday())
        for a, b in self._free_intervals(day, lo, hi):
            start = ceil_to_step(a, step)
            while start + need <= b:
                if not self._rolling_ok(day, start, start + need):
                    start += timedelta(minutes=step)             # too much work in a row here: try later
                    continue
                periods = [] if ignore_away else self._away(start, start + need)
                if not periods:
                    return start, bool(self._away(start, start + need))
                factor = min(p.load_factor for p in periods)
                if day.away_min + minutes <= int(base_cap * factor * cap_scale):
                    return start, True
                start = ceil_to_step(max(p.end for p in periods), step)    # hop past the vacation
        return None

    def _reserve(self, day: _Day, start: datetime, minutes: int) -> None:
        end = start + timedelta(minutes=minutes)
        day.tasks.append((start, end))
        day.task_min += minutes
        if self._away(start, end):
            day.away_min += minutes

    def _commit(self, task: Task, day: _Day, start: datetime, minutes: int) -> None:
        self._reserve(day, start, minutes)
        end = start + timedelta(minutes=minutes)
        self.end_of[task.id] = end
        self.result.placements[task.id] = Placement(start, end)
        if self._journal is not None:
            self._journal.append((day, start, minutes, task.id))

    def _undo(self, journal: list[tuple[_Day, datetime, int, str]]) -> None:
        for day, start, minutes, tid in reversed(journal):
            end = start + timedelta(minutes=minutes)
            day.tasks.remove((start, end))
            day.task_min -= minutes
            if self._away(start, end):
                day.away_min -= minutes
            self.result.placements.pop(tid, None)
            self.end_of.pop(tid, None)

    # ------------------------------------------------------------- the passes
    def run(self) -> ScheduleResult:
        todos = [t for t in self.tasks if t.type is TaskType.TODO]
        dailies = [t for t in self.tasks if t.type is TaskType.DAILY]        # habits are not scheduled
        fixed = [t for t in todos if t.kind is Kind.FIXED]
        flexible = sorted((t for t in todos if t.kind is Kind.FLEXIBLE), key=self._sort_key)

        for t in sorted(fixed, key=lambda t: (t.fixed_start, t.seq, t.id)):
            self._place_fixed(t)

        for t in sorted(dailies, key=lambda t: (not t.daily_time_fixed, t.seq, t.id)):
            self._place_daily(t)

        pending = [t for t in flexible if not self._keep_existing(t)]
        i = 0
        while i < len(pending):
            t = pending[i]
            if t.group is None:
                self._place_flexible(t)
                i += 1
                continue
            j = i
            while j < len(pending) and pending[j].group == t.group:
                j += 1
            self._place_group(pending[i:j])        # sessions of one long task are planned together
            i = j

        for d, day in sorted(self.days.items()):
            self.result.day_loads[d] = DayLoad(day.class_min, day.task_min, day.away_min)
        self.result.warnings.sort(key=lambda w: (w.kind, w.task_id or "", w.message))
        return self.result

    def _sort_key(self, t: Task) -> tuple:
        due = t.deadline or (self.now + timedelta(days=IMPLICIT_DUE_DAYS[t.priority]))
        if t.on_date is not None:
            due = min(due, datetime.combine(t.on_date, time(23, 59)))
        return (due, int(t.priority), t.seq, t.id)

    # ----------------------------------------------------------- fixed tasks
    def _place_fixed(self, t: Task) -> None:
        assert t.fixed_start is not None
        minutes = t.duration_min
        start = t.fixed_start
        end = start + timedelta(minutes=minutes)
        day = self._day(start.date())
        for b in day.fixed:
            if b.kind in ("class", "event", "commute") and overlaps(start, end, b.start, b.end):
                self.result.warnings.append(Conflict(
                    "overlap", f"'{t.title}' ({start:%a %H:%M}) overlaps {b.label}", t.id))
        for s, e in day.tasks:
            if overlaps(start, end, s, e):
                self.result.warnings.append(Conflict(
                    "overlap", f"'{t.title}' ({start:%a %H:%M}) overlaps another fixed task", t.id))
        self._commit(t, day, start, minutes)

    # --------------------------------------------------------------- dailies
    def _daily_days(self, t: Task) -> list[date]:
        return [self.today + timedelta(days=i) for i in range(self.rules.daily_lookahead_days + 1)
                if t.repeats_on(self.today + timedelta(days=i))]

    def _place_daily(self, t: Task) -> None:
        """Give a recurring task ONE stable time of day and reserve it on every repeat day."""
        r = self.rules
        minutes = effective_minutes(t.duration_min, r)
        days = self._daily_days(t)
        need = timedelta(minutes=minutes)

        if t.daily_time_fixed and t.daily_time is not None:           # the user chose the time
            for d in days:
                start = datetime.combine(d, t.daily_time)
                if start < self.now:
                    continue                                          # today's occurrence already passed
                day = self._day(d)
                for b in day.fixed:
                    if b.kind in ("class", "event", "commute") and overlaps(start, start + need, b.start, b.end):
                        self.result.warnings.append(Conflict(
                            "overlap", f"'{t.title}' ({d:%a} {t.daily_time:%H:%M}) overlaps {b.label}", t.id))
                self._reserve(day, start, minutes)
            self.result.daily_times[t.id] = t.daily_time
            return

        # Which days can host which start times?  Pre-compute free space once per day.
        applicable: list[tuple[date, list[tuple[datetime, datetime]], bool]] = []
        for d in days:
            day = self._day(d)
            lo, hi = self._window(d, ignore_rest=False)
            fits_cap = (day.task_min + minutes <= r.task_cap(d.weekday())
                        and day.class_min + day.task_min + minutes <= r.max_total_minutes_per_day)
            applicable.append((d, self._free_intervals(day, lo, hi), fits_cap))

        def fits(d: date, intervals, fits_cap: bool, tod: time) -> bool | None:
            start = datetime.combine(d, tod)
            if start < self.earliest:
                return None                                           # not applicable (already passed)
            if not fits_cap:
                return False
            return any(a <= start and start + need <= b for a, b in intervals)

        def evaluate(tod: time) -> tuple[int, int]:
            ok = bad = 0
            for d, intervals, fits_cap in applicable:
                verdict = fits(d, intervals, fits_cap, tod)
                if verdict is True:
                    ok += 1
                elif verdict is False:
                    bad += 1
            return ok, bad

        chosen: time | None = None
        if t.daily_time is not None:                                  # sticky: keep the old time if still perfect
            ok, bad = evaluate(t.daily_time)
            if ok > 0 and bad == 0:
                chosen = t.daily_time
        if chosen is None:
            pref = datetime.combine(self.today, r.daily_preferred_time)
            best: tuple[int, float, time] | None = None
            cursor = datetime.combine(self.today, r.day_start)
            last = datetime.combine(self.today, r.day_end) - need
            while cursor <= last:
                tod = cursor.time()
                ok, bad = evaluate(tod)
                if ok > 0:
                    score = (ok - bad, -abs((cursor - pref).total_seconds()), tod)
                    if best is None or score[:2] > best[:2]:
                        best = (score[0], score[1], tod)
                cursor += timedelta(minutes=r.slot_step_min)
            chosen = best[2] if best else None

        if chosen is None:
            if days:
                self.result.unplaced.append(t.id)
                self.result.warnings.append(Conflict(
                    "no_slot", f"No time of day found for the daily '{t.title}' ({minutes} min)", t.id))
            return

        failed: list[str] = []
        for d, intervals, fits_cap in applicable:
            verdict = fits(d, intervals, fits_cap, chosen)
            if verdict is None:
                continue
            if verdict:
                self._reserve(self._day(d), datetime.combine(d, chosen), minutes)
            else:
                failed.append(f"{d:%a}")
        if failed:
            self.result.warnings.append(Conflict(
                "daily", f"Daily '{t.title}' at {chosen:%H:%M} doesn't fit on: {', '.join(sorted(set(failed)))}", t.id))
        self.result.daily_times[t.id] = chosen

    # ------------------------------------------------------- flexible to-dos
    def _keep_existing(self, t: Task) -> bool:
        """Keep a flexible task's current slot if it is still perfectly valid."""
        p = t.placement
        if p is None:
            return False
        if p.minutes != effective_minutes(t.duration_min, self.rules):
            return False                                   # duration changed
        if p.end <= self.now:
            return False                                   # slot passed, not done -> roll over
        if p.start.date() != p.end.date():
            return False
        if t.on_date is not None and t.on_date >= self.today and p.start.date() != t.on_date:
            return False                                   # user picked a day
        if t.not_before is not None and p.start < t.not_before:
            return False
        if t.deadline is not None and p.end > t.deadline:
            return False
        d = p.start.date()
        if p.start < datetime.combine(d, self.rules.day_start) or p.end > datetime.combine(d, self.rules.day_end):
            return False
        day = self._day(d)
        if any(overlaps(p.start, p.end, b.start, b.end) for b in day.fixed):
            return False
        if any(overlaps(p.start, p.end, s, e) for s, e in day.tasks):
            return False
        self._commit(t, day, p.start, p.minutes)
        return True

    def _place_group(self, parts: list[Task]) -> None:
        """Plan the sessions of one split task as a chain. If balancing the load leaves a later
        session with no room (each must come after the previous one), undo and pack them
        as early as possible instead."""
        warn_mark, unplaced_mark = len(self.result.warnings), len(self.result.unplaced)
        self._journal = []
        ok = all(self._place_flexible(p) for p in parts)
        if not ok:
            self._undo(self._journal)
            del self.result.warnings[warn_mark:]
            del self.result.unplaced[unplaced_mark:]
            self._journal = []
            for p in parts:
                self._place_flexible(p, asap=True)
        self._journal = None

    def _place_flexible(self, t: Task, asap: bool = False) -> bool:
        r = self.rules
        minutes = effective_minutes(t.duration_min, r)
        if minutes > r.max_chunk_min:
            self.result.warnings.append(Conflict(
                "long", f"'{t.title}' is {minutes} min in one block; consider splitting it", t.id, "info"))

        earliest = self.earliest
        if t.not_before is not None:
            earliest = max(earliest, ceil_to_step(t.not_before, r.slot_step_min))
        if t.after_id and t.after_id in self.end_of:
            earliest = max(earliest, ceil_to_step(self.end_of[t.after_id] + self.buffer, r.slot_step_min))

        explicit: date | None = None
        if t.on_date is not None:
            if t.on_date >= self.today:
                explicit = t.on_date
            else:
                self.result.warnings.append(Conflict(
                    "past_date", f"'{t.title}': {t.on_date:%a %d %b} already passed; rescheduling", t.id))

        horizon_end = self.today + timedelta(days=r.horizon_days)
        for latest, ignore_rest, factor, flag, ignore_away in self._ladder(t, earliest, explicit):
            if explicit is not None:
                first = last = explicit
            else:
                first = max(self.today, earliest.date())
                last = min(latest.date(), horizon_end) if latest else horizon_end
            best = self._best_slot(t, minutes, first, last, earliest, latest, ignore_rest, factor, asap,
                                   ignore_away)
            if best is None:
                continue
            day, start = best
            self._commit(t, day, start, minutes)
            if flag == "tight":
                self.result.warnings.append(Conflict(
                    "tight", f"'{t.title}' is scheduled very close to its deadline", t.id, "info"))
            elif flag == "rest":
                self.result.warnings.append(Conflict(
                    "rest", f"'{t.title}' uses protected rest time ({start:%a %H:%M})", t.id))
            elif flag == "heavy":
                self.result.warnings.append(Conflict(
                    "heavy", f"'{t.title}' makes {start:%a %d %b} heavier than your daily limit", t.id))
            elif flag == "away":
                self.result.warnings.append(Conflict(
                    "away", f"'{t.title}' lands in your break ({start:%a %d %b %H:%M}): "
                            f"there was no other way to fit it", t.id))
            return True

        self.result.unplaced.append(t.id)
        if t.deadline is not None:
            msg = f"No room for '{t.title}' ({minutes} min) before {t.deadline:%a %d %b %H:%M}"
        elif explicit is not None:
            msg = f"No room for '{t.title}' ({minutes} min) on {explicit:%a %d %b}"
        else:
            msg = f"No free time found for '{t.title}' in the next {r.horizon_days} days"
        self.result.warnings.append(Conflict("no_slot", msg, t.id))
        return False

    def _ladder(self, t: Task, earliest: datetime, explicit: date | None):
        """Attempts from strictest to most relaxed:
        (latest, ignore_rest, cap_factor, flag, ignore_away)."""
        r = self.rules
        steps: list[tuple[datetime | None, bool, float, str | None, bool]] = []
        if t.deadline is not None:
            soft = t.deadline - timedelta(days=r.deadline_buffer_days)
            if soft > earliest and explicit is None:
                steps.append((soft, False, 1.0, None, False))
            steps.append((t.deadline, False, 1.0, None if explicit else "tight", False))
            steps.append((t.deadline, True, 1.0, "rest", False))
            steps.append((t.deadline, True, r.overload_factor, "heavy", False))
            steps.append((t.deadline, True, r.overload_factor, "away", True))
        else:
            steps.append((None, False, 1.0, None, False))
            if explicit is not None:
                steps.append((None, True, 1.0, "rest", False))
                steps.append((None, True, r.overload_factor, "heavy", False))
                steps.append((None, True, r.overload_factor, "away", True))
        return steps

    def _best_slot(self, t: Task, minutes: int, first: date, last: date, earliest: datetime,
                   latest: datetime | None, ignore_rest: bool, factor: float, asap: bool = False,
                   ignore_away: bool = False):
        r = self.rules
        w_task, w_total = (0.3, 0.15) if asap else (W_TASK_LOAD, W_TOTAL_LOAD)
        per_day = 2.0 if asap else 0.35 * DAY_DELAY_WEIGHT[t.priority]
        best: tuple[float, _Day, datetime] | None = None
        d = first
        while d <= last:
            day = self._day(d)
            cap_task = r.task_cap(d.weekday())
            cap_total = r.max_total_minutes_per_day
            if (day.task_min + minutes <= int(cap_task * factor)
                    and day.class_min + day.task_min + minutes <= int(cap_total * factor)):
                found = self._find_slot(day, minutes, earliest, latest, ignore_rest, factor, ignore_away)
                if found is not None:
                    start, in_away = found
                    cost = (w_task * (day.task_min + minutes) / max(1, cap_task)
                            + w_total * (day.class_min + day.task_min + minutes) / cap_total
                            + per_day * (d - self.today).days
                            + (AWAY_PENALTY if in_away else 0.0))
                    if best is None or cost < best[0] - 1e-9:
                        best = (cost, day, start)
            d += timedelta(days=1)
        return (best[1], best[2]) if best else None
