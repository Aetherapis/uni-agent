from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from uniagent.actions import apply_commands
from uniagent.config import load_rules
from uniagent.gemini import GeminiClient
from uniagent.interpreter import ParseContext, interpret, parse_response
from uniagent.models import Kind, Priority, Status, TaskType
from uniagent.notion import NotionAuthError, NotionClient, NotionError, discover
from uniagent.notion_setup import setup
from uniagent.notion_store import InboxMessage, NotionStore
from uniagent.ordering import build_items
from uniagent.scheduler import schedule
from uniagent.timetable import sessions_on

from fake_gemini import FakeGemini
from fake_habitica import FakeClock
from fake_notion import FakeNotion

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config.toml")
SEED = SEED_PATH
NOW = datetime(2026, 10, 26, 20, 5)


@pytest.fixture
def env():
    fake = FakeNotion()
    clock = FakeClock()
    client = NotionClient(fake.token, transport=fake, clock=clock.now, sleep=clock.sleep)
    setup(client, "parent-page", RULES, seed_path=SEED)
    store = NotionStore(client, discover(client, "parent-page"), RULES, clock=lambda: 1_790_000_000.0)
    return fake, store


def run_batch(store, commands, now=NOW, sources=None):
    """Interpret (validated) model output and apply it, the way a real run does."""
    tasks = store.load_tasks()
    tt = store.load_timetable()
    data = {"commands": [{"message": 1, **c} for c in commands], "questions": []}
    parsed = parse_response(data, ParseContext(tasks, tt, RULES, now, 1))
    applied = apply_commands(parsed.commands, tasks, tt, store, RULES, now, sources)
    return parsed, applied, tasks, tt


def add(title, **kw):
    return {"op": "add_task", "title": title, "task_type": kw.pop("task_type", "todo"), "type_confidence": 0.95, **kw}


def titles(store):
    return sorted(t.title for t in store.load_tasks())


# ----------------------------------------------------------------- adding
def test_each_kind_of_task_is_created_correctly(env):
    fake, store = env
    parsed, applied, *_ = run_batch(store, [
        add("Lab report", deadline="2026-10-30", minutes=60, priority="high"),
        add("Advisor meeting", at="2026-10-28 14:30"),
        add("Review lectures", task_type="daily", repeat_days=["sat", "tue"], daily_time="21:00"),
        add("Drink more water", task_type="habit"),
    ], sources={1: "typed message"})
    assert not applied.errors and len(applied.added) == 4
    by = {t.title: t for t in store.load_tasks()}
    assert by["Lab report"].deadline == datetime(2026, 10, 30, 23, 59) and by["Lab report"].priority is Priority.HIGH
    assert by["Advisor meeting"].kind is Kind.FIXED and by["Advisor meeting"].fixed_start == datetime(2026, 10, 28, 14, 30)
    daily = by["Review lectures"]
    assert daily.type is TaskType.DAILY and daily.repeat_days == (1, 5) and daily.daily_time_fixed
    assert by["Drink more water"].type is TaskType.HABIT and by["Drink more water"].habit_up
    row = fake.rows("Tasks")[0]["properties"]["Source"]["rich_text"][0]["plain_text"]
    assert row == "typed message"


def test_a_long_task_is_split_into_sessions_with_a_chain(env):
    fake, store = env
    _, applied, *_ = run_batch(store, [add("Physics lab report", minutes=300, deadline="2026-11-03")])
    parts = sorted(applied.added, key=lambda t: t.id)
    assert len(parts) == 4 and sum(p.duration_min for p in parts) == 300          # 90-minute sessions
    assert parts[1].after_id == parts[0].id and all(p.group == parts[0].group for p in parts)
    assert titles(store) == [f"Physics lab report ({i}/4)" for i in range(1, 5)]


