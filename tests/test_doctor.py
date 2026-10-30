from pathlib import Path

import pytest

from uniagent.config import load_rules
from uniagent.doctor import REQUIRED_ENV, SILENCE, format_checks, main, run_doctor
from uniagent.gemini import GeminiClient
from uniagent.habitica import HabiticaClient
from uniagent.notion import NotionClient
from uniagent.notion_setup import setup
from uniagent.telegram import TelegramClient

from fake_gemini import FakeGemini
from fake_habitica import FakeClock, FakeHabitica
from fake_notion import FakeNotion
from fake_telegram import OWNER, FakeTelegram

from conftest import SEED_PATH

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config.toml")
PAGE = "1f2e3d4c5b6a79881f2e3d4c5b6a7988"


class Kit:
    def __init__(self, seed=True):
        self.notion = FakeNotion(known_pages={PAGE})
        self.habitica, self.telegram, self.gemini = FakeHabitica(), FakeTelegram(), FakeGemini()
        clock = FakeClock()
        self.nclient = NotionClient(self.notion.token, transport=self.notion, clock=clock.now, sleep=clock.sleep)
        setup(self.nclient, PAGE, RULES, seed_path=SEED_PATH if seed else None)
        self.env = {"NOTION_TOKEN": self.notion.token, "NOTION_PARENT_PAGE": PAGE,
                    "HABITICA_USER_ID": self.habitica.user_id, "HABITICA_API_KEY": self.habitica.api_key,
                    "TELEGRAM_BOT_TOKEN": self.telegram.token, "TELEGRAM_CHAT_ID": str(OWNER),
                    "GEMINI_API_KEY": self.gemini.api_key}

    def clients(self, **overrides):
        base = dict(
            notion=self.nclient,
            habitica=HabiticaClient(self.habitica.user_id, self.habitica.api_key, call_delay_s=0, transport=self.habitica,
                                    clock=self.habitica.clock.now, sleep=self.habitica.clock.sleep),
            telegram=TelegramClient(self.telegram.token, transport=self.telegram, fetch=self.telegram.fetch,
                                    sleep=lambda s: None),
            gemini=GeminiClient(self.gemini.api_key, models=RULES.gemini_models, transport=self.gemini,
                                sleep=lambda s: None))
        base.update(overrides)
        return base

    def doctor(self, env=None, **flags):
        return {c.name: c for c in run_doctor(RULES, env or self.env, **self.clients(), **flags)}


def test_everything_configured_passes_every_check():
    kit = Kit()
    kit.habitica.add_foreign("todo", "mine")
    kit.gemini.answers = [{"ok": True}, {"ok": True, "heard": ""}]
    checks = kit.doctor(ai=True, audio=True, ping=True)
    assert all(c.ok for c in checks.values()), format_checks(list(checks.values()))
    assert "16 classes" in checks["notion"].detail and "4 bus trips" in checks["notion"].detail
    assert "write access ok" in checks["notion"].detail and "1 tasks (0 managed" in checks["habitica"].detail
    assert "@uni_agent_bot" in checks["telegram"].detail and kit.telegram.texts() == ["Doctor check: Telegram works."]
    assert "audio/ogg" in checks["gemini-audio"].detail


def test_the_notion_write_probe_leaves_nothing_behind():
    kit = Kit()
    kit.doctor()
    assert [r for r in kit.notion.rows("State") if "doctor-check" in str(r["properties"]["Key"])] == []


def test_the_audio_probe_sends_a_real_ogg_opus_clip():
    kit = Kit()
    kit.gemini.answers = [{"ok": True}, {"ok": True, "heard": ""}]
    checks = kit.doctor(audio=True)
    assert len(kit.gemini.log) == 2 and checks["gemini-audio"].ok         # a plain check first, then the audio one
    parts = kit.gemini.log[-1][2]["contents"][0]["parts"]
    audio = next(p for p in parts if "inline_data" in p)["inline_data"]
    import base64
    clip = base64.b64decode(audio["data"])
    assert audio["mime_type"] == "audio/ogg" and clip == SILENCE.read_bytes()
    assert clip[:4] == b"OggS" and b"OpusHead" in clip and len(clip) < 20_000


def test_gemini_is_only_called_when_asked():
    kit = Kit()
    assert "gemini" not in kit.doctor() and kit.gemini.log == []


def test_missing_settings_are_listed_by_name_only():
    kit = Kit()
    env = dict(kit.env); del env["GEMINI_API_KEY"]; env["TELEGRAM_CHAT_ID"] = ""
    check = kit.doctor(env)["settings"]
    assert not check.ok and "GEMINI_API_KEY" in check.detail and "TELEGRAM_CHAT_ID" in check.detail


def test_each_failure_is_reported_separately_and_never_leaks_a_secret():
    kit = Kit()
    wrong = kit.clients(
        notion=NotionClient("ntn_WRONG", transport=kit.notion),
        habitica=HabiticaClient(kit.habitica.user_id, "HAB-WRONG", call_delay_s=0, transport=kit.habitica,
                                clock=kit.habitica.clock.now, sleep=kit.habitica.clock.sleep),
        telegram=TelegramClient("1:TG-WRONG", transport=kit.telegram, fetch=kit.telegram.fetch, sleep=lambda s: None),
        gemini=GeminiClient("AIza-WRONG", models=("m",), transport=kit.gemini, sleep=lambda s: None))
    checks = run_doctor(RULES, kit.env, ai=True, **wrong)
    by = {c.name: c for c in checks}
    assert not any(by[n].ok for n in ("notion", "habitica", "telegram", "gemini"))
    text = format_checks(checks)
    for secret in ("ntn_WRONG", "HAB-WRONG", "TG-WRONG", "AIza-WRONG", kit.notion.token, kit.habitica.api_key,
                   kit.telegram.token, kit.gemini.api_key):
        assert secret not in text


def test_quota_problems_are_named():
    kit = Kit()
    kit.gemini.quota_models = set(RULES.gemini_models)
    check = kit.doctor(ai=True)["gemini"]
    assert not check.ok and "GeminiQuotaExhausted" in check.detail


def test_an_empty_timetable_is_called_out():
    kit = Kit(seed=False)
    check = kit.doctor()["timetable"]
    assert not check.ok and "empty" in check.detail


def test_a_wrong_parent_page_is_explained():
    kit = Kit()
    env = dict(kit.env, NOTION_PARENT_PAGE="a" * 32)
    check = kit.doctor(env)["notion"]
    assert not check.ok and "NotionNotFound" in check.detail and "shared" in check.detail


def test_main_exits_nonzero_and_names_the_problem(monkeypatch, capsys):
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert main(["--config", str(ROOT / "config.toml")]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] settings: missing: NOTION_TOKEN" in out and "[FAIL] notion" in out and "[FAIL] habitica" in out


def test_main_reports_a_broken_config_file(tmp_path, capsys):
    bad = tmp_path / "config.toml"
    bad.write_text('timezone = "Mars/Base"\n')
    assert main(["--config", str(bad)]) == 1 and "[FAIL] config" in capsys.readouterr().out
