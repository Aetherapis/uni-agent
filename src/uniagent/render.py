"""Human-readable views of a schedule (used by the demo now, Telegram replies later)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from .config import Rules
from .models import Task
from .scheduler import ScheduleResult
from .timetable import Timetable, busy_blocks


def format_schedule(result: ScheduleResult, tasks: Sequence[Task], tt: Timetable,
                    rules: Rules, now: datetime, days: int = 7) -> str:
    by_id = {t.id: t for t in tasks}
    lines: list[str] = []
    for offset in range(days):
        d = now.date() + timedelta(days=offset)
        rows: list[tuple[datetime, str]] = []
        blocks, _ = busy_blocks(tt, d, rules)
        for b in blocks:
            rows.append((b.start, f"  {b.start:%H:%M}-{b.end:%H:%M}  [{b.kind}] {b.label}"))
        for tid, p in result.placements.items():
            if p.start.date() == d:
                t = by_id.get(tid)
                rows.append((p.start, f"  {p.start:%H:%M}-{p.end:%H:%M}  TASK {t.title if t else tid}"))
        load = result.day_loads.get(d)
        suffix = ""
        if load:
            suffix = f"   (tasks {load.task_min} min, classes {load.class_min} min"
            suffix += f", {load.away_min} min while away)" if load.away_min else ")"
        away = [a.label for a in tt.away if a.start.date() <= d <= a.end.date()]
        if away:
            suffix += f"   <away: {', '.join(away)}>"
        lines.append(f"{d:%a %d %b}{suffix}")
        lines += [text for _, text in sorted(rows)] or ["  (free)"]
    if result.unplaced:
        lines.append("")
        lines.append("UNPLACED: " + ", ".join(by_id[i].title if i in by_id else i for i in result.unplaced))
    if result.warnings:
        lines.append("")
        lines.append("NOTES:")
        lines += [f"  - [{w.severity}] {w.message}" for w in result.warnings]
    return "\n".join(lines)
