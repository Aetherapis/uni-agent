"""Decide whether something you said is a one-time task, a daily, or a habit.

This is a transparent rule-based classifier. In Part 4 Gemini does the understanding and
this module acts as (a) a fallback when the LLM is unavailable and (b) a sanity check on
its answer. Every result carries a confidence and a human-readable reason, so the agent
can ask "is this a daily or a one-time thing?" when it is unsure.

    TODO  -> Habitica To-Do   one-time, has a due date, finished once ("submit report Thursday")
    DAILY -> Habitica Daily   repeats on a schedule, checked off each time ("revise every night",
                              "German vocab every Sun & Tue")
    HABIT -> Habitica Habit   no schedule, tracked with + / -  ("drink water", "stop doomscrolling")
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

from .models import TaskType

_DAY_NAMES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_RECURRENCE = [
    r"\bevery\s*day\b", r"\beveryday\b", r"\bdaily\b", r"\beach\s+day\b", r"\bnightly\b",
    r"\bevery\s+(?:morning|afternoon|evening|night)\b", r"\beach\s+(?:morning|afternoon|evening|night)\b",
    r"\bevery\s+other\s+day\b", r"\bevery\s+week(?:day)?s?\b", r"\bweekly\b", r"\beach\s+week\b",
    r"\b(?:once|twice|\d+\s*times)\s+a\s+week\b",
    r"كل\s+يوم", r"يوميا", r"يومياً", r"يوميًا", r"كل\s+(?:صباح|ليلة|مساء)", r"كل\s+أسبوع", r"أسبوعيا",
]
_HABIT_CUES = [
    r"\bhabit\b", r"\bwhenever\b", r"\bevery\s+time\b", r"\btry\s+to\b", r"\bremember\s+to\b",
    r"\bkeep\s+(?:up|on)\b", r"\bmore\s+often\b", r"\bas\s+much\s+as\s+(?:i\s+)?(?:can|possible)\b",
    r"\bdrink(?:ing)?\s+(?:more\s+)?water\b", r"\bstretch(?:ing)?\b", r"\btake\s+(?:short\s+)?breaks?\b",
    r"\bposture\b", r"عادة",
]
_NEGATIVE_CUES = [
    r"\bstop\b", r"\bquit\b", r"\bavoid\b", r"\blimit\b", r"\breduce\b", r"\bless\b", r"\bcut\s+down\b",
    r"\bno\s+more\b", r"\bdon'?t\b", r"\bdo\s+not\b", r"\bnever\b",
]
_ONE_TIME_CUES = [
    r"\btomorrow\b", r"\btoday\b", r"\btonight\b", r"\bby\s+(?:next\s+)?(?:mon|tue|wed|thu|fri|sat|sun)",
    r"\bdue\b", r"\bdeadline\b", r"\bsubmit\b", r"\bassignment\b", r"\bexam\b", r"\bquiz\b", r"\bbuy\b",
    r"\bemail\b", r"\bcall\b", r"\bmeet\b", r"\bregister\b", r"\bpay\b", r"\bnext\s+week\b",
    r"\bon\s+\d{1,2}(?:st|nd|rd|th)?\b", r"\bprint\b",
]


@dataclass(frozen=True)
class Classification:
    type: TaskType
    confidence: float                       # 0..1; below ~0.6 the agent should ask
    reason: str
    repeat_days: tuple[int, ...] = ()       # DAILY: empty = every day
    habit_up: bool = True
    habit_down: bool = False
    daily_time: time | None = None          # DAILY: a time of day the user mentioned


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def _weekdays(text: str, recurring: bool) -> tuple[int, ...]:
    """Weekdays named in the text. Only counted when it is clearly recurring ('every sun', 'sundays')."""
    found: set[int] = set()
    for tok in re.findall(r"[a-z]+", text):
        if tok in _DAY_NAMES and recurring:
            found.add(_DAY_NAMES[tok])
        elif tok.endswith("s") and tok[:-1] in _DAY_NAMES and len(tok) > 3:     # "sundays"
            found.add(_DAY_NAMES[tok[:-1]])
    return tuple(sorted(found))


def _clock_time(text: str) -> time | None:
    m = re.search(r"\b(?:at\s*)?(\d{1,2}):(\d{2})\s*(am|pm)?\b", text)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = re.search(r"\b(?:at\s*)?(\d{1,2})\s*(am|pm)\b", text)
        if not m:
            return None
        h, mi, ap = int(m.group(1)), 0, m.group(2)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return time(h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def classify_text(text: str) -> Classification:
    raw = text.strip()
    t = raw.lower()
    t_neg = re.sub(r"\b(?:don'?t|do\s+not)\s+forget\b", "", t)          # "don't forget to X" is a reminder
    recurring = _any(_RECURRENCE, t)
    named_days = _weekdays(t, recurring or bool(re.search(r"\bevery\b|\beach\b|\bon\b", t)))
    if named_days and re.search(r"\bevery\s+[a-z]+day\b|\beach\s+[a-z]+day\b|[a-z]+days\b", t):
        recurring = True
    negative = _any(_NEGATIVE_CUES, t_neg)
    habit_cue = _any(_HABIT_CUES, t)
    one_time = _any(_ONE_TIME_CUES, t)

    if negative and not (one_time and not recurring and not habit_cue):
        return Classification(TaskType.HABIT, 0.85 if (recurring or habit_cue) else 0.75,
                              "behaviour to cut down -> habit with a '-' button",
                              habit_up=False, habit_down=True)
    if habit_cue and not recurring:
        return Classification(TaskType.HABIT, 0.8, "no schedule, tracked repeatedly -> habit")
    if habit_cue and recurring:
        return Classification(TaskType.HABIT, 0.55,
                              "sounds like a habit but also has a schedule - could be a daily")
    if recurring:
        days = named_days
        sure = bool(days) or bool(re.search(r"every\s*day|everyday|daily|each\s+day|nightly|every\s+(?:morning|afternoon|evening|night)|يوم", t))
        vague = bool(re.search(r"(?:once|twice|\d+\s*times)\s+a\s+week|every\s+other\s+day", t))
        conf = 0.5 if vague and not days else (0.85 if sure else 0.7)
        reason = "repeats on a schedule -> daily" + (" (which days?)" if vague and not days else "")
        return Classification(TaskType.DAILY, conf, reason, repeat_days=days, daily_time=_clock_time(t))
    return Classification(TaskType.TODO, 0.9 if one_time else 0.65,
                          "one-time task" + ("" if one_time else " (no date or repetition mentioned)"))
