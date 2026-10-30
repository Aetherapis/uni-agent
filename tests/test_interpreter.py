import base64
from datetime import date, datetime, time
from pathlib import Path

import pytest

from uniagent.config import load_rules
from uniagent.gemini import GeminiClient
from uniagent.interpreter import (RESPONSE_SCHEMA, SYSTEM_PROMPT, ParseContext, build_context, build_parts,
                                  course_aliases, interpret, parse_response, resolve_course)
from uniagent.loaders import load_toml, timetable_from_dict
from uniagent.models import Kind, Placement, Priority, Task, TaskType
from uniagent.notion_store import InboxMessage

from fake_gemini import FakeGemini

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config.toml")
TT = timetable_from_dict(load_toml(SEED_PATH), RULES)
NOW = datetime(2026, 10, 26, 20, 5)                          # Monday evening


def task(id_, title, **kw):
    return Task(id=id_, title=title, seq=1, **kw)


TASKS = [
    task("a1", "Physics lab report", duration_min=90, group="lab", priority=Priority.HIGH),
    task("a2", "Physics lab report (2/2)", duration_min=90, group="lab"),
    task("b1", "Email the TA"),
    task("c1", "Read calculus chapter 3", placement=Placement(datetime(2026, 10, 29, 9, 0), datetime(2026, 10, 29, 10, 0))),
    task("d1", "Review lectures", type=TaskType.DAILY, repeat_days=(5, 6)),
    task("e1", "Drink water", type=TaskType.HABIT),
    task("f1", "Math tutorial sheet"),
    task("f2", "Math lecture notes"),
]
TASKS[0].title = "Physics lab report (1/2)"


def parse(*commands, questions=(), ack=None, transcripts=(), tasks=TASKS, n=1, now=NOW):
    data = {"commands": [{"message": 1, **c} for c in commands], "questions": list(questions), "ack": ack,
            "transcripts": list(transcripts)}
    return parse_response(data, ParseContext(tasks, TT, RULES, now, n))


# ------------------------------------------------------------------ add_task
def test_a_one_time_task_with_a_date_only_deadline():
    r = parse({"op": "add_task", "title": "Submit lab report", "task_type": "todo", "type_confidence": 0.95,
               "deadline": "2026-10-29", "minutes": 120, "priority": "high"})
    c = r.commands[0]
    assert (c.title, c.task_type, c.deadline, c.minutes, c.priority) == (
        "Submit lab report", TaskType.TODO, datetime(2026, 10, 29, 23, 59), 120, Priority.HIGH)
    assert not r.errors and not r.questions


def test_fixed_appointment_needs_a_time_of_day():
    ok = parse({"op": "add_task", "title": "Advisor meeting", "task_type": "todo", "type_confidence": 0.9,
                "at": "2026-10-28 14:30"})
    assert ok.commands[0].at == datetime(2026, 10, 28, 14, 30)
    bad = parse({"op": "add_task", "title": "Advisor meeting", "task_type": "todo", "at": "2026-10-28"})
    assert not bad.commands and "time of day" in bad.errors[0]


def test_a_daily_with_weekday_names_and_a_time():
    r = parse({"op": "add_task", "title": "German vocab", "task_type": "daily", "type_confidence": 0.9,
               "repeat_days": ["Sunday", "tue"], "daily_time": "21:00"})
    c = r.commands[0]
    assert c.repeat_days == (1, 6) and c.daily_time == time(21, 0)


def test_every_day_is_an_empty_tuple_not_none():
    r = parse({"op": "add_task", "title": "Review notes every night", "task_type": "daily", "type_confidence": 0.9,
               "repeat_days": ["daily"]})
    assert r.commands[0].repeat_days == ()


def test_habit_direction():
    r = parse({"op": "add_task", "title": "stop checking my phone while studying", "task_type": "habit",
               "type_confidence": 0.9, "habit_direction": "down"})
    c = r.commands[0]
    assert (c.habit_up, c.habit_down) == (False, True)


