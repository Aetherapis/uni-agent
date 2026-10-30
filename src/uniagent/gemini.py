"""Minimal Gemini API client (standard library only) using ``generateContent`` over REST.

* One request per batch of messages; the answer is forced into JSON by ``responseSchema``.
* The free tier has a separate quota for each model, so ``generate_json`` walks a model
  fallback chain: a 429 (quota) or 404 (unknown model) moves on to the next model.
* Audio (voice notes) is sent inline as base64.
Never logs the API key, prompts or answers.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Callable, Mapping, Sequence

from .httpjson import Transport, TransportError, urllib_transport

log = logging.getLogger("uniagent.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODELS = ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest")


class GeminiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GeminiAuthError(GeminiError): ...
class GeminiBlocked(GeminiError): ...
class GeminiBadRequest(GeminiError): ...


class GeminiQuotaExhausted(GeminiError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, 429)
        self.retry_after = retry_after


def text_part(text: str) -> dict:
    return {"text": text}


def audio_part(data: bytes, mime_type: str = "audio/ogg") -> dict:
    return {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")}}


class GeminiClient:
    def __init__(self, api_key: str, *, models: Sequence[str] = DEFAULT_MODELS, base_url: str = BASE_URL,
                 transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 2, timeout: float = 90.0) -> None:
        if not api_key:
            raise GeminiAuthError("Gemini API key missing")
        if not models:
            raise ValueError("at least one model is required")
        self._key = api_key
        self.models = tuple(models)
        self.base_url = base_url.rstrip("/")
        self.transport, self.sleep = transport, sleep
        self.max_retries, self.timeout = max_retries, timeout
        self.calls = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "GeminiClient":
        env = os.environ if env is None else env
        return cls(env.get("GEMINI_API_KEY", ""), **kwargs)

    def _post(self, model: str, body: dict) -> Any:
        resp = self.transport("POST", f"{self.base_url}/models/{model}:generateContent",
                              {"x-goog-api-key": self._key, "Accept": "application/json"}, body, self.timeout)
        self.calls += 1
        return resp

    def generate_json(self, system: str, parts: Sequence[dict], schema: dict, *,
                      temperature: float = 0.1, max_output_tokens: int = 8192) -> tuple[dict, str]:
        """Returns (parsed JSON answer, model that produced it)."""
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": list(parts)}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        quota_hit = False
        retry_after: float | None = None
        last_problem = "no model answered"
        for model in self.models:
            attempt = 0
            while attempt <= self.max_retries:
                try:
                    resp = self._post(model, body)
                except TransportError as exc:
                    last_problem = str(exc)
                    attempt += 1
                    self.sleep(min(20.0, 2.0 ** attempt))
                    continue
                status = resp.status
                payload = resp.body if isinstance(resp.body, dict) else {}
                if status == 200:
                    outcome = self._read_answer(payload)
                    if isinstance(outcome, dict):
                        return outcome, model
                    last_problem = outcome                       # unusable answer: try once more, then next model
                    attempt += 1
                    continue
                error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                message = str(error.get("message") or f"HTTP {status}")
                if status == 429:
                    quota_hit = True
                    retry_after = _retry_delay(error) or retry_after
                    last_problem = "quota exhausted"
                    break                                        # this model's quota is gone: next model
                if status == 404:
                    last_problem = f"model {model} not available"
                    break
                if status in (500, 502, 503, 504):
                    last_problem = f"Gemini server error {status}"
                    attempt += 1
                    self.sleep(min(30.0, 3.0 * 2 ** attempt))
                    continue
                if status in (401, 403) or "API key" in message:
                    raise GeminiAuthError("Gemini rejected the API key or the request is not permitted", status)
                raise GeminiBadRequest(message[:300], status)
        if quota_hit and last_problem == "quota exhausted":
            raise GeminiQuotaExhausted("every configured Gemini model is out of free quota", retry_after)
        raise GeminiError(last_problem)

    @staticmethod
    def _read_answer(payload: Mapping[str, Any]) -> dict | str:
        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise GeminiBlocked(f"Gemini blocked the request ({feedback['blockReason']})")
        candidates = payload.get("candidates") or []
        if not candidates:
            return "empty answer"
        cand = candidates[0]
        finish = cand.get("finishReason")
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        if finish not in (None, "STOP"):
            return f"answer incomplete ({finish})"
        try:
            data = json.loads(text)
        except ValueError:
            return "answer was not valid JSON"
        return data if isinstance(data, dict) else "answer was not a JSON object"


def _retry_delay(error: Mapping[str, Any]) -> float | None:
    for detail in error.get("details") or []:
        raw = detail.get("retryDelay") if isinstance(detail, dict) else None
        if isinstance(raw, str) and raw.endswith("s"):
            try:
                return float(raw[:-1])
            except ValueError:
                return None
    return None
