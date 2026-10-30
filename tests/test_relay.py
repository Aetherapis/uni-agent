"""The instant-reply relay: the Worker (JavaScript) and the agent (Python) must agree."""
import json
import re
import shutil
import subprocess
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from uniagent import relay_setup
from uniagent.doctor import run_doctor
from uniagent.notion import NOTION_VERSION
from uniagent.notion_store import VOICE_PLACEHOLDER

from fake_telegram import FakeTelegram
from test_doctor import Kit
from test_runner import World, add, load_rules, ROOT as _ROOT   # noqa: F401  (World fixture helpers)

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@pytest.fixture
def world():
    return World(rules=load_rules(ROOT / "config.toml"))


def worker_rows():
    out = subprocess.run([NODE, "dump_requests.mjs"], cwd=RELAY, check=True, capture_output=True, text=True).stdout
    return json.loads(out)


# ------------------------------------------------------------- the Worker itself
@needs_node
def test_the_workers_own_tests_pass():
    result = subprocess.run([NODE, "--test"], cwd=RELAY, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-2000:]


def test_constants_shared_with_the_agent_match():
    source = (RELAY / "worker.js").read_text()
    assert re.search(r'VOICE_PLACEHOLDER = "([^"]+)"', source).group(1) == VOICE_PLACEHOLDER
    assert re.search(r'NOTION_VERSION = "([^"]+)"', source).group(1) == NOTION_VERSION


def test_the_event_the_worker_sends_is_one_the_workflow_listens_for():
    workflow = yaml.safe_load((ROOT / ".github/workflows/agent.yml").read_text())
    listens = (workflow.get("on") or workflow.get(True))["repository_dispatch"]["types"]
    default_event = re.search(r'env\.GITHUB_EVENT \|\| "([^"]+)"', (RELAY / "worker.js").read_text()).group(1)
    assert default_event in listens
    assert tomllib.loads((RELAY / "wrangler.toml").read_text())["vars"]["GITHUB_EVENT"] in listens


def test_every_setting_the_worker_needs_is_declared():
    source = (RELAY / "worker.js").read_text()
    required = set(re.search(r"REQUIRED = \[(.*?)\]", source, re.S).group(1).replace('"', "").replace(" ", "").replace("\n", "").split(",")) - {""}
    wrangler = tomllib.loads((RELAY / "wrangler.toml").read_text())
    declared = set(wrangler["vars"]) | {"WEBHOOK_SECRET", "NOTION_TOKEN", "GITHUB_TOKEN"}
    assert required <= declared, required - declared
    assert "secret put" in (RELAY / "wrangler.toml").read_text()               # secrets are never stored in the file


# ------------------------------------------------- Worker rows become real Inbox messages
@needs_node
def test_rows_written_by_the_worker_are_read_back_correctly_by_the_agent(world):
    rows = worker_rows()
    ds = world.store.ws["Inbox"]
    for props in rows:
        world.store.client.create_page(ds, props)           # the fake Notion is strict about names and shapes
    pending = world.store.inbox_pending()
    assert [(m.update_id, m.source, m.text, m.file_id) for m in pending] == [
        (9001, "text", "email the TA tomorrow", ""),
        (9002, "voice", "", "VOICE-1"),                      # placeholder text is hidden from the interpreter
        (9003, "voice", "with a caption", "VOICE-2")]
    assert pending[0].chat_id == 777 and pending[0].message_id == 1
    assert pending[0].received == datetime(2026, 9, 21, 17, 13, 20)            # UTC 14:13:20 -> Cairo 17:13:20


@needs_node
def test_worker_and_agent_send_the_same_columns_in_the_same_shapes(world):
    world.notion.log.clear()
    world.store.inbox_add(text="hello", source="text", update_id=1, chat_id=777, message_id=1,
                          received=datetime(2026, 9, 21, 17, 13, 20))
    sent = next(body for method, path, body in world.notion.log if method == "POST" and path == "/pages")["properties"]
    from_worker = worker_rows()[0]
    assert set(from_worker) == set(sent)
    for column in sent:                                      # same kind of value in every column
        assert set(from_worker[column]) == set(sent[column]), column