def test_dates_on_a_daily_or_habit_are_dropped_with_a_note():
    r = parse({"op": "add_task", "title": "Drink water", "task_type": "habit", "type_confidence": 0.9,
               "deadline": "2026-10-30"})
    assert r.commands[0].deadline is None and "ignored the date" in r.commands[0].note


@pytest.mark.parametrize("field,value", [("deadline", "2026-10-20"), ("on_date", "2026-10-25"),
                                         ("at", "2026-10-26 09:00")])
def test_dates_in_the_past_are_rejected(field, value):
    r = parse({"op": "add_task", "title": "Something", "task_type": "todo", "type_confidence": 0.9, field: value})
    assert not r.commands and "past" in r.errors[0]


def test_garbage_dates_are_reported_not_fatal():
    r = parse({"op": "add_task", "title": "A", "task_type": "todo", "deadline": "next thursday-ish"},
              {"op": "add_task", "title": "B", "task_type": "todo", "type_confidence": 0.9})
    assert [c.title for c in r.commands] == ["B"] and r.errors


def test_nulls_in_any_spelling_are_ignored_and_minutes_are_clamped():
    r = parse({"op": "add_task", "title": "Chore", "task_type": "todo", "type_confidence": 0.9, "deadline": "null",
               "notes": "None", "minutes": 100000})
    c = r.commands[0]
    assert c.deadline is None and c.notes is None and c.minutes == 720


def test_unknown_operations_and_junk_are_ignored():
    data = {"commands": [{"op": "delete_everything", "message": 1}, "junk", None, {"op": "add_task", "message": 1}],
            "questions": []}
    r = parse_response(data, ParseContext(TASKS, TT, RULES, NOW, 1))
    assert r.commands == [] and r.errors == []


def test_unsure_type_becomes_a_question_instead_of_a_guess():
    r = parse({"op": "add_task", "title": "gym twice a week", "task_type": "daily", "type_confidence": 0.4})
    assert not r.commands and "one-time task" in r.questions[0]


def test_type_missing_falls_back_to_the_classifier():
    r = parse({"op": "add_task", "title": "review my notes every night"})
    assert r.commands[0].task_type is TaskType.DAILY


def test_strong_disagreement_with_the_classifier_is_flagged_but_not_blocking():
    r = parse({"op": "add_task", "title": "review my notes every night", "task_type": "todo", "type_confidence": 0.7})
    assert r.commands and r.commands[0].task_type is TaskType.TODO and "tell me if that's wrong" in r.commands[0].note


# ------------------------------------------------------- existing tasks
def test_reference_by_id_expands_to_every_session_of_a_split_task():
    r = parse({"op": "update_task", "ref": "a1", "on_date": "2026-10-29"})
    assert {t.id for t in r.commands[0].targets} == {"a1", "a2"}


def test_reference_by_title_when_the_id_is_missing_or_wrong():
    r = parse({"op": "cancel_task", "ref": None, "title": "email the ta"})
    assert [t.id for t in r.commands[0].targets] == ["b1"]
    r2 = parse({"op": "complete_task", "ref": "zzz", "title": "calculus chapter 3"})
    assert [t.id for t in r2.commands[0].targets] == ["c1"]


def test_ambiguous_reference_asks_which_one():
    r = parse({"op": "cancel_task", "title": "math"})
    assert not r.commands and r.questions and "Math" in r.questions[0]


def test_unknown_reference_is_an_error():
    r = parse({"op": "complete_task", "title": "chemistry project"})
    assert not r.commands and "chemistry project" in r.errors[0]


def test_update_with_nothing_to_change_is_rejected():
    r = parse({"op": "update_task", "ref": "b1"})
    assert not r.commands and r.errors


