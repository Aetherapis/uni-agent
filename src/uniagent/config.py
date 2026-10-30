"""User preferences ("rules") loaded from config.toml.

Everything here is a preference, not a secret. Secrets (API keys) never go in
this file - they live in environment variables / GitHub Secrets.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEKDAY_NAMES = {v: k for k, v in WEEKDAYS.items()}


class ConfigError(ValueError):
    """Raised when config.toml has a mistake. The message says what to fix."""


def parse_hhmm(value: Any, what: str = "time") -> time:
    try:
        hh, mm = str(value).strip().split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{what}: expected 'HH:MM' like '08:30', got {value!r}") from exc


def parse_weekday(value: Any, what: str = "weekday") -> int:
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    key = str(value).strip().lower()[:3]
    if key not in WEEKDAYS:
        raise ConfigError(f"{what}: expected mon/tue/wed/thu/fri/sat/sun, got {value!r}")
    return WEEKDAYS[key]


@dataclass(frozen=True)
class Rules:
    timezone: str = "UTC"

    # shape of a day
    day_start: time = time(8, 0)
    day_end: time = time(21, 30)               # flexible tasks never end after this
    rest_evenings: tuple[int, ...] = (4,)      # weekdays kept free after rest_evening_start
    rest_evening_start: time = time(18, 0)
    rest_days: tuple[int, ...] = ()            # weekdays with no flexible tasks at all
    daily_preferred_time: time = time(20, 0)   # where recurring dailies sit when no time is given

    # workload / burnout protection
    max_task_minutes_per_day: int = 180
    max_total_minutes_per_day: int = 600       # classes + tasks together
    weekday_task_caps: Mapping[int, int] = field(default_factory=dict)
    overload_factor: float = 1.25              # last resort when a deadline is otherwise impossible
    away_load_factor: float = 0.35             # share of the normal daily cap allowed while you're away

    # pacing
    buffer_min: int = 10                       # gap kept around every class/task
    min_lead_min: int = 15                     # never schedule sooner than now + this
    slot_step_min: int = 5
    max_chunk_min: int = 120                   # longer tasks are split into sessions
    min_chunk_min: int = 25
    rolling_window_min: int = 240              # burnout guard: look at any stretch of this length...
    max_rolling_task_min: int = 180            # ...and allow at most this many task minutes in it

    # planning
    horizon_days: int = 21
    deadline_buffer_days: int = 1              # prefer finishing this many days early
    daily_lookahead_days: int = 14             # how far ahead recurring dailies are planned

    # commute (buses)
    arrive_before_class_min: int = 10
    leave_after_class_min: int = 10
    trip_pad_before_min: int = 10              # walking to the stop / getting ready
    trip_pad_after_min: int = 10               # walking from the stop

    # Habitica presentation
    habitica_horizon_days: int = 7             # only push the next N days to Habitica
    include_classes_in_habitica: bool = False  # also list lectures as to-dos
    class_lookahead_days: int = 1
    digest_morning: time | None = None         # e.g. 07:30 -> "today" message; None = off
    digest_evening: time | None = time(21, 0)  # "tomorrow" preview (buses can leave at 06:40, so evening is useful)
    weekly_day: int | None = 4                 # weekly review weekday (4 = Friday, your weekend)
    weekly_time: time = time(10, 0)
    habitica_call_delay_s: float = 30.0        # Habitica asks background scripts to wait 30 s between calls
    habitica_budget_min: int = 25              # stop syncing after this long; the next run continues
    habitica_max_deletes: int = 15             # safety stop: refuse to delete more than this in one run

    # Gemini (free tier: each model has its own quota, so a fallback chain is used)
    gemini_models: tuple[str, ...] = ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest")
    gemini_max_batch: int = 10                 # messages interpreted per Gemini call
    max_bulk_changes: int = 5                  # refuse to cancel/complete more tasks than this in one go

    def __post_init__(self) -> None:
        problems: list[str] = []
        if self.day_start >= self.day_end:
            problems.append("day.start must be earlier than day.end")
        if not (self.day_start <= self.rest_evening_start <= self.day_end):
            problems.append("day.rest_evening_start must be between day.start and day.end")
        if self.slot_step_min not in (1, 5, 10, 15, 30):
            problems.append("pacing.slot_step_min must be one of 1, 5, 10, 15, 30")
        if self.rolling_window_min < 30 or self.max_rolling_task_min < self.max_chunk_min:
            problems.append("pacing: rolling_window_min must be >= 30 and max_rolling_task_min at least max_chunk_min")
        if self.max_chunk_min < 2 * self.min_chunk_min:
            problems.append("pacing.max_chunk_min must be at least 2 x pacing.min_chunk_min")
        if self.max_task_minutes_per_day <= 0 or self.max_total_minutes_per_day <= 0:
            problems.append("limits must be positive")
        if not 0.0 <= self.away_load_factor <= 1.0:
            problems.append("limits.away_load_factor must be between 0 and 1")
        if self.overload_factor < 1.0:
            problems.append("limits.overload_factor must be >= 1.0")
        if self.horizon_days < 1:
            problems.append("planning.horizon_days must be >= 1")
        if self.habitica_call_delay_s < 30.0:
            problems.append("habitica.call_delay_seconds must be at least 30 (Habitica's rule for automated scripts)")
        if not self.gemini_models or self.gemini_max_batch < 1 or self.max_bulk_changes < 1:
            problems.append("gemini.models must not be empty; gemini.max_batch and limits.max_bulk_changes must be >= 1")
        if self.habitica_budget_min < 1 or self.habitica_max_deletes < 0:
            problems.append("habitica.run_budget_minutes must be >= 1 and max_deletes_per_run >= 0")
        try:
            ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001
            problems.append(f"timezone {self.timezone!r} is not a valid IANA name (e.g. 'Africa/Cairo')")
        if problems:
            raise ConfigError("Invalid config: " + "; ".join(problems))

    def task_cap(self, weekday: int) -> int:
        """Max flexible-task minutes allowed on a given weekday (0=Mon)."""
        return int(self.weekday_task_caps.get(weekday, self.max_task_minutes_per_day))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def local_now(rules: Rules) -> datetime:
    """Current time as naive local time in the user's timezone."""
    return datetime.now(rules.tz).replace(tzinfo=None, microsecond=0)


