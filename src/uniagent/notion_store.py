"""Notion-backed storage: tasks, timetable, buses, vacations, inbox and small state values.

Everything personal lives in Notion (so the code repo contains no personal data), and you can
edit it from the Notion app - a row you add by hand is picked up on the next run.

All datetimes inside the agent are naive local time; Notion gets ISO strings with the UTC
offset of the configured timezone and they are converted back on read.
"""
from __future__ import annotations

import json
import logging
import secrets
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .config import Rules, parse_hhmm
from .models import Block, Kind, Placement, Priority, Status, Task, TaskType
from .notion import DAYS, NotionClient, Workspace
from .scheduler import ScheduleResult
from .timetable import ALL_DAYS, AwayPeriod, BusTrip, ClassSession, Timetable

log = logging.getLogger("uniagent.notion_store")

STATUS_LABEL = {Status.ACTIVE: "Active", Status.DONE: "Done", Status.CANCELLED: "Cancelled"}
TYPE_LABEL = {TaskType.TODO: "To-do", TaskType.DAILY: "Daily", TaskType.HABIT: "Habit"}
PRIORITY_LABEL = {Priority.URGENT: "Urgent", Priority.HIGH: "High", Priority.NORMAL: "Normal", Priority.LOW: "Low"}
BY_LABEL = {
    "status": {v.lower(): k for k, v in STATUS_LABEL.items()},
    "type": {v.lower(): k for k, v in TYPE_LABEL.items()},
    "priority": {v.lower(): k for k, v in PRIORITY_LABEL.items()},
}
VOICE_PLACEHOLDER = "(voice note)"                 # shown in Notion until the voice note has been transcribed
AGENT_MANAGED = ("Scheduled", "Agent note")        # written by save_schedule, never by update_task


def new_key() -> str:
    return secrets.token_hex(4)


# ------------------------------------------------------------- value helpers
def _title(v: str) -> dict:
    return {"title": [{"text": {"content": v[:2000]}}]}


def _text(v: str | None) -> dict:
    return {"rich_text": [{"text": {"content": v[:2000]}}]} if v else {"rich_text": []}


def _select(name: str | None) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def _multi(names: Iterable[str]) -> dict:
    return {"multi_select": [{"name": n} for n in names]}


def _date(tz: ZoneInfo, start: datetime | date | None, end: datetime | None = None) -> dict:
    if start is None:
        return {"date": None}
    def fmt(v):
        return v.isoformat() if type(v) is date else v.replace(tzinfo=tz).isoformat()
    value: dict = {"start": fmt(start)}
    if end is not None:
        value["end"] = fmt(end)
    return {"date": value}


def from_notion_dt(value: str, tz: ZoneInfo) -> datetime:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(0, 0))
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(tz).replace(tzinfo=None) if parsed.tzinfo else parsed


def _prop(page: Mapping, name: str) -> Mapping:
    return (page.get("properties") or {}).get(name) or {}


def plain(page: Mapping, name: str) -> str:
    p = _prop(page, name)
    parts = p.get("title") if "title" in p else p.get("rich_text")
    return "".join(x.get("plain_text") or (x.get("text") or {}).get("content", "") for x in (parts or [])).strip()


def selected(page: Mapping, name: str) -> str | None:
    return ((_prop(page, name).get("select")) or {}).get("name")


def number(page: Mapping, name: str) -> float | None:
    return _prop(page, name).get("number")


def checked(page: Mapping, name: str) -> bool:
    return bool(_prop(page, name).get("checkbox"))


def multi(page: Mapping, name: str) -> list[str]:
    return [o.get("name", "") for o in (_prop(page, name).get("multi_select") or [])]