def test_clearing_fields_counts_as_a_change():
    r = parse({"op": "update_task", "ref": "c1", "clear": ["deadline", "bogus"]})
    assert r.commands[0].clear == frozenset({"deadline"})


def test_bulk_cancellations_and_completions_are_refused_but_a_few_are_fine():
    many = [task(f"x{i}", f"Chore number {i}") for i in range(8)]
    few = parse(*[{"op": "cancel_task", "ref": f"x{i}"} for i in range(5)], tasks=many)
    assert len(few.commands) == 5 and not few.errors                         # at the limit: allowed
    lots = parse(*[{"op": "cancel_task", "ref": f"x{i}"} for i in range(8)], tasks=many)
    assert lots.commands == [] and "8 tasks at once" in lots.errors[0]
    done = parse(*[{"op": "complete_task", "ref": f"x{i}"} for i in range(8)], tasks=many)
    assert done.commands == [] and "complete 8" in done.errors[0]


def test_bulk_refusal_keeps_the_harmless_commands():
    many = [task(f"x{i}", f"Chore number {i}") for i in range(8)]
    r = parse(*[{"op": "cancel_task", "ref": f"x{i}"} for i in range(8)],
              {"op": "add_task", "title": "Buy milk", "task_type": "todo", "type_confidence": 0.9}, tasks=many)
    assert [c.op for c in r.commands] == ["add_task"]


def test_a_split_task_counts_as_one_in_the_bulk_limit():
    parts = [task(f"p{i}", f"Report ({i}/9)", group="g") for i in range(9)]
    r = parse({"op": "cancel_task", "ref": "p0"}, tasks=parts)
    assert len(r.commands) == 1 and len(r.commands[0].targets) == 9 and not r.errors


def test_a_check_in_defaults_to_up():
    r = parse({"op": "check_in", "ref": "e1"})
    assert r.commands[0].direction == "up" and r.commands[0].targets[0].id == "e1"


# ---------------------------------------------------- vacation, classes, events
def test_vacation_after_uni_starts_when_the_last_class_ends():
    r = parse({"op": "add_away", "title": "Trip home", "from_day": "2026-10-28", "after_classes": True,
               "until": "2026-10-30 22:00", "load": "light"})
    c = r.commands[0]
    assert c.start == datetime(2026, 10, 28, 15, 15) and c.end == datetime(2026, 10, 30, 22, 0)
    assert c.load == RULES.away_load_factor and c.label == "Trip home"


def test_vacation_without_an_end_asks_when_you_are_back():
    r = parse({"op": "add_away", "from_day": "2026-10-28", "after_classes": True})
    assert not r.commands and "Until when" in r.questions[0]


@pytest.mark.parametrize("load,expected", [("half", 0.5), ("none", 0.0), ("0.2", 0.2), ("5", 1.0)])
def test_vacation_load_words_and_numbers(load, expected):
    r = parse({"op": "add_away", "from": "2026-10-28 16:00", "until": "2026-10-30 22:00", "load": load})
    assert r.commands[0].load == expected


def test_vacation_that_ends_before_it_starts_is_an_error():
    r = parse({"op": "add_away", "from": "2026-10-30 16:00", "until": "2026-10-28 22:00"})
    assert not r.commands and r.errors


def test_cancelled_class_resolves_nicknames():
    r = parse({"op": "cancel_class", "course": "phys", "date": "2026-11-01"})
    assert r.commands[0].course == "Physics" and r.commands[0].dates == (date(2026, 11, 1),)
    r2 = parse({"op": "cancel_class", "course": "DLD", "date": "2026-11-01", "date_to": "2026-11-03"})
    assert r2.commands[0].course == "Digital Logic Design" and len(r2.commands[0].dates) == 3


def test_cancelled_class_with_unknown_course_or_no_date():
    assert "chemistry" in parse({"op": "cancel_class", "course": "chemistry", "date": "2026-11-01"}).errors[0].lower()
    assert parse({"op": "cancel_class", "course": "math"}).questions