_SECTIONS: dict[str, set[str]] = {
    "day": {"start", "end", "rest_evenings", "rest_evening_start", "rest_days",
            "daily_preferred_time"},
    "limits": {"max_task_minutes_per_day", "max_total_minutes_per_day",
               "weekday_task_caps", "overload_factor", "away_load_factor", "max_bulk_changes"},
    "pacing": {"buffer_min", "min_lead_min", "slot_step_min", "max_chunk_min", "min_chunk_min",
               "rolling_window_min", "max_rolling_task_min"},
    "planning": {"horizon_days", "deadline_buffer_days", "daily_lookahead_days"},
    "commute": {"arrive_before_class_min", "leave_after_class_min",
                "trip_pad_before_min", "trip_pad_after_min"},
    "gemini": {"models", "max_batch"},
    "digest": {"morning", "evening", "weekly_day", "weekly_time"},
    "habitica": {"horizon_days", "include_classes", "class_lookahead_days",
                 "call_delay_seconds", "run_budget_minutes", "max_deletes_per_run"},
}


def rules_from_dict(data: Mapping[str, Any]) -> Rules:
    unknown = set(data) - set(_SECTIONS) - {"timezone"}
    if unknown:
        raise ConfigError(f"Unknown config section(s): {sorted(unknown)}")
    for section, allowed in _SECTIONS.items():
        extra = set(data.get(section, {})) - allowed
        if extra:
            raise ConfigError(f"Unknown key(s) in [{section}]: {sorted(extra)} (typo?)")

    day = data.get("day", {})
    limits = data.get("limits", {})
    pacing = data.get("pacing", {})
    planning = data.get("planning", {})
    commute = data.get("commute", {})
    hab = data.get("habitica", {})
    gem = data.get("gemini", {})
    dig = data.get("digest", {})

    kw: dict[str, Any] = {}
    if "timezone" in data:
        kw["timezone"] = str(data["timezone"])
    if "start" in day:
        kw["day_start"] = parse_hhmm(day["start"], "day.start")
    if "end" in day:
        kw["day_end"] = parse_hhmm(day["end"], "day.end")
    if "daily_preferred_time" in day:
        kw["daily_preferred_time"] = parse_hhmm(day["daily_preferred_time"], "day.daily_preferred_time")
    if "rest_evening_start" in day:
        kw["rest_evening_start"] = parse_hhmm(day["rest_evening_start"], "day.rest_evening_start")
    if "rest_evenings" in day:
        kw["rest_evenings"] = tuple(parse_weekday(x, "day.rest_evenings") for x in day["rest_evenings"])
    if "rest_days" in day:
        kw["rest_days"] = tuple(parse_weekday(x, "day.rest_days") for x in day["rest_days"])
    for key in ("max_task_minutes_per_day", "max_total_minutes_per_day"):
        if key in limits:
            kw[key] = int(limits[key])
    if "overload_factor" in limits:
        kw["overload_factor"] = float(limits["overload_factor"])
    if "away_load_factor" in limits:
        kw["away_load_factor"] = float(limits["away_load_factor"])
    if "weekday_task_caps" in limits:
        kw["weekday_task_caps"] = {
            parse_weekday(k, "limits.weekday_task_caps"): int(v)
            for k, v in limits["weekday_task_caps"].items()
        }
    for key in ("buffer_min", "min_lead_min", "slot_step_min", "max_chunk_min", "min_chunk_min",
                "rolling_window_min", "max_rolling_task_min"):
        if key in pacing:
            kw[key] = int(pacing[key])
    for key in ("horizon_days", "deadline_buffer_days", "daily_lookahead_days"):
        if key in planning:
            kw[key] = int(planning[key])
    for key in ("arrive_before_class_min", "leave_after_class_min",
                "trip_pad_before_min", "trip_pad_after_min"):
        if key in commute:
            kw[key] = int(commute[key])
    if "horizon_days" in hab:
        kw["habitica_horizon_days"] = int(hab["horizon_days"])
    if "include_classes" in hab:
        kw["include_classes_in_habitica"] = bool(hab["include_classes"])
    if "class_lookahead_days" in hab:
        kw["class_lookahead_days"] = int(hab["class_lookahead_days"])
    for key, field_name in (("morning", "digest_morning"), ("evening", "digest_evening")):
        if key in dig:
            kw[field_name] = parse_hhmm(dig[key], f"digest.{key}") if str(dig[key]).strip() else None
    if "weekly_day" in dig:
        kw["weekly_day"] = parse_weekday(dig["weekly_day"], "digest.weekly_day") if str(dig["weekly_day"]).strip() else None
    if "weekly_time" in dig:
        kw["weekly_time"] = parse_hhmm(dig["weekly_time"], "digest.weekly_time")
    if "models" in gem:
        kw["gemini_models"] = tuple(str(m) for m in gem["models"])
    if "max_batch" in gem:
        kw["gemini_max_batch"] = int(gem["max_batch"])
    if "max_bulk_changes" in limits:
        kw["max_bulk_changes"] = int(limits["max_bulk_changes"])
    if "call_delay_seconds" in hab:
        kw["habitica_call_delay_s"] = float(hab["call_delay_seconds"])
    if "run_budget_minutes" in hab:
        kw["habitica_budget_min"] = int(hab["run_budget_minutes"])
    if "max_deletes_per_run" in hab:
        kw["habitica_max_deletes"] = int(hab["max_deletes_per_run"])
    return Rules(**kw)


def load_rules(path: str | os.PathLike[str] | None = None) -> Rules:
    """Load rules from ``path``, ``$UNIAGENT_CONFIG``, ``./config.toml`` or defaults."""
    candidates = [path, os.environ.get("UNIAGENT_CONFIG"), "config.toml"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            with open(candidate, "rb") as fh:
                return rules_from_dict(tomllib.load(fh))
    return Rules()
