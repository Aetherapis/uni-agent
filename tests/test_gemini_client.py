import base64
import json

import pytest

from uniagent.gemini import (GeminiAuthError, GeminiBadRequest, GeminiBlocked, GeminiClient, GeminiError,
                             GeminiQuotaExhausted, audio_part, text_part)
from uniagent.httpjson import Response

from fake_gemini import FakeGemini

MODELS = ("m-flash", "m-lite", "m-latest")
SCHEMA = {"type": "OBJECT", "properties": {"commands": {"type": "ARRAY", "items": {"type": "OBJECT"}}}}


def make(fake, **kw):
    sleeps = []
    client = GeminiClient(fake.api_key, models=MODELS, transport=fake, sleep=sleeps.append, **kw)
    return client, sleeps


def ask(client):
    return client.generate_json("system text", [text_part("hello")], SCHEMA)


def test_request_shape_and_key_header():
    fake = FakeGemini(answers=[{"commands": [], "questions": []}])
    client, _ = make(fake)
    data, model = ask(client)
    assert model == "m-flash" and data == {"commands": [], "questions": []}
    _, headers, body = fake.log[0]
    assert headers["x-goog-api-key"] == "AIza-secret"
    assert body["systemInstruction"]["parts"][0]["text"] == "system text"
    assert body["contents"][0]["parts"] == [{"text": "hello"}]
    assert body["generationConfig"]["responseSchema"] == SCHEMA


def test_audio_is_sent_inline_as_base64():
    part = audio_part(b"\x00\x01OggS", "audio/ogg")
    assert part["inline_data"]["mime_type"] == "audio/ogg"
    assert base64.b64decode(part["inline_data"]["data"]) == b"\x00\x01OggS"


def test_quota_on_one_model_falls_back_to_the_next():
    fake = FakeGemini(quota_models={"m-flash"}, answers=[{"commands": [], "questions": ["q"]}])
    client, _ = make(fake)
    data, model = ask(client)
    assert model == "m-lite" and data["questions"] == ["q"]
    assert [m for m, _, _ in fake.log] == ["m-flash", "m-lite"]


def test_every_model_out_of_quota_is_reported_with_the_retry_delay():
    fake = FakeGemini(quota_models=set(MODELS))
    client, _ = make(fake)
    with pytest.raises(GeminiQuotaExhausted) as err:
        ask(client)
    assert err.value.retry_after == 37.0 and len(fake.log) == 3


def test_unknown_model_is_skipped():
    fake = FakeGemini(missing_models={"m-flash", "m-lite"}, answers=[{"commands": [], "questions": []}])
    client, _ = make(fake)
    assert ask(client)[1] == "m-latest"


def test_server_errors_are_retried_with_backoff():
    fake = FakeGemini(fail_next=[(503, {"error": {"message": "overloaded"}}), (500, {})],
                      answers=[{"commands": [], "questions": []}])
    client, sleeps = make(fake)
    assert ask(client)[1] == "m-flash" and len(sleeps) == 2


def test_garbled_answers_are_retried_then_the_next_model_is_tried():
    fake = FakeGemini(answers=["not json", "{still not", "[]", {"commands": [], "questions": []}])
    client, _ = make(fake, max_retries=1)
    data, model = ask(client)
    assert model == "m-lite"                      # m-flash gave two bad answers (1 try + 1 retry)


def test_gives_up_with_a_clear_error_when_nothing_usable_comes_back():
    fake = FakeGemini(answers=["x"] * 20)
    client, _ = make(fake, max_retries=0)
    with pytest.raises(GeminiError) as err:
        ask(client)
    assert not isinstance(err.value, GeminiQuotaExhausted)


def test_truncated_answers_are_not_accepted():
    class Truncating(FakeGemini):
        def __call__(self, method, url, headers, body, timeout):
            return Response(200, {}, {"candidates": [{"content": {"parts": [{"text": '{"commands": ['}]},
                                                       "finishReason": "MAX_TOKENS"}]})
    client, _ = make(Truncating(), max_retries=0)
    with pytest.raises(GeminiError, match="incomplete"):
        ask(client)


def test_bad_key_raises_without_leaking_it():
    fake = FakeGemini()
    client = GeminiClient("AIza-WRONG", models=MODELS, transport=fake, sleep=lambda s: None)
    with pytest.raises(GeminiAuthError) as err:
        ask(client)
    assert "AIza-WRONG" not in str(err.value)


def test_missing_key_is_reported_up_front():
    with pytest.raises(GeminiAuthError):
        GeminiClient.from_env({})


def test_blocked_prompts_are_surfaced_not_retried():
    class Blocking(FakeGemini):
        def __call__(self, method, url, headers, body, timeout):
            self.log.append((url, headers, body))
            return Response(200, {}, {"promptFeedback": {"blockReason": "SAFETY"}})
    fake = Blocking()
    client, _ = make(fake)
    with pytest.raises(GeminiBlocked):
        ask(client)
    assert len(fake.log) == 1


def test_other_client_errors_are_not_retried():
    fake = FakeGemini(fail_next=[(400, {"error": {"message": "Invalid JSON payload"}})])
    client, _ = make(fake)
    with pytest.raises(GeminiBadRequest):
        ask(client)
    assert len(fake.log) == 1
