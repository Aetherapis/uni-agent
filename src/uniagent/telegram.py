"""Minimal Telegram Bot API client (standard library only).

Telegram keeps undelivered messages for 24 hours, so polling every few minutes loses nothing.
Only the owner's private chat is ever accepted. The bot token lives in every request URL, so it
is scrubbed from all error messages and never logged.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .httpjson import Transport, TransportError, urllib_transport

log = logging.getLogger("uniagent.telegram")

API_URL = "https://api.telegram.org"
MAX_TEXT = 4000                      # Telegram's hard limit is 4096 characters per message


class TelegramError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TelegramAuthError(TelegramError): ...


Fetch = Callable[[str, float], "tuple[int, bytes]"]


def urllib_fetch(url: str, timeout: float = 60.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except urllib.error.URLError as exc:
        raise TransportError(f"network error: {exc.reason}") from exc


@dataclass(frozen=True)
class Incoming:
    update_id: int
    chat_id: int
    message_id: int
    kind: str                  # "text" | "voice" | "unsupported"
    text: str = ""
    file_id: str = ""
    sent_at: datetime | None = None      # naive local time


def parse_update(update: Mapping[str, Any], tz: ZoneInfo, max_voice_bytes: int = 15_000_000) -> Incoming | None:
    """Turn a raw update into an Incoming message; None if it isn't a private-chat message."""
    msg = update.get("message")
    if not isinstance(msg, Mapping) or not isinstance(update.get("update_id"), int):
        return None
    chat = msg.get("chat") or {}
    if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
        return None
    sent = None
    if isinstance(msg.get("date"), int):
        sent = datetime.fromtimestamp(msg["date"], timezone.utc).astimezone(tz).replace(tzinfo=None)
    base = dict(update_id=update["update_id"], chat_id=chat["id"], message_id=int(msg.get("message_id", 0)), sent_at=sent)
    voice = msg.get("voice") or msg.get("audio")
    if isinstance(voice, Mapping) and voice.get("file_id"):
        if int(voice.get("file_size") or 0) > max_voice_bytes:
            return Incoming(kind="unsupported", text="that voice note is too large", **base)
        return Incoming(kind="voice", file_id=str(voice["file_id"]), text=str(msg.get("caption") or "").strip(), **base)
    if isinstance(msg.get("text"), str) and msg["text"].strip():
        return Incoming(kind="text", text=msg["text"].strip(), **base)
    return Incoming(kind="unsupported", text="I can only read text and voice notes", **base)


def mime_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"mp3": "audio/mp3", "wav": "audio/wav", "flac": "audio/flac", "aac": "audio/aac",
            "m4a": "audio/aac"}.get(ext, "audio/ogg")           # Telegram voice notes are .oga (OGG/Opus)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line breaks so a long reply becomes a few readable messages."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks or [""]


class TelegramClient:
    def __init__(self, token: str, *, base_url: str = API_URL, transport: Transport = urllib_transport,
                 fetch: Fetch = urllib_fetch, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 30.0, max_file_bytes: int = 15_000_000) -> None:
        if not token:
            raise TelegramAuthError("Telegram bot token missing")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.transport, self.fetch, self.sleep = transport, fetch, sleep
        self.timeout, self.max_file_bytes = timeout, max_file_bytes

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "TelegramClient":
        env = os.environ if env is None else env
        return cls(env.get("TELEGRAM_BOT_TOKEN", ""), **kwargs)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***")

    def _call(self, method: str, body: dict | None = None) -> Any:
        for attempt in range(3):
            try:
                resp = self.transport("POST", f"{self.base_url}/bot{self._token}/{method}", {}, body or {}, self.timeout)
            except TransportError as exc:
                raise TelegramError(self._scrub(str(exc))) from exc
            payload = resp.body if isinstance(resp.body, dict) else {}
            if resp.status == 200 and payload.get("ok"):
                return payload.get("result")
            if resp.status == 429 and attempt < 2:
                wait = float((payload.get("parameters") or {}).get("retry_after", 2))
                self.sleep(min(wait, 30.0))
                continue
            description = self._scrub(str(payload.get("description") or f"HTTP {resp.status}"))
            if resp.status in (401, 404):
                raise TelegramAuthError(f"Telegram rejected the bot token ({resp.status})", resp.status)
            raise TelegramError(description, resp.status)
        raise TelegramError("Telegram rate limit", 429)

    def get_me(self) -> dict:
        return dict(self._call("getMe") or {})

    def set_webhook(self, url: str, secret_token: str) -> None:
        """Deliver updates to ``url`` (HTTPS) instead of polling. Queued updates are kept, not dropped."""
        self._call("setWebhook", {"url": url, "secret_token": secret_token, "allowed_updates": ["message"],
                                  "drop_pending_updates": False})

    def delete_webhook(self) -> None:
        """Back to polling with getUpdates."""
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_webhook_info(self) -> dict:
        return dict(self._call("getWebhookInfo") or {})

    def get_updates(self, offset: int | None = None, limit: int = 100) -> list[dict]:
        body: dict = {"limit": limit, "timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        return list(self._call("getUpdates", body) or [])

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in split_text(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        info = self._call("getFile", {"file_id": file_id}) or {}
        path = info.get("file_path")
        if not path:
            raise TelegramError("Telegram returned no file path")
        if int(info.get("file_size") or 0) > self.max_file_bytes:
            raise TelegramError("file too large")
        try:
            status, data = self.fetch(f"{self.base_url}/file/bot{self._token}/{path}", self.timeout)
        except TransportError as exc:
            raise TelegramError(self._scrub(str(exc))) from exc
        if status != 200 or not data:
            raise TelegramError(f"could not download the file ({status})", status)
        return data, mime_for(path)