def test_an_event_gets_a_one_hour_default_and_cannot_end_before_it_starts():
    r = parse({"op": "add_event", "title": "Math midterm", "start": "2026-11-03 10:00"})
    assert r.commands[0].end == datetime(2026, 11, 3, 11, 0)
    bad = parse({"op": "add_event", "title": "X", "start": "2026-11-03 10:00", "end": "2026-11-03 09:00"})
    assert not bad.commands and bad.errors


def test_agenda_requests_are_clamped():
    r = parse({"op": "show_agenda", "date": "2026-10-27", "days": 99})
    assert r.commands[0].dates == (date(2026, 10, 27),) and r.commands[0].days == 7


# -------------------------------------------------------------- the envelope
def test_questions_are_deduplicated_and_limited_ack_and_transcripts_are_kept():
    r = parse(questions=["Which day?", "Which day?", "a", "b", "c"], ack="x" * 500,
              transcripts=[{"message": 2, "text": " hello "}, {"message": "x", "text": "bad"}])
    assert len(r.questions) == 3 and len(r.ack) == 160 and r.transcripts == {2: "hello"}


def test_out_of_range_message_numbers_fall_back_to_the_first():
    r = parse({"op": "replan_all", "message": 9}, n=2)
    assert r.commands[0].message == 1


def test_course_aliases_cover_how_students_actually_talk():
    assert {"dld", "digital", "cs103"} <= course_aliases("Digital Logic Design", "CS103")
    assert {"eap", "english", "eng101"} <= course_aliases("English for Academic Purposes", "ENG101")
    assert {"math", "maths", "math109", "mathematics"} <= course_aliases("Mathematics I", "MATH109")
    assert resolve_course("prog", TT) == "Programming I" and resolve_course("nonsense", TT) is None


# --------------------------------------------------------------------- prompt
def test_system_prompt_teaches_the_engine_vocabulary():
    for needle in ("todo", "daily", "habit", "on_date", "deadline", "after_classes", "tutorial", "slot", "Saturday"):
        assert needle in SYSTEM_PROMPT


def test_schema_lists_every_operation():
    ops = RESPONSE_SCHEMA["properties"]["commands"]["items"]["properties"]["op"]["enum"]
    assert {"add_task", "update_task", "add_away", "cancel_class", "check_in", "show_agenda"} <= set(ops)


def test_context_has_calendar_slots_courses_and_task_ids():
    ctx = build_context(NOW, TT, TASKS, RULES, pending_questions=["Which days?"])
    assert "NOW: Monday 2026-10-26 20:05 (Africa/Cairo)" in ctx
    assert "Mon 2026-10-26 (today, no classes)" in ctx and "Tue 2026-10-27 (4 classes)" in ctx
    assert "Fri 2026-10-30 (no classes, protected evening)" in ctx
    assert "slot 3 = 12:00-13:30" in ctx and "cs103" in ctx
    assert "a1 | Physics lab report (1/2)" in ctx and "e1 | Drink water | habit" in ctx
    assert "Which days?" in ctx
    assert len(ctx) < 12_000


def test_context_marks_vacations_and_is_capped():
    from uniagent.timetable import AwayPeriod
    tt = timetable_from_dict(load_toml(SEED_PATH), RULES)
    tt.away.append(AwayPeriod(datetime(2026, 10, 28, 15, 15), datetime(2026, 10, 30, 22, 0), 0.35, "Trip"))
    many = [task(f"t{i}", f"Task {i}") for i in range(80)]
    ctx = build_context(NOW, tt, many, RULES)
    assert "Wed 2026-10-28 (4 classes, away)" in ctx and "and 20 more" in ctx


def msg(i, text="", source="text", file_id=""):
    return InboxMessage(page_id=f"p{i}", text=text, source=source, update_id=100 + i, chat_id=1, message_id=i,
                        file_id=file_id, received=None, status="Pending")


