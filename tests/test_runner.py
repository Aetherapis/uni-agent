"""End to end: Telegram message -> Notion inbox -> Gemini -> commands -> plan -> reply -> Habitica."""
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from uniagent import runner
from uniagent.config import load_rules
from uniagent.gemini import GeminiClient
from uniagent.habitica import HabiticaClient
from uniagent.models import Status
from uniagent.notion import NotionAuthError, NotionClient, discover
from uniagent.notion_setup import setup
from uniagent.notion_store import NotionStore
from uniagent.runner import Runtime, Services, run_once
from uniagent.telegram import TelegramClient

from fake_gemini import FakeGemini
from fake_habitica import FakeClock, FakeHabitica
from fake_notion import FakeNotion
from fake_telegram import OWNER, FakeTelegram

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
SEED = SEED_PATH
START = datetime(2026, 10, 26, 20, 5)                         # Monday evening


@dataclass
class World:
    rules: object
    notion: FakeNotion = field(default_factory=FakeNotion)
    habitica: FakeHabitica = field(default_factory=FakeHabitica)
    gemini: FakeGemini = field(default_factory=FakeGemini)
    telegram: FakeTelegram = field(default_factory=FakeTelegram)
    now: datetime = START

    def __post_init__(self):
        clock = FakeClock()
        nclient = NotionClient(self.notion.token, transport=self.notion, clock=clock.now, sleep=clock.sleep)
        setup(nclient, "parent-page", self.rules, seed_path=SEED)
        self.store = NotionStore(nclient, discover(nclient, "parent-page"), self.rules)
        self.svc = Services(
            rules=self.rules, store=self.store,
            gemini=GeminiClient(self.gemini.api_key, models=self.rules.gemini_models, transport=self.gemini,
                                sleep=lambda s: None),
            habitica=HabiticaClient(self.habitica.user_id, self.habitica.api_key, call_delay_s=0,
                                    transport=self.habitica, clock=self.habitica.clock.now,
                                    sleep=self.habitica.clock.sleep),
            telegram=TelegramClient(self.telegram.token, transport=self.telegram, fetch=self.telegram.fetch,
                                    sleep=lambda s: None),
            owner_chat=OWNER, now=lambda: self.now)

    def run(self, **kw):
        return run_once(self.svc, **kw)

    def answer(self, commands=(), questions=(), transcripts=(), ack=None):
        self.gemini.answers.append({"commands": [{"message": 1, **c} for c in commands], "questions": list(questions),
                                    "transcripts": list(transcripts), "ack": ack})

    def active(self):
        return NotionStore(self.store.client, self.store.ws, self.rules).load_tasks()

    def inbox(self, status):
        return [r for r in self.notion.rows("Inbox")
                if r["properties"]["Status"]["select"]["name"] == status]

    def habitica_calls(self):
        return len(self.habitica.log)

    def runtime(self):
        return Runtime(NotionStore(self.store.client, self.store.ws, self.rules)).data


@pytest.fixture
def world():
    return World(rules=load_rules(ROOT / "config.toml"))


def add(title, **kw):
    return {"op": "add_task", "title": title, "task_type": kw.pop("task_type", "todo"), "type_confidence": 0.95, **kw}


BIG_ANSWER = [
    add("Email the TA about the project", priority="urgent", minutes=15),
    add("Read calculus chapter 3", minutes=60, deadline="2026-10-29"),
    add("Physics lab report", minutes=240, deadline="2026-11-03 17:00", priority="high"),
    add("Review today's lectures", task_type="daily", repeat_days=["sat", "sun", "tue", "wed"], minutes=30),
    add("Drink more water", task_type="habit"),
    {"op": "add_away", "title": "Trip home", "from_day": "2026-10-28", "after_classes": True, "until": "2026-10-30 22:00"},
]


# ------------------------------------------------------------------- quiet runs
def test_an_idle_run_costs_no_ai_no_messages_and_little_else(world):
    r1 = world.run()
    calls_after_first = world.habitica_calls()
    r2 = world.run()
    assert world.gemini.log == [] and world.telegram.sent == []
    assert r2.habitica is None and r2.habitica_skipped == "nothing changed"
    assert world.habitica_calls() == calls_after_first                    # Habitica untouched when nothing changed


