"""Build models from plain dicts (TOML today, Notion rows in a later part)."""
from __future__ import annotations

import tomllib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping

from .config import ConfigError, Rules, parse_hhmm, parse_weekday
from .models import HABIT_RESETS, Block, Kind, Priority, Task, TaskType
from .timetable import ALL_DAYS, AwayPeriod, BusTrip, ClassSession, Timetable, end_of_uni_day

PRIORITIES = {"urgent": Priority.URGENT, "high": Priority.HIGH,
              "normal": Priority.NORMAL, "low": Priority.LOW}
_EPOCH = date(2000, 1, 1)


def parse_date(value: Any, what: str = "date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{what}: expected YYYY-MM-DD, got {value!r}") from exc


def parse_datetime(value: Any, what: str = "datetime", date_only_end: bool = False) -> datetime:
    """Accepts 'YYYY-MM-DD HH:MM' or a bare date. Bare dates mean 23:59 when ``date_only_end``."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59) if date_only_end else time(0, 0))
    text = str(value).strip().replace("T", " ")
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time(23, 59) if date_only_end else time(0, 0))
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{what}: expected 'YYYY-MM-DD HH:MM', got {value!r}") from exc


def merge_data(*parts: Mapping[str, Any]) -> dict[str, Any]:
    """Combine several TOML dicts: lists are concatenated, tables merged, scalars overridden."""
    out: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if isinstance(value, list):
                out[key] = list(out.get(key, [])) + value
            elif isinstance(value, dict):
                out[key] = {**out.get(key, {}), **value}
            else:
                out[key] = value
    return out


def _periods(data: Mapping[str, Any]) -> dict[int, tuple[time, time]]:
    return {int(k): (parse_hhmm(v[0], f"periods.{k}"), parse_hhmm(v[1], f"periods.{k}"))
            for k, v in data.get("periods", {}).items()}


def _plus_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(_EPOCH, t) + timedelta(minutes=minutes)).time()


def _bus(b: Mapping[str, Any], default_ride: int) -> BusTrip:
    ride = int(b.get("ride_min", default_ride))
    dep = parse_hhmm(b["depart"], "bus.depart") if "depart" in b else None
    arr = parse_hhmm(b["arrive"], "bus.arrive") if "arrive" in b else None
    if dep is None and arr is not None and ride:
        dep = _plus_minutes(arr, -ride)
    elif arr is None and dep is not None and ride:
        arr = _plus_minutes(dep, ride)
    if dep is None or arr is None:
        raise ConfigError(f"Bus {b.get('route')!r}: give depart + arrive, or one of them plus ride_min")
    return BusTrip(
        route=str(b.get("route", "bus")), direction=b["direction"], depart=dep, arrive=arr,
        weekdays=tuple(parse_weekday(x, "bus.days") for x in b["days"]) if "days" in b else ALL_DAYS)


def _away_load(value: Any, rules: Rules) -> float:
    text = str(value).strip().lower()
    named = {"light": rules.away_load_factor, "half": 0.5, "none": 0.0, "off": 0.0, "zero": 0.0}
    if text in named:
        return named[text]
    try:
        return float(text)
    except ValueError as exc:
        raise ConfigError(f"away load: use light / half / none or a number 0-1, got {value!r}") from exc


def _away(a: Mapping[str, Any], tt: Timetable, rules: Rules) -> AwayPeriod:
    """Resolve an away entry. 'after_classes' means: starts when that day's last class ends."""
    if "from" in a:
        start = parse_datetime(a["from"], "away.from")
    else:
        day = parse_date(a["from_day"], "away.from_day")
        end_of_classes = end_of_uni_day(tt, day) if a.get("after_classes") else None
        start = end_of_classes or datetime.combine(day, time(0, 0))
    return AwayPeriod(start=start, end=parse_datetime(a["until"], "away.until"),
                      load_factor=_away_load(a.get("load", "light"), rules),
                      label=a.get("label", "away"))