@needs_node
def test_a_message_that_arrives_through_the_relay_is_processed_like_any_other(world):
    world.telegram.webhook = {"url": "https://relay.example.workers.dev", "secret": "x" * 20}     # webhook mode
    for props in worker_rows()[:1]:
        world.store.client.create_page(world.store.ws["Inbox"], props)
    world.answer([add("Email the TA", minutes=15)], ack="On it!")
    report = world.run()
    assert report.webhook_mode and report.processed == 1 and "webhook mode" in report.summary()
    assert [t.title for t in world.active()] == ["Email the TA"]
    assert any("Email the TA" in t for t in world.telegram.texts())
    assert world.habitica.texts("todo")[0].endswith("Email the TA")


def test_webhook_mode_does_not_poll_or_crash_and_scheduled_runs_still_work(world):
    world.telegram.webhook = {"url": "https://relay.example.workers.dev", "secret": "x" * 20}
    report = world.run()
    assert report.webhook_mode and report.errors == []
    polls = [name for name, _ in world.telegram.log if name == "getUpdates"]
    assert polls                                                  # it tried once, was told 409, and carried on


def test_other_telegram_errors_are_not_mistaken_for_webhook_mode(world):
    world.telegram.fail_next = [(500, {"ok": False, "description": "server error"})]
    with pytest.raises(Exception):
        world.run()


# ---------------------------------------------------------------- relay_setup
def tg(fake):
    from uniagent.telegram import TelegramClient
    return TelegramClient(fake.token, transport=fake, fetch=fake.fetch, sleep=lambda s: None)


SECRET = "a-long-random-secret-123"


def test_switching_to_the_relay_and_back():
    fake = FakeTelegram()
    code, text = relay_setup.run("info", telegram=tg(fake))
    assert code == 0 and "polling" in text
    code, text = relay_setup.run("set", telegram=tg(fake), url="https://relay.example.workers.dev/hook", secret=SECRET)
    assert code == 0 and "relay.example.workers.dev" in text and SECRET not in text and fake.webhook["secret"] == SECRET
    assert "webhook mode: relay.example.workers.dev" in relay_setup.run("info", telegram=tg(fake))[1]
    code, text = relay_setup.run("clear", telegram=tg(fake))
    assert code == 0 and fake.webhook == {} and "poll" in text


def test_queued_messages_are_kept_when_switching():
    fake = FakeTelegram()
    fake.say("waiting")
    relay_setup.run("set", telegram=tg(fake), url="https://relay.example.workers.dev", secret=SECRET)
    assert len(fake.updates) == 1
    assert "1 update(s) waiting" in relay_setup.run("info", telegram=tg(fake))[1]


@pytest.mark.parametrize("url,secret,problem", [
    ("http://insecure.example", SECRET, "https"), ("https://ok.example", "short", "secret"),
    ("https://ok.example", "has spaces in it, not allowed!!", "secret")])
def test_unsafe_relay_settings_are_refused(url, secret, problem):
    code, text = relay_setup.run("set", telegram=tg(FakeTelegram()), url=url, secret=secret, env={})
    assert code == 1 and problem in text


def test_the_secret_can_come_from_the_environment():
    fake = FakeTelegram()
    code, _ = relay_setup.run("set", telegram=tg(fake), url="https://r.example", env={"TELEGRAM_WEBHOOK_SECRET": SECRET})
    assert code == 0 and fake.webhook["secret"] == SECRET


def test_inbox_id_is_printed_for_the_worker_settings():
    kit = Kit()
    code, text = relay_setup.run("inbox-id", notion=kit.nclient, env=kit.env)
    assert code == 0 and re.fullmatch(r"NOTION_INBOX_DS=ds-\d+", text)


def test_main_never_prints_secrets_on_failure(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SUPER-SECRET")
    monkeypatch.setattr(relay_setup, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("token 123:SUPER-SECRET leaked")))
    assert relay_setup.main(["info"]) == 1
    out = capsys.readouterr().out
    assert "SUPER-SECRET" not in out and "RuntimeError" in out


# ---------------------------------------------------------------------- doctor
def test_doctor_reports_how_messages_arrive():
    kit = Kit()
    assert "messages are polled" in kit.doctor()["telegram"].detail
    kit.telegram.webhook = {"url": "https://relay.example.workers.dev/hook", "secret": "x" * 20, "fail": True}
    detail = kit.doctor()["telegram"].detail
    assert "via webhook (relay.example.workers.dev)" in detail and "last delivery error" in detail
    assert "/hook" not in detail                                   # only the host is shown


@needs_node
def test_the_dashboard_bundle_is_up_to_date_with_the_worker_source():
    result = subprocess.run([NODE, "build.mjs", "--check"], cwd=RELAY, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