def test_idle_run_makes_a_small_fixed_number_of_notion_calls(world):
    world.run()
    before = len(world.notion.log)
    world.run()
    assert len(world.notion.log) - before <= 14


# ------------------------------------------------------------- text, the full path
def test_a_text_message_becomes_scheduled_tasks_a_reply_and_habitica_items(world):
    uid = world.telegram.say("email the TA, ch3 by thursday, lab report..., review lectures, water, away wed")
    world.answer(BIG_ANSWER, ack="On it!")
    report = world.run()

    assert report.ingested == 1 and report.processed == 1 and report.gemini_used
    assert [r["properties"]["Status"]["select"]["name"] for r in world.notion.rows("Inbox")] == ["Done"]
    tasks = world.active()
    assert len(tasks) == 7 and sum("Physics lab report" in t.title for t in tasks) == 3    # 240 min -> 3 sessions
    reply = world.telegram.texts()[0]
    assert "On it!" in reply and "Email the TA about the project" in reply and "Away (Trip home)" in reply
    assert "sessions of" in reply and "Review today's lectures - daily at" in reply and "Drink more water - habit (+)" in reply

    todos = world.habitica.texts("todo")
    assert todos and todos[0].endswith("Email the TA about the project")             # urgent first
    assert len(world.habitica.texts("daily")) == 1 and world.habitica.texts("habit") == ["Drink more water"]
    assert world.runtime()["offset"] == uid + 1


def test_the_second_run_after_a_message_changes_nothing(world):
    world.telegram.say("add stuff")
    world.answer(BIG_ANSWER)
    world.run()
    gemini_calls, sent, writes = len(world.gemini.log), len(world.telegram.sent), len(world.habitica.writes())
    again = world.run()
    assert len(world.gemini.log) == gemini_calls and len(world.telegram.sent) == sent
    assert len(world.habitica.writes()) == writes and again.plan_writes == 0


# --------------------------------------------------------------------- voice
def test_a_voice_note_is_downloaded_transcribed_and_acted_on(world):
    world.telegram.voice(b"OggS-fake-audio")
    world.answer([add("Buy a notebook", minutes=20, priority="low")],
                 transcripts=[{"message": 1, "text": "buy a notebook sometime"}])
    world.run()
    parts = world.gemini.log[0][2]["contents"][0]["parts"]
    audio = [p for p in parts if "inline_data" in p]
    assert len(audio) == 1 and audio[0]["inline_data"]["mime_type"] == "audio/ogg"
    row = world.notion.rows("Inbox")[0]["properties"]
    assert row["Text"]["title"][0]["plain_text"] == "buy a notebook sometime"        # the transcript is kept
    assert row["Status"]["select"]["name"] == "Done"
    assert [t.title for t in world.active()] == ["Buy a notebook"]
    assert any("Buy a notebook" in t for t in world.telegram.texts())


def test_a_voice_note_that_cannot_be_downloaded_is_reported_not_retried_forever(world):
    uid = world.telegram.voice()
    world.telegram.files.clear()                                                    # Telegram forgot the file
    world.run()
    assert world.gemini.log == []                                                   # nothing to ask Gemini
    assert world.inbox("Error") and not world.inbox("Pending")
    assert any("Couldn't process a message" in t for t in world.telegram.texts())


# ---------------------------------------------------------------- security
def test_messages_from_other_chats_are_ignored_completely(world):
    world.telegram.say("delete everything", chat=999)
    world.telegram.say("hello", chat=999)
    report = world.run()
    assert report.ignored == 2 and world.notion.rows("Inbox") == []
    assert world.gemini.log == [] and world.telegram.sent == []


def test_photos_get_a_polite_reply_and_are_not_stored(world):
    world.telegram.photo()
    world.run()
    assert world.telegram.texts() == ["Sorry, I can only read text and voice notes."]
    assert world.notion.rows("Inbox") == []


