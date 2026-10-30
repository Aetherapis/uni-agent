"""Timetable + bus times -> concrete blocks of busy time for a given day."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .config import ConfigError, Rules
from .models import Block, day_bounds, overlaps

ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)


@dataclass(frozen=True)
class ClassSession:
    course: str
    kind: str                      # Lecture, Lab, Tutorial, Section...
    weekday: int                   # 0 = Monday
    start: time
    end: time
    location: str = ""
    valid_from: date | None = None  # term start for this session
    valid_to: date | None = None
    weeks: str = "all"             # "all" | "odd" | "even" (alternating-week labs)
    code: str = ""                 # e.g. MATH109
    group: str = ""                # e.g. "Tut 2", "Group A", "Lab 1"
    every_weeks: int = 1           # 2 = biweekly (every other week)
    anchor: date | None = None     # a date this session DID/WILL happen; needed when every_weeks > 1

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ConfigError(f"{self.course} {self.kind}: end must be after start")
        if self.every_weeks < 1:
            raise ConfigError(f"{self.course} {self.kind}: every_weeks must be 1 or more")
        if self.every_weeks > 1:
            if self.anchor is None:
                raise ConfigError(f"{self.course} {self.kind}: a biweekly class needs 'anchor' "
                                  f"(a date it happens, e.g. the last time you attended)")
            if self.anchor.weekday() != self.weekday:
                raise ConfigError(f"{self.course} {self.kind}: anchor {self.anchor} is not on the same "
                                  f"weekday as the class")
        if self.weeks not in ("all", "odd", "even"):
            raise ConfigError(f"{self.course} {self.kind}: weeks must be all/odd/even")


@dataclass(frozen=True)
class BusTrip:
    route: str
    direction: str                 # "to_campus" | "to_home"
    depart: time
    arrive: time
    weekdays: tuple[int, ...] = ALL_DAYS

    def __post_init__(self) -> None:
        if self.direction not in ("to_campus", "to_home"):
            raise ConfigError(f"Bus {self.route}: direction must be to_campus or to_home")
        if self.arrive <= self.depart:
            raise ConfigError(f"Bus {self.route} {self.depart}: arrive must be after depart")


@dataclass(frozen=True)
class AwayPeriod:
    """A stretch of time you're away / on a break: fewer tasks get planned in it."""
    start: datetime
    end: datetime
    load_factor: float = 0.35      # share of the normal daily task cap allowed (0 = none at all)
    label: str = "away"

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ConfigError(f"Away period '{self.label}' must end after it starts")
        if not 0.0 <= self.load_factor <= 1.0:
            raise ConfigError(f"Away period '{self.label}': load must be between 0 and 1")


@dataclass
class Timetable:
    sessions: list[ClassSession] = field(default_factory=list)
    buses: list[BusTrip] = field(default_factory=list)
    term_start: date | None = None                          # week 1 starts here (for odd/even weeks)
    cancelled: list[tuple[date, str]] = field(default_factory=list)   # (day, course or "*")
    events: list[Block] = field(default_factory=list)       # exams, one-off make-up lectures...
    away: list[AwayPeriod] = field(default_factory=list)    # vacations / trips
    periods: dict[int, tuple[time, time]] = field(default_factory=dict)   # "slot 3" -> (12:00, 13:30)

    def __post_init__(self) -> None:
        if self.term_start is None and any(s.weeks != "all" for s in self.sessions):
            raise ConfigError("Sessions with odd/even weeks need a term_start date")


def week_number(tt: Timetable, day: date) -> int | None:
    if tt.term_start is None:
        return None
    return (day - tt.term_start).days // 7 + 1