# ----------------------------------------------------------------- updating
def test_do_it_thursday_moves_only_that_task_after_replanning(env):
    fake, store = env
    run_batch(store, [add(f"Task {i}", minutes=45) for i in range(1, 5)])
    tasks, tt = store.load_tasks(), store.load_timetable()
    first = schedule(tasks, tt, RULES, NOW)
    store.save_schedule(tasks, first)

    target = next(t for t in store.load_tasks() if t.title == "Task 2")
    parsed, applied, tasks, tt = run_batch(store, [{"op": "update_task", "ref": target.id, "on_date": "2026-10-29"}])
    assert [t.title for t in applied.updated] == ["Task 2"]
    second = schedule(tasks, tt, RULES, NOW)
    assert second.placements[target.id].start.date() == date(2026, 10, 29)
    for t in tasks:
        if t.id != target.id:
            assert second.placements[t.id] == first.placements[t.id]


def test_giving_an_exact_time_turns_a_flexible_task_fixed_and_a_day_frees_it_again(env):
    fake, store = env
    run_batch(store, [add("Call the bank")])
    t = store.load_tasks()[0]
    run_batch(store, [{"op": "update_task", "ref": t.id, "at": "2026-10-28 17:00"}])
    fixed = store.load_tasks()[0]
    assert fixed.kind is Kind.FIXED and fixed.fixed_start == datetime(2026, 10, 28, 17, 0)
    run_batch(store, [{"op": "update_task", "ref": t.id, "on_date": "2026-10-29"}])
    freed = store.load_tasks()[0]
    assert freed.kind is Kind.FLEXIBLE and freed.fixed_start is None and freed.on_date == date(2026, 10, 29)


def test_clearing_a_deadline_and_renaming_a_split_task_keeps_its_numbering(env):
    fake, store = env
    run_batch(store, [add("Lab report", minutes=200, deadline="2026-11-03")])
    ref = store.load_tasks()[0].id
    run_batch(store, [{"op": "update_task", "ref": ref, "new_title": "Physics lab", "clear": ["deadline"]}])
    tasks = store.load_tasks()
    assert sorted(t.title for t in tasks) == ["Physics lab (1/3)", "Physics lab (2/3)", "Physics lab (3/3)"]
    assert all(t.deadline is None for t in tasks)


def test_shrinking_a_split_task_replans_it_with_a_new_total(env):
    fake, store = env
    run_batch(store, [add("Lab report", minutes=300, deadline="2026-11-03")])
    ref = store.load_tasks()[0].id
    _, applied, *_ = run_batch(store, [{"op": "update_task", "ref": ref, "minutes": 120}])
    now_active = store.load_tasks()
    assert sum(t.duration_min for t in now_active) == 120 and len(applied.cancelled) == 4


def test_converting_a_todo_into_a_daily(env):
    fake, store = env
    run_batch(store, [add("Revise German", deadline="2026-10-30")])
    ref = store.load_tasks()[0].id
    run_batch(store, [{"op": "update_task", "ref": ref, "task_type": "daily", "repeat_days": ["sun", "tue"]}])
    t = store.load_tasks()[0]
    assert t.type is TaskType.DAILY and t.repeat_days == (1, 6) and t.deadline is None


def test_dailies_and_habits_accept_their_own_kind_of_update(env):
    fake, store = env
    run_batch(store, [add("Review", task_type="daily"), add("Phone", task_type="habit")])
    by = {t.title: t for t in store.load_tasks()}
    run_batch(store, [{"op": "update_task", "ref": by["Review"].id, "daily_time": "22:15", "repeat_days": ["wed"]},
                      {"op": "update_task", "ref": by["Phone"].id, "habit_direction": "down"}])
    by = {t.title: t for t in store.load_tasks()}
    assert by["Review"].daily_time == time(22, 15) and by["Review"].daily_time_fixed and by["Review"].repeat_days == (2,)
    assert by["Phone"].habit_down and not by["Phone"].habit_up