# ------------------------------------------------------------------- failures
def test_quota_exhaustion_keeps_messages_and_tells_you_once(world):
    world.telegram.say("add a task")
    world.gemini.quota_models = set(world.rules.gemini_models)
    world.run()
    assert len(world.inbox("Pending")) == 1 and len(world.telegram.sent) == 1 and "quota" in world.telegram.sent[0][1]
    world.now += timedelta(minutes=10)
    world.run()
    assert len(world.telegram.sent) == 1                                            # no second notice
    world.gemini.quota_models = set()
    world.answer([add("Finally", minutes=20)])
    world.now += timedelta(minutes=10)
    world.run()
    assert not world.inbox("Pending") and [t.title for t in world.active()] == ["Finally"]


def test_unreadable_answers_are_retried_then_given_up_with_a_message(world):
    world.telegram.say("something")
    world.gemini.answers = ["not json"] * 60
    for _ in range(3):
        world.now += timedelta(minutes=5)
        world.run()
    assert not world.inbox("Pending") and len(world.inbox("Error")) == 1
    assert any("rephrase" in t for t in world.telegram.texts())


def test_an_overlapping_run_does_not_process_messages_twice(world):
    world.telegram.say("add a task")
    world.run(sync_only=True)                                                       # warm up the runtime row
    rt = Runtime(world.store)
    rt.set("lock_until", (world.now + timedelta(minutes=10)).isoformat())
    rt.save()
    report = world.run()
    assert report.busy and world.gemini.log == [] and len(world.inbox("Pending")) == 1
    world.now += timedelta(minutes=30)                                              # the lock expired
    world.answer([add("Task", minutes=20)])
    world.run()
    assert not world.inbox("Pending")


def test_without_a_gemini_key_messages_wait_and_you_are_told(world):
    world.svc.gemini = None
    world.telegram.say("add a task")
    world.run()
    assert len(world.inbox("Pending")) == 1 and "no Gemini key" in world.telegram.texts()[0]


def test_a_crash_before_the_offset_is_saved_never_duplicates_messages(world):
    world.telegram.say("add a task")
    rt = Runtime(world.store)
    runner.ingest(world.svc, rt, runner.RunReport())                                # stored, but we "crash" before saving
    rt2 = Runtime(world.store)                                                      # a fresh run sees no offset
    report = runner.RunReport()
    rt2.data.pop("offset", None)
    world.telegram.updates = world.telegram.updates or []
    runner.ingest(world.svc, rt2, report)
    assert len(world.notion.rows("Inbox")) == 1


def test_only_a_batch_of_messages_is_interpreted_per_run(world):
    world.rules = replace(world.rules, gemini_max_batch=2)
    world.svc.rules = world.rules
    for i in range(5):
        world.telegram.say(f"task {i}")
    world.answer([add("T0", minutes=15)])
    world.run()
    assert len(world.inbox("Pending")) == 3 and len(world.inbox("Done")) == 2
    world.answer([add("T1", minutes=15)]); world.run()
    world.answer([add("T2", minutes=15)]); world.run()
    assert not world.inbox("Pending")


# --------------------------------------------------------------- conversation
def test_questions_are_remembered_and_shown_to_the_model_next_time(world):
    world.telegram.say("gym twice a week")
    world.answer(questions=["Which days do you want to go to the gym?"])
    world.run()
    assert "Question: Which days" in world.telegram.texts()[0]
    world.telegram.say("sunday and tuesday")
    world.answer([add("Gym", task_type="daily", repeat_days=["sun", "tue"], minutes=60)])
    world.run()
    context = world.gemini.log[1][2]["contents"][0]["parts"][0]["text"]
    assert "Which days do you want to go to the gym?" in context
    assert world.runtime().get("pending_questions") is None


# --------------------------------------------------------------- slash commands
def test_slash_commands_need_no_ai(world):
    for text in ("/help", "/agenda tomorrow", "/status", "/bogus"):
        world.telegram.say(text)
    world.run()
    assert world.gemini.log == [] and len(world.inbox("Done")) == 4
    helps, agenda, status, bogus = world.telegram.texts()
    assert "voice notes" in helps and "Tuesday 27 Oct (4 classes" in agenda
    assert "to-dos" in status and "/bogus" in bogus