def date_range(page: Mapping, name: str, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    d = _prop(page, name).get("date")
    if not d or not d.get("start"):
        return None, None
    return from_notion_dt(d["start"], tz), (from_notion_dt(d["end"], tz) if d.get("end") else None)


def _hhmm(page: Mapping, name: str) -> time | None:
    raw = plain(page, name)
    return parse_hhmm(raw, name) if raw else None


# ------------------------------------------------------------------- tasks
def task_properties(t: Task, tz: ZoneInfo) -> dict:
    """The user-facing columns for a task (schedule columns are written separately)."""
    return {
        "Name": _title(t.title), "Key": _text(t.id),
        "Status": _select(STATUS_LABEL[t.status]), "Type": _select(TYPE_LABEL[t.type]),
        "Kind": _select("Fixed" if t.kind is Kind.FIXED else "Flexible"),
        "Priority": _select(PRIORITY_LABEL[t.priority]), "Minutes": {"number": t.duration_min},
        "Fixed start": _date(tz, t.fixed_start), "On date": _date(tz, t.on_date),
        "Not before": _date(tz, t.not_before), "Deadline": _date(tz, t.deadline),
        "After": _text(t.after_id), "Group": _text(t.group),
        "Repeat days": _multi(DAYS[d] for d in t.repeat_days),
        "Daily time": _text(f"{t.daily_time:%H:%M}" if t.daily_time else ""),
        "Daily time fixed": {"checkbox": t.daily_time_fixed},
        "Habit +": {"checkbox": t.habit_up}, "Habit -": {"checkbox": t.habit_down},
        "Habit reset": _select(t.habit_reset if t.type is TaskType.HABIT else None),
        "Notes": _text(t.notes), "Seq": {"number": t.seq},
    }


def task_from_page(page: Mapping, tz: ZoneInfo) -> Task:
    """Build a Task from a Notion row. Raises ValueError for rows that can't be used."""
    title = plain(page, "Name")
    if not title:
        raise ValueError("row has no name")
    ttype = BY_LABEL["type"].get((selected(page, "Type") or "to-do").lower(), TaskType.TODO)
    fixed_start, _ = date_range(page, "Fixed start", tz)
    kind_label = (selected(page, "Kind") or "").lower()
    is_fixed = ttype is TaskType.TODO and (kind_label == "fixed" or (fixed_start is not None and kind_label != "flexible"))
    if is_fixed and fixed_start is None:
        raise ValueError("Kind is Fixed but 'Fixed start' is empty")
    created = page.get("created_time")
    seq = int(number(page, "Seq") or (datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000
                                         if created else 0))
    extra: dict[str, Any] = {}
    if ttype is TaskType.TODO:
        on_start, _ = date_range(page, "On date", tz)
        not_before, _ = date_range(page, "Not before", tz)
        deadline, _ = date_range(page, "Deadline", tz)
        s, e = date_range(page, "Scheduled", tz)
        minutes = int(number(page, "Minutes") or 30)
        extra.update(
            fixed_start=fixed_start if is_fixed else None,
            on_date=on_start.date() if on_start else None, not_before=not_before,
            deadline=(deadline.replace(hour=23, minute=59) if deadline and len((_prop(page, "Deadline").get("date") or {}).get("start", "")) == 10
                      else deadline),
            placement=Placement(s, e if e and e > s else s + timedelta(minutes=minutes)) if s else None)
    elif ttype is TaskType.DAILY:
        extra.update(repeat_days=tuple(DAYS.index(d) for d in multi(page, "Repeat days") if d in DAYS),
                     daily_time=_hhmm(page, "Daily time"), daily_time_fixed=checked(page, "Daily time fixed"))
    else:
        up, down = checked(page, "Habit +"), checked(page, "Habit -")
        extra.update(habit_up=up or not down, habit_down=down,
                     habit_reset=selected(page, "Habit reset") or "daily")
    return Task(
        id=plain(page, "Key") or str(page["id"]), title=title,
        duration_min=int(number(page, "Minutes") or 30),
        kind=Kind.FIXED if is_fixed else Kind.FLEXIBLE,
        status=BY_LABEL["status"].get((selected(page, "Status") or "active").lower(), Status.ACTIVE),
        priority=BY_LABEL["priority"].get((selected(page, "Priority") or "normal").lower(), Priority.NORMAL),
        after_id=plain(page, "After") or None, group=plain(page, "Group") or None,
        notes=plain(page, "Notes"), seq=seq, type=ttype, **extra)


def agent_note(result: ScheduleResult, task_id: str) -> str:
    notes = [w.message for w in result.warnings if w.task_id == task_id and w.kind != "long"]
    return "; ".join(dict.fromkeys(notes))[:1900]


@dataclass
class InboxMessage:
    page_id: str
    text: str
    source: str
    update_id: int
    chat_id: int
    message_id: int
    file_id: str
    received: datetime | None
    status: str


class NotionStore:
    def __init__(self, client: NotionClient, ws: Workspace, rules: Rules,
                 clock: Callable[[], float] = _time.time) -> None:
        self.client, self.ws, self.rules, self.clock = client, ws, rules, clock
        self.tz = rules.tz
        self.problems: list[tuple[str, str]] = []              # (page id, what is wrong) - skipped rows
        self._pages: dict[str, str] = {}                       # task key -> page id
        self._baseline: dict[str, dict] = {}                   # key -> columns as last read/written
        self._snapshot: dict[str, tuple[Placement | None, time | None, str]] = {}

    def _now(self) -> datetime:
        """Current time as an aware datetime in the user's timezone (uses the injectable clock)."""
        return datetime.fromtimestamp(self.clock(), self.tz)

    # ------------------------------------------------------------- tasks
    def load_tasks(self) -> list[Task]:
        """Active tasks (rows with no Status count as Active). Bad rows are skipped, see ``problems``."""
        self.problems = []
        pages = self.client.query(
            self.ws["Tasks"],
            filter={"or": [{"property": "Status", "select": {"equals": "Active"}},
                           {"property": "Status", "select": {"is_empty": True}}]},
            sorts=[{"property": "Seq", "direction": "ascending"}])
        tasks: list[Task] = []
        for page in pages:
            try:
                task = task_from_page(page, self.tz)
            except ValueError as exc:
                self.problems.append((str(page.get("id")), str(exc)))
                continue
            if any(t.id == task.id for t in tasks):
                self.problems.append((str(page.get("id")), "duplicate Key - row ignored"))
                continue
            self._pages[task.id] = page["id"]
            self._baseline[task.id] = task_properties(task, self.tz)
            self._snapshot[task.id] = (task.placement, task.daily_time, plain(page, "Agent note"))
            tasks.append(task)
        return tasks

    def load_done_keys(self, days: int = 14) -> list[str]:
        """Keys of tasks marked Done recently (so Habitica can complete them and award the reward)."""
        cutoff = (self._now() - timedelta(days=days)).isoformat()
        pages = self.client.query(self.ws["Tasks"], filter={"and": [
            {"property": "Status", "select": {"equals": "Done"}},
            {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cutoff}}]})
        return [plain(p, "Key") or str(p["id"]) for p in pages]

    def load_recent_tasks(self, status: Status, days: int = 7) -> list[Task]:
        """Tasks set to ``status`` (Done / Cancelled) within the last ``days`` days - for the weekly review."""
        cutoff = (self._now() - timedelta(days=days)).isoformat()
        pages = self.client.query(self.ws["Tasks"], filter={"and": [
            {"property": "Status", "select": {"equals": STATUS_LABEL[status]}},
            {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cutoff}}]})
        out: list[Task] = []
        for page in pages:
            try:
                out.append(task_from_page(page, self.tz))
            except ValueError:
                continue
        return out

    def add_tasks(self, tasks: Sequence[Task], source: str = "") -> None:
        taken = set(self._pages)
        for t in tasks:
            if not t.id or t.id in taken:
                t.id = new_key()
            taken.add(t.id)
            if not t.seq:
                t.seq = int(self.clock() * 1000)
            props = task_properties(t, self.tz)
            if source:
                props["Source"] = _text(source)
            page = self.client.create_page(self.ws["Tasks"], props)
            self._pages[t.id] = page["id"]
            self._baseline[t.id] = task_properties(t, self.tz)
            self._snapshot[t.id] = (None, t.daily_time, "")

    def update_task(self, task: Task) -> bool:
        """Write only the columns that changed since the task was loaded/created."""
        page_id = self._pages.get(task.id)
        if page_id is None:
            raise KeyError(f"unknown task {task.id!r}")
        new, old = task_properties(task, self.tz), self._baseline.get(task.id, {})
        changed = {k: v for k, v in new.items() if old.get(k) != v}
        if not changed:
            return False
        self.client.update_page(page_id, changed)
        self._baseline[task.id] = new
        return True

    def set_status(self, task: Task, status: Status) -> None:
        task.status = status
        self.update_task(task)

    def save_schedule(self, tasks: Sequence[Task], result: ScheduleResult) -> int:
        """Write the plan back (Scheduled slot, chosen daily time, agent note). Returns pages updated."""
        updated = 0
        for t in tasks:
            if not t.active or t.type is TaskType.HABIT or t.id not in self._pages:
                continue
            old_place, old_time, old_note = self._snapshot.get(t.id, (None, None, ""))
            props: dict = {}
            new_place = result.placements.get(t.id) if t.type is TaskType.TODO else None
            if new_place != old_place:
                props["Scheduled"] = _date(self.tz, new_place.start, new_place.end) if new_place else _date(self.tz, None)
            new_time = result.daily_times.get(t.id) if t.type is TaskType.DAILY else old_time
            if t.type is TaskType.DAILY and new_time != old_time and not t.daily_time_fixed:
                props["Daily time"] = _text(f"{new_time:%H:%M}" if new_time else "")
            note = agent_note(result, t.id)
            if note != old_note:
                props["Agent note"] = _text(note)
            if props:
                self.client.update_page(self._pages[t.id], props)
                updated += 1
            t.placement = new_place
            if t.type is TaskType.DAILY and not t.daily_time_fixed:
                t.daily_time = new_time
            self._snapshot[t.id] = (new_place, new_time if t.type is TaskType.DAILY else old_time, note)
        return updated

    # --------------------------------------------------------- timetable
    def load_timetable(self) -> Timetable:
        self.problems = []
        periods = self._load_periods()
        sessions = [s for s in (self._session(p, periods) for p in self.client.query(self.ws["Timetable"])) if s]
        buses = [b for b in (self._bus(p) for p in self.client.query(self.ws["Buses"])) if b]
        away = [a for a in (self._away(p) for p in self.client.query(self.ws["Away"])) if a]
        cancelled: list[tuple[date, str]] = []
        events: list[Block] = []
        for page in self.client.query(self.ws["Exceptions"]):
            self._exception(page, cancelled, events)
        return Timetable(sessions=sessions, buses=buses, cancelled=cancelled, events=events,
                         away=away, periods=periods)

    def _skip(self, page: Mapping, why: str) -> None:
        self.problems.append((str(page.get("id")), why))

    def _load_periods(self) -> dict[int, tuple[time, time]]:
        raw = self.get_state("periods")
        if not raw:
            return {}
        try:
            return {int(k): (parse_hhmm(v[0]), parse_hhmm(v[1])) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError, KeyError, IndexError):
            self.problems.append(("state:periods", "periods value is not valid JSON like {\"1\": [\"08:30\", \"10:00\"]}"))
            return {}

    def _session(self, page: Mapping, periods: Mapping[int, tuple[time, time]]) -> ClassSession | None:
        try:
            course, day = plain(page, "Course"), selected(page, "Day")
            if not course or day not in DAYS:
                return self._skip(page, "class needs a Course and a Day")
            start, end, period = _hhmm(page, "Start"), _hhmm(page, "End"), number(page, "Period")
            if (start is None or end is None) and period is not None and int(period) in periods:
                start, end = periods[int(period)]
            if start is None or end is None:
                return self._skip(page, f"class '{course}' needs Start+End or a known Period")
            anchor, _ = date_range(page, "Anchor", self.tz)
            valid_from, _ = date_range(page, "From", self.tz)
            valid_to, _ = date_range(page, "To", self.tz)
            return ClassSession(course=course, kind=selected(page, "Kind") or "Lecture", weekday=DAYS.index(day),
                                start=start, end=end, location=plain(page, "Location"),
                                valid_from=valid_from.date() if valid_from else None,
                                valid_to=valid_to.date() if valid_to else None,
                                code=plain(page, "Code"), group=plain(page, "Group"),
                                every_weeks=int(number(page, "Every weeks") or 1),
                                anchor=anchor.date() if anchor else None)
        except Exception as exc:  # noqa: BLE001 - a bad row must not break the run
            return self._skip(page, f"class row unusable: {exc}")

    def _bus(self, page: Mapping) -> BusTrip | None:
        try:
            direction = {"to campus": "to_campus", "to home": "to_home"}.get((selected(page, "Direction") or "").lower())
            dep, arr = _hhmm(page, "Depart"), _hhmm(page, "Arrive")
            if direction is None or dep is None or arr is None:
                return self._skip(page, "bus needs Direction, Depart and Arrive")
            days = tuple(DAYS.index(d) for d in multi(page, "Days") if d in DAYS) or ALL_DAYS
            return BusTrip(route=plain(page, "Route") or "bus", direction=direction, depart=dep, arrive=arr, weekdays=days)
        except Exception as exc:  # noqa: BLE001
            return self._skip(page, f"bus row unusable: {exc}")

    def _away(self, page: Mapping) -> AwayPeriod | None:
        try:
            start, end = date_range(page, "When", self.tz)
            if start is None or end is None:
                return self._skip(page, "away period needs a start and an end in 'When'")
            load = number(page, "Load")
            return AwayPeriod(start, end, self.rules.away_load_factor if load is None else min(1.0, max(0.0, float(load))),
                              plain(page, "Label") or "away")
        except Exception as exc:  # noqa: BLE001
            return self._skip(page, f"away row unusable: {exc}")

    def _exception(self, page: Mapping, cancelled: list, events: list) -> None:
        try:
            kind, title = (selected(page, "Type") or "").lower(), plain(page, "Title")
            start, end = date_range(page, "When", self.tz)
            if start is None:
                return self._skip(page, "exception needs a date in 'When'")
            if kind == "cancel class":
                last = (end or start).date()
                day = start.date()
                while day <= last:
                    cancelled.append((day, plain(page, "Course") or "*"))
                    day += timedelta(days=1)
            elif kind == "event":
                events.append(Block(start, end if end and end > start else start + timedelta(hours=1), title or "event", "event"))
            else:
                self._skip(page, "exception Type must be 'Cancel class' or 'Event'")
        except Exception as exc:  # noqa: BLE001
            self._skip(page, f"exception row unusable: {exc}")

    def add_away(self, label: str, start: datetime, end: datetime, load: float | None = None) -> None:
        props = {"Label": _title(label), "When": _date(self.tz, start, end)}
        if load is not None:
            props["Load"] = {"number": load}
        self.client.create_page(self.ws["Away"], props)

    def add_exception(self, kind: str, title: str, start: datetime | date, end: datetime | None = None,
                      course: str = "") -> None:
        self.client.create_page(self.ws["Exceptions"], {
            "Title": _title(title), "Type": _select("Cancel class" if kind == "cancel" else "Event"),
            "When": _date(self.tz, start, end), "Course": _text(course)})

    def seed_timetable(self, tt: Timetable) -> None:
        """One-time import of classes and buses (setup script)."""
        for s in tt.sessions:
            props = {"Course": _title(s.course), "Kind": _select(s.kind), "Day": _select(DAYS[s.weekday]),
                     "Start": _text(f"{s.start:%H:%M}"), "End": _text(f"{s.end:%H:%M}"),
                     "Location": _text(s.location), "Code": _text(s.code), "Group": _text(s.group),
                     "Every weeks": {"number": s.every_weeks}}
            if s.anchor:
                props["Anchor"] = _date(self.tz, s.anchor)
            for (period, (a, b)) in tt.periods.items():
                if (a, b) == (s.start, s.end):
                    props["Period"] = {"number": period}
            self.client.create_page(self.ws["Timetable"], props)
        for b in tt.buses:
            self.client.create_page(self.ws["Buses"], {
                "Route": _title(b.route), "Direction": _select("To campus" if b.direction == "to_campus" else "To home"),
                "Depart": _text(f"{b.depart:%H:%M}"), "Arrive": _text(f"{b.arrive:%H:%M}"),
                "Days": _multi(DAYS[d] for d in b.weekdays)})
        for a in tt.away:
            self.add_away(a.label, a.start, a.end, a.load_factor)
        for day, course in tt.cancelled:
            self.add_exception("cancel", f"{course} off" if course != "*" else "No classes", day,
                               course="" if course == "*" else course)
        for ev in tt.events:
            self.add_exception("event", ev.label, ev.start, ev.end)
        if tt.periods:
            self.set_state("periods", json.dumps({str(k): [f"{a:%H:%M}", f"{b:%H:%M}"] for k, (a, b) in tt.periods.items()}))

    # ------------------------------------------------------------- inbox
    def inbox_add(self, *, text: str, source: str, update_id: int, chat_id: int, message_id: int,
                  file_id: str = "", received: datetime | None = None) -> bool:
        """Store an incoming message as Pending. Returns False if this Telegram update is already stored."""
        if self.client.query(self.ws["Inbox"], filter={"property": "Update id", "number": {"equals": update_id}}):
            return False
        self.client.create_page(self.ws["Inbox"], {
            "Text": _title(text or VOICE_PLACEHOLDER), "Source": _select(source), "Status": _select("Pending"),
            "Update id": {"number": update_id}, "Chat id": {"number": chat_id}, "Message id": {"number": message_id},
            "File id": _text(file_id), "Received": _date(self.tz, received or self._now().replace(tzinfo=None))})
        return True

    def inbox_pending(self) -> list[InboxMessage]:
        pages = self.client.query(self.ws["Inbox"], filter={"property": "Status", "select": {"equals": "Pending"}},
                                  sorts=[{"property": "Update id", "direction": "ascending"}])
        out = []
        for p in pages:
            received, _ = date_range(p, "Received", self.tz)
            text = plain(p, "Text")
            out.append(InboxMessage(
                page_id=str(p["id"]), text="" if text == VOICE_PLACEHOLDER else text, source=selected(p, "Source") or "text",
                update_id=int(number(p, "Update id") or 0), chat_id=int(number(p, "Chat id") or 0),
                message_id=int(number(p, "Message id") or 0), file_id=plain(p, "File id"),
                received=received, status="Pending"))
        return out

    def inbox_mark(self, msg: InboxMessage, status: str, result: str = "") -> None:
        self.client.update_page(msg.page_id, {"Status": _select(status), "Result": _text(result)})
        msg.status = status

    def inbox_set_text(self, msg: InboxMessage, text: str) -> None:
        self.client.update_page(msg.page_id, {"Text": _title(text)})
        msg.text = text

    # ------------------------------------------------------------- state
    def _state_page(self, key: str) -> dict | None:
        rows = self.client.query(self.ws["State"], filter={"property": "Key", "title": {"equals": key}})
        return rows[0] if rows else None

    def get_state(self, key: str, default: str | None = None) -> str | None:
        page = self._state_page(key)
        return plain(page, "Value") if page else default

    def set_state(self, key: str, value: str) -> None:
        page = self._state_page(key)
        if page is None:
            self.client.create_page(self.ws["State"], {"Key": _title(key), "Value": _text(value)})
        elif plain(page, "Value") != value:
            self.client.update_page(page["id"], {"Value": _text(value)})
