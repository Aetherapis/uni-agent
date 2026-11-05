"""Find the day the student actually WROTE (or said) in a message.

Gemini sometimes puts the right date in the wrong field ("tomorrow" under not_before, or no date
at all) or counts "tomorrow" from the wrong day. Words like "tomorrow", "31 October" and "Saturday"
can be read by plain code, so the interpreter uses this as a cross-check on what the model returned.

Deliberately conservative: it only reports a mention it is sure about, ignores possessives
("tomorrow's quiz"), ignores dates that already passed, and says nothing when it is ambiguous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
          "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
          "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY_ALT = "|".join(WEEKDAYS)
_SHORT_DAY = r"(?:(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+)?"
_NOT_POSSESSIVE = r"(?![’'`]s)"

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(rf"\b{_SHORT_DAY}(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b\.?(?:\s+(\d{{4}}))?")
_MONTH_DAY = re.compile(rf"\b{_SHORT_DAY}({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_AFTER_TOMORROW = re.compile(rf"\bday after tomorrow\b{_NOT_POSSESSIVE}")
_RELATIVE = re.compile(rf"\b(today|tonight|tomorrow|tmrw|tmr)\b{_NOT_POSSESSIVE}")
_WEEKDAY = re.compile(rf"\b({_DAY_ALT})\b{_NOT_POSSESSIVE}")

_CLOCK_COLON = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?(?![\d:])")
_CLOCK_AMPM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b")

_DEADLINE_WORDS = re.compile(r"\b(by|before|until|till|due|deadline|no later than|latest)\b")
_START_WORDS = re.compile(r"\b(after|from|starting|starts?|since|not before|no earlier than|onwards?)\b")


@dataclass(frozen=True)
class Mention:
    day: date
    kind: str            # "relative" | "explicit" | "weekday"
    role: str            # "on_date" | "deadline" | "not_before"  (from the words just before it)
    position: int


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(raw: str | None, today: date, month: int, day: int) -> date | None:
    if raw:
        year = int(raw) + (2000 if len(raw) == 2 else 0)
        return _safe_date(year, month, day)
    return _safe_date(today.year, month, day)


def _role(text: str, position: int) -> str:
    before = text[max(0, position - 28):position]
    if _DEADLINE_WORDS.search(before):
        return "deadline"
    if _START_WORDS.search(before):
        return "not_before"
    return "on_date"


def find_mentions(text: str, today: date) -> list[Mention]:
    """All days named in ``text`` that are today or later, in the order they appear."""
    low = text.lower().replace("’", "'")
    found: list[tuple[int, int, date, str]] = []                     # (start, end, day, kind)

    def add(m: re.Match, day: date | None, kind: str) -> None:
        if day is not None and day >= today:
            found.append((m.start(), m.end(), day, kind))

    for m in _ISO.finditer(low):
        add(m, _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "explicit")
    for m in _DAY_MONTH.finditer(low):
        add(m, _year(m.group(3), today, MONTHS[m.group(2)], int(m.group(1))), "explicit")
    for m in _MONTH_DAY.finditer(low):
        add(m, _year(m.group(3), today, MONTHS[m.group(1)], int(m.group(2))), "explicit")
    for m in _NUMERIC.finditer(low):                                 # dd/mm[/yyyy]: day first (Egypt/Europe)
        add(m, _year(m.group(3), today, int(m.group(2)), int(m.group(1))), "explicit")
    for m in _AFTER_TOMORROW.finditer(low):
        add(m, today + timedelta(days=2), "relative")
    for m in _RELATIVE.finditer(low):
        if low[max(0, m.start() - 10):m.start()].endswith("after "):  # "day after tomorrow" handled above
            continue
        offset = 0 if m.group(1) in ("today", "tonight") else 1
        add(m, today + timedelta(days=offset), "relative")
    for m in _WEEKDAY.finditer(low):
        if any(s <= m.start() < e for s, e, _, _ in found):          # "Saturday 31 October": the date covers it
            continue
        target = WEEKDAYS[m.group(1)]
        ahead = (target - today.weekday()) % 7 or 7                  # the NEXT such day, never today
        add(m, today + timedelta(days=ahead), "weekday")

    found.sort(key=lambda item: item[0])
    return [Mention(day, kind, _role(low, start), start) for start, _, day, kind in found]


def single_day(text: str, today: date) -> Mention | None:
    """The one day the text is about, or None when there is none or it is ambiguous (two different days)."""
    mentions = find_mentions(text, today)
    if not mentions or len({m.day for m in mentions}) != 1:
        return None
    strongest = next((m for m in mentions if m.kind != "weekday"), mentions[0])
    return Mention(strongest.day, strongest.kind, mentions[0].role, mentions[0].position)


def _to_time(hour: int, minute: int, suffix: str | None) -> time | None:
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return time(hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


def find_clock_times(text: str) -> list[time]:
    """Clock times written as 9AM, 3:30 pm or 21:00 (a bare "at 9" is too ambiguous to trust)."""
    low = text.lower()
    found: list[tuple[int, time]] = []
    for m in _CLOCK_COLON.finditer(low):
        t = _to_time(int(m.group(1)), int(m.group(2)), m.group(3))
        if t:
            found.append((m.start(), t))
    for m in _CLOCK_AMPM.finditer(low):
        if any(abs(m.start() - pos) < 6 for pos, _ in found):         # already caught as 3:30pm
            continue
        t = _to_time(int(m.group(1)), 0, m.group(2))
        if t:
            found.append((m.start(), t))
    return [t for _, t in sorted(found)]