def test_agenda_shows_what_was_planned(world):
    world.telegram.say("add things")
    world.answer([add("Read ch3", minutes=60, on_date="2026-10-27")])
    world.run()
    world.telegram.say("/agenda tomorrow")
    world.run()
    assert "[ ] Read ch3" in world.telegram.texts()[-1]


def test_replan_forgets_old_slots_and_sync_forces_a_habitica_visit(world):
    world.telegram.say("add things")
    world.answer([add("Read ch3", minutes=60)])
    world.run()
    calls = world.habitica_calls()
    world.telegram.say("/sync")
    world.run()
    assert world.habitica_calls() > calls and "Syncing Habitica" in world.telegram.texts()[-1]
    world.telegram.say("/replan")
    report = world.run()
    assert "Re-planning" in world.telegram.texts()[-1] and report.slash_commands == 1


# ---------------------------------------------------------------- the loop closes
def test_finishing_a_task_in_chat_completes_it_in_habitica(world):
    world.telegram.say("add"); world.answer([add("Email TA", minutes=15)]); world.run()
    ref = world.active()[0].id
    world.telegram.say("done with the email")
    world.answer([{"op": "complete_task", "ref": ref}])
    world.run()
    assert world.active() == [] and world.habitica.completed and world.habitica.texts("todo") == []


def test_ticking_a_task_off_in_habitica_marks_it_done_in_notion(world):
    world.telegram.say("add"); world.answer([add("Email TA", minutes=15), add("Read ch3", minutes=30)]); world.run()
    alias = next(t["alias"] for t in world.habitica.tasks.values() if "Email" in t["text"])
    client = HabiticaClient(world.habitica.user_id, world.habitica.api_key, call_delay_s=0, transport=world.habitica,
                            clock=world.habitica.clock.now, sleep=world.habitica.clock.sleep)
    client.score_task(world.habitica.by_alias(alias)["_id"], "up")                  # ticked in the Habitica app
    world.run(force_sync=True)
    assert [t.title for t in world.active()] == ["Read ch3"]
    assert world.habitica.by_alias(alias) is None and len(world.habitica.completed) == 1   # not re-created


def test_a_habit_check_in_is_logged_in_habitica(world):
    world.telegram.say("add"); world.answer([add("Drink water", task_type="habit")]); world.run()
    ref = world.active()[0].id
    world.telegram.say("I drank water")
    world.answer([{"op": "check_in", "ref": ref, "direction": "up"}])
    world.run()
    assert world.habitica.scores and world.habitica.scores[0][1] == "up" and "+1 Drink water" in world.telegram.texts()[-1]


def test_time_passing_replans_missed_tasks_and_updates_habitica(world):
    world.telegram.say("add"); world.answer([add("Read ch3", minutes=60, on_date="2026-10-26")]); world.run()
    before = world.habitica.texts("todo")
    world.now += timedelta(days=2)
    world.telegram.say("/status")
    world.run()
    after = world.habitica.texts("todo")
    assert after and after != before and world.habitica.writes()


def test_impossible_deadlines_trigger_one_heads_up(world):
    world.telegram.say("add"); world.answer([add("Huge task", minutes=600, deadline="2026-10-26 22:00")]); world.run()
    first = [t for t in world.telegram.texts() if "No room" in t]
    assert first
    world.now += timedelta(minutes=5)
    world.run()
    assert len([t for t in world.telegram.texts() if "No room" in t]) == len(first)          # not repeated


def test_habitica_safety_stop_is_reported_once_and_can_be_overridden(world):
    world.telegram.say("add")
    world.answer([add(f"Chore {i}", minutes=15, priority="low") for i in range(8)])
    world.run()
    assert len(world.habitica.texts("todo")) == 8
    for t in world.active()[:7]:
        world.store.set_status(t, Status.CANCELLED)
    world.run(force_sync=True)
    alerts = [t for t in world.telegram.texts() if "Habitica sync paused" in t]
    assert len(alerts) == 1 and len(world.habitica.texts("todo")) == 8
    world.run(force_sync=True)
    assert len([t for t in world.telegram.texts() if "Habitica sync paused" in t]) == 1
    world.telegram.say("/sync force")
    world.run()
    assert len(world.habitica.texts("todo")) == 1


