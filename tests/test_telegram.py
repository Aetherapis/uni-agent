from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from uniagent.telegram import (TelegramAuthError, TelegramClient, TelegramError, mime_for, parse_update,
                               split_text)

from fake_telegram import OWNER, FakeTelegram

TZ = ZoneInfo("Africa/Cairo")


def client(fake, **kw):
    return TelegramClient(fake.token, transport=fake, fetch=fake.fetch, sleep=lambda s: None, **kw)


def test_text_and_voice_updates_are_understood():
    fake = FakeTelegram()
    fake.say("buy a notebook"); fake.voice(); fake.photo()
    parsed = [parse_update(u, TZ) for u in fake.updates]
    assert [p.kind for p in parsed] == ["text", "voice", "unsupported"]
    assert parsed[0].text == "buy a notebook" and parsed[1].file_id.startswith("file-")
    assert parsed[0].sent_at == datetime(2026, 9, 21, 17, 13, 20)        # 1_790_000_000 s = 14:13 UTC = 17:13 Cairo (UTC+3 in September)
    assert parsed[0].chat_id == OWNER


def test_group_chats_edits_and_junk_are_ignored():
    assert parse_update({"update_id": 1, "message": {"chat": {"id": 5, "type": "group"}, "text": "hi"}}, TZ) is None
    assert parse_update({"update_id": 2, "edited_message": {"text": "x"}}, TZ) is None
    assert parse_update({"message": {"text": "no update id"}}, TZ) is None
    assert parse_update({}, TZ) is None


def test_oversized_voice_notes_are_flagged_unsupported():
    fake = FakeTelegram()
    fake.voice(size=50_000_000)
    assert parse_update(fake.updates[0], TZ).kind == "unsupported"


def test_get_updates_confirms_older_messages_through_the_offset():
    fake = FakeTelegram()
    a, b = fake.say("one"), fake.say("two")
    c = client(fake)
    assert [u["update_id"] for u in c.get_updates()] == [a, b]
    assert [u["update_id"] for u in c.get_updates(offset=b)] == [b]            # a is confirmed and gone
    assert c.get_updates(offset=b + 1) == []


def test_long_replies_are_split_into_valid_messages():
    fake = FakeTelegram()
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    client(fake).send_message(OWNER, text)
    assert len(fake.sent) >= 3 and all(len(t) <= 4096 for _, t in fake.sent)
    assert "\n".join(t for _, t in fake.sent) == text


def test_one_enormous_line_is_still_split():
    assert all(len(c) <= 4000 for c in split_text("y" * 9000))


def test_a_wrong_token_raises_without_leaking_it():
    fake = FakeTelegram()
    bad = TelegramClient("999:WRONG-TOKEN", transport=fake, fetch=fake.fetch, sleep=lambda s: None)
    with pytest.raises(TelegramAuthError) as err:
        bad.get_updates()
    assert "WRONG-TOKEN" not in str(err.value)


def test_missing_token_is_reported_up_front():
    with pytest.raises(TelegramAuthError):
        TelegramClient.from_env({})


def test_rate_limits_are_waited_out():
    fake = FakeTelegram(fail_next=[(429, {"ok": False, "parameters": {"retry_after": 3}, "description": "slow down"})])
    waited = []
    c = TelegramClient(fake.token, transport=fake, fetch=fake.fetch, sleep=waited.append)
    c.send_message(OWNER, "hi")
    assert waited == [3.0] and fake.texts() == ["hi"]


def test_voice_notes_download_with_a_mime_type():
    fake = FakeTelegram()
    fake.voice(b"OggS-hello")
    fid = next(iter(fake.files))
    assert client(fake).download_file(fid) == (b"OggS-hello", "audio/ogg")


def test_download_failures_are_errors_not_crashes():
    fake = FakeTelegram()
    with pytest.raises(TelegramError):
        client(fake).download_file("nope")
    fake.voice(b"x" * 100)
    with pytest.raises(TelegramError, match="large"):
        client(fake, max_file_bytes=10).download_file(next(iter(fake.files)))


@pytest.mark.parametrize("path,mime", [("voice/a.oga", "audio/ogg"), ("music/a.mp3", "audio/mp3"),
                                       ("x/a.wav", "audio/wav"), ("x/noext", "audio/ogg")])
def test_mime_types(path, mime):
    assert mime_for(path) == mime