def sessions_on(tt: Timetable, day: date) -> list[ClassSession]:
    out: list[ClassSession] = []
    cancelled = {(d, c.lower()) for d, c in tt.cancelled}
    for s in tt.sessions:
        if s.weekday != day.weekday():
            continue
        if s.valid_from and day < s.valid_from:
            continue
        if s.valid_to and day > s.valid_to:
            continue
        if s.every_weeks > 1 and s.anchor is not None and (day - s.anchor).days % (7 * s.every_weeks) != 0:
            continue                                       # off week of a biweekly class
        if s.weeks != "all":
            wk = week_number(tt, day)
            if wk is None or ("odd" if wk % 2 == 1 else "even") != s.weeks:
                continue
        if (day, "*") in cancelled or (day, s.course.lower()) in cancelled:
            continue
        out.append(s)
    return sorted(out, key=lambda s: (s.start, s.end))


def class_blocks(tt: Timetable, day: date) -> list[Block]:
    return [
        Block(
            start=datetime.combine(day, s.start),
            end=datetime.combine(day, s.end),
            label=f"{s.course} {s.kind}" + (f" @ {s.location}" if s.location else ""),
            kind="class",
        )
        for s in sessions_on(tt, day)
    ]


def commute_blocks(tt: Timetable, day: date, rules: Rules) -> tuple[list[Block], list[str]]:
    """Pick the bus to campus and the bus home for a day with classes.

    to_campus: the *latest* bus that still gets you there on time.
    to_home:   the *earliest* bus you can catch after your last class.
    """
    sessions = sessions_on(tt, day)
    if not sessions or not tt.buses:
        return [], []

    first_start = datetime.combine(day, sessions[0].start)
    last_end = max(datetime.combine(day, s.end) for s in sessions)
    trips = [t for t in tt.buses if day.weekday() in t.weekdays]
    blocks: list[Block] = []
    warnings: list[str] = []
    label_day = f"{day:%a %d %b}"

    must_arrive_by = first_start - timedelta(minutes=rules.arrive_before_class_min)
    outbound = [t for t in trips if t.direction == "to_campus"
                and datetime.combine(day, t.arrive) <= must_arrive_by]
    if outbound:
        t = max(outbound, key=lambda t: (t.arrive, t.depart))
        blocks.append(_trip_block(day, t, rules, "Bus to campus"))
    else:
        warnings.append(f"{label_day}: no bus gets you to campus before your {first_start:%H:%M} class")

    can_leave_from = last_end + timedelta(minutes=rules.leave_after_class_min)
    inbound = [t for t in trips if t.direction == "to_home"
               and datetime.combine(day, t.depart) >= can_leave_from]
    if inbound:
        t = min(inbound, key=lambda t: (t.depart, t.arrive))
        blocks.append(_trip_block(day, t, rules, "Bus home"))
    else:
        warnings.append(f"{label_day}: no bus home after your last class ends at {last_end:%H:%M}")
    return blocks, warnings


def _trip_block(day: date, t: BusTrip, rules: Rules, what: str) -> Block:
    return Block(
        start=datetime.combine(day, t.depart) - timedelta(minutes=rules.trip_pad_before_min),
        end=datetime.combine(day, t.arrive) + timedelta(minutes=rules.trip_pad_after_min),
        label=f"{what} ({t.route} {t.depart:%H:%M}->{t.arrive:%H:%M})",
        kind="commute",
    )


def busy_blocks(tt: Timetable, day: date, rules: Rules) -> tuple[list[Block], list[str]]:
    """Everything immovable on ``day``: classes, buses, one-off events."""
    blocks = class_blocks(tt, day)
    commute, warnings = commute_blocks(tt, day, rules)
    blocks += commute
    lo, hi = day_bounds(day)
    blocks += [e for e in tt.events if overlaps(e.start, e.end, lo, hi)]
    return sorted(blocks, key=lambda b: (b.start, b.end)), warnings


def away_overlapping(tt: Timetable, start: datetime, end: datetime) -> list[AwayPeriod]:
    return [a for a in tt.away if overlaps(a.start, a.end, start, end)]


def end_of_uni_day(tt: Timetable, day: date) -> datetime | None:
    """When your last class of ``day`` ends (None if you have no classes) - i.e. "after uni"."""
    sessions = sessions_on(tt, day)
    if not sessions:
        return None
    return max(datetime.combine(day, s.end) for s in sessions)