# ------------------------------------------------------- finishing / cancelling
def test_complete_cancel_and_check_in(env):
    fake, store = env
    run_batch(store, [add("A"), add("B"), add("Review", task_type="daily"), add("Water", task_type="habit")])
    by = {t.title: t for t in store.load_tasks()}
    _, applied, *_ = run_batch(store, [
        {"op": "complete_task", "ref": by["A"].id}, {"op": "cancel_task", "ref": by["B"].id},
        {"op": "check_in", "ref": by["Water"].id}, {"op": "complete_task", "ref": by["Review"].id}])
    assert [t.title for t in applied.completed] == ["A"] and [t.title for t in applied.cancelled] == ["B"]
    assert sorted((t.title, d) for t, d in applied.check_ins) == [("Review", "up"), ("Water", "up")]
    assert titles(store) == ["Review", "Water"]                      # routines stay; to-dos are gone
    assert store.load_done_keys() == [by["A"].id]


def test_finishing_a_split_task_finishes_every_session(env):
    fake, store = env
    run_batch(store, [add("Lab report", minutes=250)])
    ref = store.load_tasks()[0].id
    _, applied, *_ = run_batch(store, [{"op": "complete_task", "ref": ref}])
    assert len(applied.completed) == 3 and store.load_tasks() == []


# ------------------------------------------------------ vacation / classes / events
def test_vacation_is_saved_and_changes_the_plan(env):
    fake, store = env
    _, applied, tasks, tt = run_batch(store, [{
        "op": "add_away", "title": "Trip home", "from_day": "2026-10-28", "after_classes": True,
        "until": "2026-10-30 22:00"}])
    assert applied.away_added == [("Trip home", datetime(2026, 10, 28, 15, 15), datetime(2026, 10, 30, 22, 0))]
    assert store.load_timetable().away[0].start == datetime(2026, 10, 28, 15, 15)
    again = run_batch(store, [{"op": "add_away", "title": "Trip home", "from_day": "2026-10-28",
                               "after_classes": True, "until": "2026-10-30 22:00"}])[1]
    assert again.away_added == [] and len(fake.rows("Away")) == 1             # the same trip isn't stored twice


def test_a_cancelled_lecture_removes_it_from_the_timetable(env):
    fake, store = env
    _, applied, *_ = run_batch(store, [{"op": "cancel_class", "course": "physics", "date": "2026-11-08"}])
    tt = store.load_timetable()
    assert not [s for s in sessions_on(tt, date(2026, 11, 8)) if s.course == "Physics"]
    assert [s for s in sessions_on(tt, date(2026, 11, 4)) if s.course == "Physics"]      # other weeks untouched


def test_a_range_of_cancelled_days_is_one_row(env):
    fake, store = env
    run_batch(store, [{"op": "cancel_class", "course": "*", "date": "2026-11-08", "date_to": "2026-11-10"}])
    tt = store.load_timetable()
    assert len(fake.rows("Exceptions")) == 1
    assert all(not sessions_on(tt, d) for d in (date(2026, 11, 8), date(2026, 11, 10)))


def test_events_block_time_in_the_plan(env):
    fake, store = env
    run_batch(store, [{"op": "add_event", "title": "Math midterm", "start": "2026-11-03 10:00",
                       "end": "2026-11-03 12:00"}])
    tt = store.load_timetable()
    assert tt.events[0].label == "Math midterm"


def test_agenda_requests_and_replan_are_reported_not_stored(env):
    fake, store = env
    fake.log.clear()                                       # ignore the setup writes
    _, applied, *_ = run_batch(store, [{"op": "show_agenda", "date": "2026-10-27", "days": 2}, {"op": "replan_all"}])
    assert applied.agenda == [(date(2026, 10, 27), 2)] and applied.replan_all and fake.writes() == []