def test_voice_notes_are_attached_as_inline_audio():
    voice = msg(2, source="voice", file_id="F")
    parts, skipped, used = build_parts([msg(1, "buy a notebook"), voice], "CTX", lambda m: (b"OggS-bytes", "audio/ogg"))
    texts = [p.get("text", "") for p in parts]
    assert any("[1] (typed) buy a notebook" in t for t in texts) and any("[2] (voice note" in t for t in texts)
    audio = [p for p in parts if "inline_data" in p]
    assert len(audio) == 1 and base64.b64decode(audio[0]["inline_data"]["data"]) == b"OggS-bytes"
    assert [m.update_id for m in used] == [101, 102] and skipped == {}


def test_a_voice_note_that_cannot_be_fetched_is_skipped_and_reported():
    def boom(m):
        raise RuntimeError("telegram down")
    parts, skipped, used = build_parts([msg(1, "typed"), msg(2, source="voice", file_id="F")], "CTX", boom)
    assert [m.update_id for m in used] == [101] and 102 in skipped
    parts, skipped, used = build_parts([msg(2, source="voice", file_id="F")], "CTX", None)
    assert used == [] and 102 in skipped


def test_audio_beyond_the_size_limit_waits_for_the_next_run():
    big = lambda m: (b"x" * 9_000_000, "audio/ogg")                  # noqa: E731
    parts, skipped, used = build_parts([msg(1, source="voice"), msg(2, source="voice")], "CTX", big)
    assert [m.update_id for m in used] == [101]


def test_a_transcribed_voice_note_is_sent_as_text_not_audio():
    already = msg(1, "remind me to call mum", source="voice", file_id="F")
    parts, _, used = build_parts([already], "CTX", None)
    assert not any("inline_data" in p for p in parts) and used == [already]


# --------------------------------------------------------------- end to end
def client_for(fake):
    return GeminiClient(fake.api_key, models=("m1",), transport=fake, sleep=lambda s: None)


def test_interpret_maps_message_numbers_back_to_inbox_updates():
    fake = FakeGemini(answers=[{
        "transcripts": [{"message": 2, "text": "I'm away from Wednesday after uni until late Friday"}],
        "commands": [
            {"op": "add_task", "message": 1, "title": "Email the TA", "task_type": "todo", "type_confidence": 0.9},
            {"op": "add_away", "message": 2, "from_day": "2026-10-28", "after_classes": True,
             "until": "2026-10-30 22:00"}],
        "questions": [], "ack": "Got it!"}])
    result = interpret(client_for(fake), [msg(1, "email the TA"), msg(2, source="voice", file_id="F")], TASKS, TT, RULES, NOW,
                       audio_loader=lambda m: (b"audio", "audio/ogg"))
    assert [c.message for c in result.commands] == [101, 102]
    assert result.transcripts == {102: "I'm away from Wednesday after uni until late Friday"}
    assert result.ack == "Got it!" and result.model == "m1"
    sent = fake.log[0][2]["contents"][0]["parts"]
    assert "ACTIVE TASKS" in sent[0]["text"] and any("inline_data" in p for p in sent)


def test_nothing_to_interpret_makes_no_gemini_call():
    fake = FakeGemini()
    result = interpret(client_for(fake), [msg(1, source="voice")], TASKS, TT, RULES, NOW, audio_loader=None)
    assert fake.log == [] and result.commands == [] and 101 in result.skipped


def test_batches_are_limited_to_the_configured_size():
    from dataclasses import replace
    fake = FakeGemini()
    rules = replace(RULES, gemini_max_batch=3)
    interpret(client_for(fake), [msg(i, f"m{i}") for i in range(1, 8)], TASKS, TT, rules, NOW)
    sent = [p["text"] for p in fake.log[0][2]["contents"][0]["parts"] if p.get("text", "").startswith("[")]
    assert len(sent) == 3
