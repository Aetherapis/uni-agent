"""A stand-in for the Telegram Bot API."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from uniagent.httpjson import Response

OWNER = 777


@dataclass
class FakeTelegram:
    token: str = "123:SECRET"
    updates: list = field(default_factory=list)            # unconfirmed updates
    sent: list = field(default_factory=list)               # (chat_id, text)
    files: dict = field(default_factory=dict)              # file_id -> (path, bytes)
    fail_next: list = field(default_factory=list)
    webhook: dict = field(default_factory=dict)             # {"url":..., "secret":...} while a webhook is set
    log: list = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    # ---- helpers for tests ----
    def say(self, text, chat=OWNER, chat_type="private"):
        uid = next(self._ids)
        self.updates.append({"update_id": uid, "message": {
            "message_id": uid, "date": 1_790_000_000, "text": text, "chat": {"id": chat, "type": chat_type}}})
        return uid

    def voice(self, data=b"OggS-voice", chat=OWNER, size=None):
        uid = next(self._ids)
        fid = f"file-{uid}"
        self.files[fid] = (f"voice/{fid}.oga", data)
        self.updates.append({"update_id": uid, "message": {
            "message_id": uid, "date": 1_790_000_000, "chat": {"id": chat, "type": "private"},
            "voice": {"file_id": fid, "duration": 5, "file_size": size or len(data)}}})
        return uid

    def photo(self, chat=OWNER):
        uid = next(self._ids)
        self.updates.append({"update_id": uid, "message": {
            "message_id": uid, "date": 1_790_000_000, "chat": {"id": chat, "type": "private"}, "photo": [{}]}})
        return uid

    def texts(self, chat=OWNER):
        return [t for c, t in self.sent if c == chat]

    # ---- transport ----
    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith("https://api.telegram.org/bot"), url
        tok, _, name = url[len("https://api.telegram.org/bot"):].partition("/")
        self.log.append((name, body))
        if tok != self.token:
            return Response(401, {}, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if self.fail_next:
            status, payload = self.fail_next.pop(0)
            return Response(status, {}, payload)
        if name == "getMe":
            return Response(200, {}, {"ok": True, "result": {"id": 42, "is_bot": True, "username": "uni_agent_bot"}})
        if name == "setWebhook":
            import re as _re
            if not str(body.get("url", "")).startswith("https://") or not _re.fullmatch(r"[A-Za-z0-9_-]{1,256}", str(body.get("secret_token", ""))):
                return Response(400, {}, {"ok": False, "description": "Bad Request: invalid webhook url or secret_token"})
            self.webhook = {"url": body["url"], "secret": body["secret_token"]}
            return Response(200, {}, {"ok": True, "result": True})
        if name == "deleteWebhook":
            self.webhook = {}
            return Response(200, {}, {"ok": True, "result": True})
        if name == "getWebhookInfo":
            return Response(200, {}, {"ok": True, "result": {
                "url": self.webhook.get("url", ""), "pending_update_count": len(self.updates),
                "last_error_message": "Wrong response from the webhook: 500" if self.webhook.get("fail") else None}})
        if name == "getUpdates":
            if self.webhook:
                return Response(409, {}, {"ok": False, "error_code": 409, "description":
                                          "Conflict: can't use getUpdates method while webhook is active; use deleteWebhook to delete the webhook first"})
            offset = body.get("offset")
            if offset is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]      # confirmed ones vanish
            return Response(200, {}, {"ok": True, "result": [dict(u) for u in self.updates[: body.get("limit", 100)]]})
        if name == "sendMessage":
            assert len(body["text"]) <= 4096
            self.sent.append((body["chat_id"], body["text"]))
            return Response(200, {}, {"ok": True, "result": {"message_id": 1}})
        if name == "getFile":
            entry = self.files.get(body["file_id"])
            if not entry:
                return Response(400, {}, {"ok": False, "description": "Bad Request: invalid file_id"})
            return Response(200, {}, {"ok": True, "result": {"file_id": body["file_id"], "file_path": entry[0],
                                                              "file_size": len(entry[1])}})
        return Response(404, {}, {"ok": False, "description": "no such method"})

    def fetch(self, url, timeout):
        assert f"/file/bot{self.token}/" in url
        path = url.split(f"/file/bot{self.token}/", 1)[1]
        for fid, (p, data) in self.files.items():
            if p == path:
                return 200, data
        return 404, b""