def timetable_from_dict(data: Mapping[str, Any], rules: Rules | None = None) -> Timetable:
    rules = rules or Rules()
    periods = _periods(data)
    sessions: list[ClassSession] = []
    for s in data.get("sessions", []):
        if "period" in s:
            if int(s["period"]) not in periods:
                raise ConfigError(f"{s.get('course')}: unknown period {s['period']} (define it under [periods])")
            start, end = periods[int(s["period"])]
        else:
            start, end = parse_hhmm(s["start"], "session.start"), parse_hhmm(s["end"], "session.end")
        sessions.append(ClassSession(
            course=s["course"], kind=s.get("kind", "Lecture"),
            weekday=parse_weekday(s["day"], "session.day"), start=start, end=end,
            location=s.get("location", ""),
            valid_from=parse_date(s["from"]) if "from" in s else None,
            valid_to=parse_date(s["to"]) if "to" in s else None,
            weeks=s.get("weeks", "all"), code=s.get("code", ""), group=s.get("group", ""),
            every_weeks=int(s.get("every_weeks", 1)),
            anchor=parse_date(s["anchor"], "session.anchor") if "anchor" in s else None))

    default_ride = int(data.get("ride_min", 0))
    buses = [_bus(b, default_ride) for b in data.get("buses", [])]
    events = [
        Block(start=parse_datetime(e["start"], "event.start"), end=parse_datetime(e["end"], "event.end"),
              label=e["title"], kind="event")
        for e in data.get("events", [])
    ]
    cancelled = [(parse_date(c["date"], "cancelled.date"), c.get("course", "*"))
                 for c in data.get("cancelled", [])]
    term_start = parse_date(data["term_start"], "term_start") if "term_start" in data else None
    tt = Timetable(sessions=sessions, buses=buses, term_start=term_start,
                   cancelled=cancelled, events=events, periods=periods)
    tt.away = [_away(a, tt, rules) for a in data.get("away", [])]
    return tt


def tasks_from_dicts(items: list[Mapping[str, Any]]) -> list[Task]:
    tasks: list[Task] = []
    for seq, item in enumerate(items, start=1):
        title = item["title"]
        at = item.get("at")
        priority = PRIORITIES.get(str(item.get("priority", "normal")).lower())
        if priority is None:
            raise ConfigError(f"task {title!r}: priority must be urgent/high/normal/low")
        try:
            ttype = TaskType(str(item.get("type", "todo")).lower())
        except ValueError as exc:
            raise ConfigError(f"task {title!r}: type must be todo / daily / habit") from exc

        extra: dict[str, Any] = {}
        if ttype is TaskType.DAILY:
            repeat = item.get("repeat", "daily")
            extra["repeat_days"] = () if repeat in ("daily", "everyday", "all") else tuple(
                parse_weekday(x, "task.repeat") for x in repeat)
            if "time" in item:
                extra["daily_time"] = parse_hhmm(item["time"], "task.time")
                extra["daily_time_fixed"] = True
        elif ttype is TaskType.HABIT:
            direction = str(item.get("direction", "up")).lower()
            extra["habit_up"] = direction in ("up", "both")
            extra["habit_down"] = direction in ("down", "both")
            reset = str(item.get("reset", "daily")).lower()
            if reset not in HABIT_RESETS:
                raise ConfigError(f"task {title!r}: reset must be one of {HABIT_RESETS}")
            extra["habit_reset"] = reset

        tasks.append(Task(
            id=str(item.get("id", f"task{seq}")), title=title,
            duration_min=int(item.get("minutes", 30)),
            kind=Kind.FIXED if at else Kind.FLEXIBLE, type=ttype, priority=priority,
            fixed_start=parse_datetime(at, "task.at") if at else None,
            on_date=parse_date(item["on"], "task.on") if "on" in item else None,
            not_before=parse_datetime(item["not_before"], "task.not_before") if "not_before" in item else None,
            deadline=parse_datetime(item["deadline"], "task.deadline", date_only_end=True)
            if "deadline" in item else None,
            notes=item.get("notes", ""), seq=seq, **extra))
    return tasks


def load_toml(*paths: str | Path) -> dict[str, Any]:
    parts = []
    for path in paths:
        with open(path, "rb") as fh:
            parts.append(tomllib.load(fh))
    return merge_data(*parts)


def rules_note(rules: Rules) -> str:  # tiny helper for the demo header
    return f"tz={rules.timezone}, cap={rules.max_task_minutes_per_day} min/day"