def test_skip_habitica_flag(world):
    world.telegram.say("add"); world.answer([add("Task", minutes=15)])
    report = world.run(skip_habitica=True)
    assert world.habitica.log == [] and report.habitica_skipped == "disabled for this run" and world.active()


# ---------------------------------------------------------------- hygiene
def test_logs_never_contain_messages_titles_or_keys(world, caplog):
    secret_title = "Top secret project zebra"
    world.telegram.say(f"remember {secret_title}")
    world.answer([add(secret_title, minutes=30)])
    with caplog.at_level(logging.DEBUG):
        world.run()
    text = caplog.text + "".join(r.getMessage() for r in caplog.records)
    for forbidden in (secret_title, "zebra", world.notion.token, world.habitica.api_key, world.gemini.api_key,
                      world.telegram.token):
        assert forbidden not in text


def test_run_summary_is_counts_only(world):
    world.telegram.say("add"); world.answer([add("Secret thing", minutes=20)])
    summary = world.run().summary()
    assert "Secret" not in summary and "processed 1" in summary


def test_authentication_failures_exit_nonzero_without_leaking(world, monkeypatch, capsys):
    world.store.client._token = "revoked"
    monkeypatch.setattr(runner, "build_services", lambda env, rules, need_telegram=True: world.svc)
    monkeypatch.setattr(runner, "load_rules", lambda path: world.rules)
    code = runner.main(["--config", "ignored"])
    out = capsys.readouterr().out
    assert code == 1 and "Authentication problem" in out and "revoked" not in out


# ---------------------------------------------------------------- digests
def digests(world):
    return [t for t in world.telegram.texts() if t.startswith(("Tomorrow:", "Good morning", "Weekly review"))]


def test_the_evening_preview_is_sent_once_and_only_in_its_window(world):
    world.telegram.say("add"); world.answer([add("Read ch3", minutes=45, deadline="2026-10-29 12:00")]); world.run()
    assert digests(world) == []                                      # 20:05: too early
    world.now = datetime(2026, 10, 26, 21, 5)
    world.run()
    assert len(digests(world)) == 1 and "Tuesday 27 Oct" in digests(world)[0]
    world.now += timedelta(minutes=10)
    world.run()
    assert len(digests(world)) == 1                                  # not repeated the same evening
    world.now = datetime(2026, 10, 27, 0, 40)
    world.run()
    assert len(digests(world)) == 1                                  # nor sent stale after midnight
    world.now = datetime(2026, 10, 27, 21, 5)
    world.run()
    assert len(digests(world)) == 2                                  # but again the next evening


def test_a_quiet_evening_sends_nothing_and_does_not_retry_all_night(world):
    world.now = datetime(2026, 10, 28, 21, 5)                        # tomorrow is a free Thursday with nothing due
    world.run(); world.now += timedelta(minutes=30); world.run()
    assert digests(world) == [] and world.runtime()["digests"]["evening"] == "2026-10-28"


def test_digest_and_review_commands_answer_on_demand(world):
    world.telegram.say("/digest"); world.telegram.say("/review")
    world.run()
    digest, review = world.telegram.texts()
    assert digest.startswith("Tomorrow:") and "nothing planned" not in digest.split("\n")[0]
    assert review.startswith("Weekly review") and "Next 7 days:" in review


def test_the_friday_review_counts_what_was_finished(world):
    world.telegram.say("add"); world.answer([add("Email TA", minutes=15), add("Read ch3", minutes=30)]); world.run()
    ref = next(t.id for t in world.active() if t.title == "Email TA")
    world.telegram.say("done"); world.answer([{"op": "complete_task", "ref": ref}]); world.run()
    world.now = datetime(2026, 10, 30, 10, 5)                        # Friday morning
    world.run()
    review = next(t for t in world.telegram.texts() if t.startswith("Weekly review"))
    assert "Done (1): Email TA" in review and "Still open: 1 to-dos" in review
    assert world.runtime()["digests"]["weekly"] == "2026-10-30"
