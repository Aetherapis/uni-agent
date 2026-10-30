"""Core data models.

IMPORTANT: every datetime in this package is *naive local time* in the user's
timezone (see ``Rules.timezone``). Conversion to/from real timezones happens only
at the edges (Telegram, Notion, Habitica). This keeps the scheduler free of DST
and timezone bugs and makes it easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum, IntEnum


class Status(str, Enum):
    ACTIVE = "active"        # still needs doing (scheduled or not yet)
    DONE = "done"
    CANCELLED = "cancelled"


class Kind(str, Enum):
    FIXED = "fixed"          # the user gave an exact date AND time; never moved
    FLEXIBLE = "flexible"    # the scheduler picks the slot


class TaskType(str, Enum):
    """Maps 1:1 onto Habitica's three task kinds."""
    TODO = "todo"      # one-time task                      -> Habitica To-Do
    DAILY = "daily"    # repeats on a schedule (every day / chosen weekdays) -> Habitica Daily
    HABIT = "habit"    # no schedule, tracked with +/-      -> Habitica Habit


HABIT_RESETS = ("daily", "weekly", "monthly")


class Priority(IntEnum):
    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


@dataclass(frozen=True)
class Placement:
    """A concrete time slot for a task."""
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("Placement end must be after start")

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Block:
    """Something that occupies time and can't be moved (class, bus, exam...)."""
    start: datetime
    end: datetime
    label: str = ""
    kind: str = "event"      # "class" | "commute" | "event"

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Block '{self.label}' must end after it starts")

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Task:
    id: str
    title: str
    duration_min: int = 30
    kind: Kind = Kind.FLEXIBLE
    status: Status = Status.ACTIVE
    priority: Priority = Priority.NORMAL
    fixed_start: datetime | None = None    # required when kind == FIXED
    on_date: date | None = None            # user named a day but no time
    not_before: datetime | None = None     # earliest allowed start
    deadline: datetime | None = None       # must be finished by this moment
    after_id: str | None = None            # do this after another task (split sessions)
    group: str | None = None               # parent id when a long task was split
    notes: str = ""
    seq: int = 0                           # creation order (tie-breaker)
    placement: Placement | None = None     # current slot, None = not scheduled yet

    # --- what kind of task is it? (to-do / daily / habit) ---
    type: TaskType = TaskType.TODO
    repeat_days: tuple[int, ...] = ()      # DAILY: weekdays it repeats on (0=Mon); empty = every day
    daily_time: time | None = None         # DAILY: time of day (given by the user or chosen by the agent)
    daily_time_fixed: bool = False         # True when the user asked for that time -> never moved
    habit_up: bool = True                  # HABIT: has a "+" button (good behaviour)
    habit_down: bool = False               # HABIT: has a "-" button (behaviour to cut down)
    habit_reset: str = "daily"             # HABIT: counter resets daily / weekly / monthly

    def __post_init__(self) -> None:
        if self.duration_min <= 0:
            raise ValueError(f"Task '{self.title}': duration must be positive")
        if self.kind is Kind.FIXED and self.fixed_start is None:
            raise ValueError(f"Task '{self.title}': FIXED tasks need fixed_start")
        if self.type is not TaskType.TODO:
            if self.kind is Kind.FIXED or self.fixed_start is not None or self.on_date is not None:
                raise ValueError(f"Task '{self.title}': only one-time to-dos can have a fixed date/time")
        if self.type is TaskType.DAILY:
            if any(not 0 <= d <= 6 for d in self.repeat_days):
                raise ValueError(f"Task '{self.title}': repeat_days must be 0..6")
            self.repeat_days = tuple(sorted(set(self.repeat_days)))
        if self.type is TaskType.HABIT:
            if not (self.habit_up or self.habit_down):
                raise ValueError(f"Task '{self.title}': a habit needs a + and/or - button")
            if self.habit_reset not in HABIT_RESETS:
                raise ValueError(f"Task '{self.title}': habit_reset must be one of {HABIT_RESETS}")

    @property
    def active(self) -> bool:
        return self.status is Status.ACTIVE

    def repeats_on(self, d: date) -> bool:
        """DAILY only: does it fall on this date?"""
        return not self.repeat_days or d.weekday() in self.repeat_days


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True if [a_start, a_end) and [b_start, b_end) share any time."""
    return a_start < b_end and b_start < a_end


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ALIAS_RE = re.compile(r"[^A-Za-z0-9_-]")


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "x"


def alias_safe(text: str) -> str:
    """Habitica task aliases may only contain letters, digits, '-' and '_'."""
    return _ALIAS_RE.sub("-", text)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day)
    return start, start + timedelta(days=1)
