"""Try the engine on YOUR timetable:  python -m uniagent.demo [--now "2026-10-26 20:00"]"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .config import load_rules, local_now
from .loaders import load_toml, rules_note, tasks_from_dicts, timetable_from_dict
from .ordering import apply_moves, build_items, items_by_kind, plan_moves
from .render import format_schedule
from .scheduler import schedule, split_task

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--now", help="pretend it is this local time, e.g. '2026-10-26 20:00'")
    real = ROOT / "seed" / "timetable.toml"
    ap.add_argument("--timetable", default=str(real if real.exists() else ROOT / "seed" / "timetable.example.toml"))
    ap.add_argument("--tasks", default=str(ROOT / "examples" / "demo_tasks.toml"))
    ap.add_argument("--days", type=int, default=9)
    args = ap.parse_args()

    rules = load_rules(ROOT / "config.toml")
    now = datetime.fromisoformat(args.now) if args.now else local_now(rules)
    data = load_toml(args.timetable, args.tasks)
    tt = timetable_from_dict(data, rules)

    tasks = []
    for t in tasks_from_dicts(data.get("tasks", [])):
        tasks.extend(split_task(t, rules))

    result = schedule(tasks, tt, rules, now)
    print(f"Now: {now:%a %d %b %Y %H:%M}   ({rules_note(rules)})")
    for a in tt.away:
        print(f"Away: {a.label}  {a.start:%a %d %b %H:%M} -> {a.end:%a %d %b %H:%M}  (load x{a.load_factor})")
    print()
    print(format_schedule(result, tasks, tt, rules, now, days=args.days))

    items = items_by_kind(build_items(tasks, result.placements, tt, rules, now, result.daily_times))
    for kind, label in (("todo", "TO-DOS (top = next)"), ("daily", "DAILIES"), ("habit", "HABITS")):
        print(f"\nHABITICA {label}:")
        for i, item in enumerate(items[kind], start=1):
            extra = ""
            if kind == "todo":
                extra = f"   [due {item.due:%a %d %b}]" if item.due else "   [no due date]"
            elif kind == "daily":
                extra = "   [every day]" if not item.repeat_days else \
                    "   [" + ",".join("Mon Tue Wed Thu Fri Sat Sun".split()[d] for d in item.repeat_days) + "]"
            else:
                extra = "   [" + ("+" if item.up else "") + ("-" if item.down else "") + f", resets {item.reset}]"
            print(f"  {i:>2}. {item.title}{extra}")

    todo_keys = [i.key for i in items["todo"]]
    scrambled = ["my-own-1"] + todo_keys[::-1] + ["my-own-2"]
    moves = plan_moves(scrambled, todo_keys)
    assert apply_moves(scrambled, moves)[: len(todo_keys)] == todo_keys
    print(f"\n(Reordering a fully scrambled Habitica to-do list needs {len(moves)} move(s).)")


if __name__ == "__main__":
    main()