# ------------------------------------------------------------------- safety
def test_a_failing_command_does_not_stop_the_others(env):
    fake, store = env
    tasks, tt = store.load_tasks(), store.load_timetable()
    parsed = parse_response({"commands": [{"message": 1, **add("Doomed")}, {"message": 1, **add("Survivor")}],
                             "questions": []}, ParseContext(tasks, tt, RULES, NOW, 1))
    fake.fail_next = [(503, {}, {"message": "unavailable"})]                  # the first WRITE dies (reads are done)
    applied = apply_commands(parsed.commands, tasks, tt, store, RULES, NOW)
    assert [t.title for t in applied.added] == ["Survivor"] and "Couldn't add task" in applied.errors[0]
    assert titles(store) == ["Survivor"]


def test_auth_errors_abort_the_run(env):
    fake, store = env
    from uniagent.interpreter import Command
    tt = store.load_timetable()
    store.client._token = "revoked"
    with pytest.raises(NotionAuthError):
        apply_commands([Command(op="add_task", title="x", task_type=TaskType.TODO)], [], tt, store, RULES, NOW)


def test_the_same_commands_applied_twice_would_duplicate_so_callers_must_mark_messages_done(env):
    """Documents the at-most-once contract: apply_commands is not idempotent, the inbox status is."""
    fake, store = env
    run_batch(store, [add("Once")])
    run_batch(store, [add("Once")])
    assert titles(store) == ["Once", "Once"]


# ---------------------------------------------------------- the whole journey
def test_a_realistic_message_from_text_to_habitica_items(env):
    """Gemini's (faked) reading of a messy multi-intent message, through to what Habitica would show."""
    fake, store = env
    gemini = FakeGemini(answers=[{
        "transcripts": [],
        "commands": [
            add("Email the TA about the project", priority="urgent", minutes=15),
            add("Read calculus chapter 3", minutes=60, deadline="2026-10-29"),
            add("Physics lab report", minutes=240, deadline="2026-11-03 17:00", priority="high"),
            add("Review today's lectures", task_type="daily", repeat_days=["sat", "sun", "tue", "wed"], minutes=30),
            add("Drink more water", task_type="habit"),
            {"op": "add_away", "title": "Trip home", "from_day": "2026-10-28", "after_classes": True,
             "until": "2026-10-30 22:00"},
            {"op": "cancel_class", "course": "physics", "date": "2026-11-08"}],
        "questions": [], "ack": "On it!"}])
    client = GeminiClient(gemini.api_key, models=("m",), transport=gemini, sleep=lambda s: None)
    msgs = [InboxMessage("p1", "email the TA urgently, read ch3 by thursday, lab report by next tuesday 5pm "
                         "4 hours, review lectures on class days, drink more water, away wed after uni till late fri, "
                         "no physics lecture on the 8th", "text", 501, 1, 1, "", None, "Pending")]
    tasks, tt = store.load_tasks(), store.load_timetable()
    result = interpret(client, msgs, tasks, tt, RULES, NOW)
    assert not result.errors and not result.questions and len(result.commands) == 7
    applied = apply_commands(result.commands, tasks, tt, store, RULES, NOW)
    assert not applied.errors and applied.needs_replan

    tasks, tt = store.load_tasks(), store.load_timetable()                      # a fresh run reloads everything
    plan = schedule(tasks, tt, RULES, NOW)
    assert plan.unplaced == [] or all("lab" not in t.title.lower() for t in tasks if t.id in plan.unplaced)
    store.save_schedule(tasks, plan)
    items = build_items(tasks, plan.placements, tt, RULES, NOW, plan.daily_times)
    kinds = {i.kind for i in items}
    assert kinds == {"todo", "daily", "habit"}
    todos = [i for i in items if i.kind == "todo"]
    assert todos[0].title.endswith("Email the TA about the project")           # urgent goes first
    away = tt.away[0]
    in_break = [i for i in todos if i.start and away.start <= i.start < away.end]
    assert sum(1 for _ in in_break) <= 2                                        # the trip stays light
